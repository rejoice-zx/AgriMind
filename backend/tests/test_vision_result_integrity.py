"""Exercise real parsing and safety gates with a controlled HTTP client, never a model."""
import json
import asyncio
import httpx
import random
import struct
import zlib
import pytest
from app.config import settings
from app.services.vision_service import VisionService
from app.services.agent_service import AgriTools


@pytest.fixture
def vision_mock(monkeypatch):
    from pathlib import Path
    directory = Path(settings.DATA_DIR) / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    def chunk(name, content):
        return struct.pack(">I", len(content)) + name + content + struct.pack(">I", zlib.crc32(name + content) & 0xffffffff)
    pixels = random.Random(7).randbytes(300 * 300 * 3)
    image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 300, 300, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + pixels[i:i + 900] for i in range(0, len(pixels), 900)))) + chunk(b"IEND", b"")
    (directory / "input.png").write_bytes(image)
    state = {"content": "{}", "status": 200, "calls": [], "failure": False,
             "finish_reason": "stop", "reasoning": "", "client_options": {}, "delay": 0}
    class Response:
        @property
        def status_code(self): return state["status"]
        text = "controlled error"
        def json(self): return {"choices": [{"message": {"content": state["content"],
            "reasoning_content": state["reasoning"]}, "finish_reason": state["finish_reason"]}]}
    class Client:
        def __init__(self, **kwargs): state["client_options"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            state["calls"].append(kwargs["json"])
            if isinstance(state["failure"], Exception): raise state["failure"]
            if state["failure"]: raise RuntimeError("offline")
            if state["delay"]: await asyncio.sleep(state["delay"])
            return Response()
    monkeypatch.setattr("app.services.vision_service.httpx.AsyncClient", Client)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, {}, {"confidence": None}, {"confidence": True}, {"confidence": "0.9"}, {"confidence": 2}, {"confidence": -1}])
async def test_missing_or_invalid_score_stays_null(vision_mock, raw):
    vision_mock["content"] = json.dumps(raw)
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["confidence"] is None and result["need_expert"]
    assert result["severity"] == "unknown" and result["crop"] == "未提供"
    assert "番茄" not in result["suspected_disease"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["model_flag", "non_plant", "too_few_features"])
async def test_safety_gate_preserves_actual_model_score_and_honors_referral(vision_mock, reason):
    raw = {"suspected_disease": "待鉴别", "confidence": 0.912345, "features_detected": ["边缘纹理", "颜色分布"], "severity": "low", "need_expert": False}
    if reason == "model_flag": raw["need_expert"] = True
    if reason == "non_plant": raw["suspected_disease"] = "非植物图片"
    if reason == "too_few_features": raw["features_detected"] = ["边缘纹理", 3, {}]
    vision_mock["content"] = json.dumps(raw)
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["confidence"] == 0.912345 and result["need_expert"]
    assert all(isinstance(value, str) for value in result["features_detected"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parse", "http", "network"])
async def test_failure_has_no_fabricated_score_features_or_stage(vision_mock, failure):
    if failure == "parse": vision_mock["content"] = "invalid-json-with-private-response"
    if failure == "http": vision_mock["status"] = 503
    if failure == "network": vision_mock["failure"] = True
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["confidence"] is None and result["need_expert"]
    assert result["features_detected"] == [] and result["severity"] == "unknown"
    assert "private-response" not in result["description"]


@pytest.mark.asyncio
async def test_tool_without_image_has_no_score_or_fake_ticket():
    result = await AgriTools.diagnose_crop_image("")
    assert result["confidence"] is None and result["crop"] == "未提供"
    assert "未创建专家工单" in result["description"]


@pytest.mark.asyncio
async def test_real_parser_distinguishes_confidence_only_from_mandatory_review(vision_mock):
    raw = {"suspected_disease": "待鉴别", "confidence": 0.55,
           "features_detected": ["边缘纹理", "颜色分布"], "need_expert": False}
    vision_mock['content'] = json.dumps(raw)
    result = await VisionService.diagnose_leaf_image('/static/uploads/input.png', api_key='fixture-only')
    assert result['need_expert'] and result['mandatory_review'] is False
    raw['features_detected'] = ['仅一项']
    vision_mock['content'] = json.dumps(raw)
    result = await VisionService.diagnose_leaf_image('/static/uploads/input.png', api_key='fixture-only')
    assert result['need_expert'] and result['mandatory_review'] is True


@pytest.mark.asyncio
async def test_no_provider_credentials_makes_no_http_call_and_has_no_fake_score(vision_mock):
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="")
    assert vision_mock["calls"] == []
    assert result["confidence"] is None and result["features_detected"] == [] and result["need_expert"]


@pytest.mark.asyncio
async def test_quality_policy_routes_review_without_rewriting_model_score(vision_mock, monkeypatch):
    vision_mock["content"] = json.dumps({"suspected_disease": "待鉴别", "confidence": 0.94, "features_detected": ["特征1", "特征2"], "need_expert": False})
    monkeypatch.setattr("app.services.vision_service.preflight_image_quality", lambda *args: {
        "format": "png", "width": 200, "height": 200, "size_bytes": 1000,
        "suspicious_synthetic": True, "warning_reasons": ["应用复核阈值"],
    })
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["confidence"] == 0.94 and result["need_expert"]
    assert not result["image_quality"]["passed_preflight"]


@pytest.mark.asyncio
@pytest.mark.parametrize("root,instant", [("https://api.siliconflow.cn/v1", True), ("https://gateway.example/v1", False)])
async def test_real_diagnosis_kimi_uses_bounded_instant_call_without_environment_proxy(vision_mock, root, instant):
    vision_mock["content"] = json.dumps({"suspected_disease": "待鉴别", "description": "现场信息不足"})
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only",
        base_url=root, model_name="Pro/moonshotai/Kimi-K2.6")
    assert result["source_model"] == "Pro/moonshotai/Kimi-K2.6"
    assert len(vision_mock["calls"]) == 1
    payload = vision_mock["calls"][0]
    assert payload["max_tokens"] == 2048 and payload["stream"] is False
    assert (payload.get("enable_thinking") is False) is instant
    assert vision_mock["client_options"]["trust_env"] is False
    assert vision_mock["client_options"]["timeout"].read == 90
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [(httpx.ReadTimeout("do not echo fixture-only"), "timeout"),
    (httpx.ConnectError("do not echo fixture-only"), "network_error")])
async def test_diagnosis_transport_failure_is_explicit_and_never_retried(vision_mock, failure, code):
    vision_mock["failure"] = failure
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["diagnosis_status"] == "failed" and result["error_code"] == code
    assert len(vision_mock["calls"]) == 1 and result["confidence"] is None
    assert "fixture-only" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 503])
async def test_diagnosis_http_errors_keep_safe_specific_codes(vision_mock, status):
    vision_mock["status"] = status
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["diagnosis_status"] == "failed" and result["error_code"] == f"http_{status}"
    assert "controlled error" not in result["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish,reasoning,code", [
    ('{}', 'length', '', 'output_truncated'), (None, 'stop', 'private thought', 'reasoning_without_answer'),
    (None, 'stop', '', 'invalid_response'), ('not json', 'stop', '', 'invalid_json'),
])
async def test_diagnosis_invalid_output_is_not_a_completed_diagnosis(vision_mock, content, finish, reasoning, code):
    vision_mock.update(content=content, finish_reason=finish, reasoning=reasoning)
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["diagnosis_status"] == "failed" and result["error_code"] == code
    assert result["features_detected"] == [] and result["confidence"] is None
    assert "private thought" not in json.dumps(result)


@pytest.mark.asyncio
async def test_diagnosis_total_deadline_bounds_slow_provider_without_retry(vision_mock, monkeypatch):
    monkeypatch.setattr(VisionService, "CALL_SECONDS", .01)
    vision_mock["delay"] = .1
    result = await VisionService.diagnose_leaf_image("/static/uploads/input.png", api_key="fixture-only")
    assert result["error_code"] == "timeout" and len(vision_mock["calls"]) == 1
