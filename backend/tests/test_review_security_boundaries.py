"""Regression tests for redirect SSRF, bootstrap ordering and model reasons."""
import io
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import inspect

from app.config import settings, is_production_environment
from app.core.embedding import EmbeddingClient
from app.database import init_db
from app.schemas.agent import AgentDecisionRequest
from app.services.agent_service import AgriAgentService, AgriToolRegistry
from tests.test_bootstrap_security import temp_init  # noqa: F401
from tests.test_agri_agent import farm_env, mock_external_services  # noqa: F401


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["http://127.0.0.1/internal", "http://[::1]/internal", "https://other.example.com/private"])
def test_embedding_does_not_follow_redirects(code, destination, monkeypatch, controlled_dns):
    controlled_dns({"public.example.com": ["93.184.216.34"]})
    client = EmbeddingClient()
    monkeypatch.setattr(client, "_override_api_key", "synthetic-test-key")
    monkeypatch.setattr(client, "_override_api_url", "https://public.example.com/v1")
    visited = []

    class MemoryAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            visited.append(request.url)
            response = requests.Response()
            response.status_code = code
            response.headers["Location"] = destination
            response.url = request.url
            response.request = request
            response.raw = io.BytesIO()
            response._content = b""
            return response

        def close(self):
            pass

    # The isolated runner disables Session.send; restore only the genuine method
    # on this local session, whose adapters can never touch the network.
    session = requests.Session()
    session.trust_env = False
    session.mount("http://", MemoryAdapter())
    session.mount("https://", MemoryAdapter())
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("requests.review_sessions", requests.sessions.__file__)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    session.send = module.Session.send.__get__(session, requests.Session)
    monkeypatch.setattr("app.core.embedding.requests.post", session.post)
    try:
        assert client._embed_batch(["test text"]) is None
        assert visited == ["https://public.example.com/v1/embeddings"]
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["", "prodution", "staging"])
def test_unknown_modes_keep_production_protections(mode, monkeypatch):
    monkeypatch.setenv("APP_ENV", mode)
    monkeypatch.delenv("ENV", raising=False)
    assert is_production_environment() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["jwt", "cipher", "admin-password", "admin-short"])
async def test_invalid_bootstrap_has_no_table_side_effects(failure, temp_init, monkeypatch):
    import app.database as dbmod
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-only-strong-jwt-key-0123456789")
    monkeypatch.setattr(settings, "SETTINGS_ENCRYPTION_KEY", "test-only-strong-cipher-key-0123456789")
    monkeypatch.delenv("INITIAL_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("INITIAL_ADMIN_PASSWORD", raising=False)
    if failure == "jwt":
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "short")
    elif failure == "cipher":
        monkeypatch.setattr(settings, "SETTINGS_ENCRYPTION_KEY", "")
    else:
        monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "root_admin")
        if failure == "admin-short":
            monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "short")
    with pytest.raises(RuntimeError):
        await init_db()
    async with dbmod.engine.connect() as connection:
        assert await connection.run_sync(lambda c: inspect(c).get_table_names()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [{"unexpected": "object"}, ["list"], 123, "x" * 5000, None])
async def test_expert_reason_always_matches_response_schema(
        reason, test_session, farm_env, mock_external_services, monkeypatch):
    original = AgriToolRegistry.execute_tool

    async def execute(name, args, db):
        if name == "diagnose_crop_image":
            return SimpleNamespace(status="success", result={
                "suspected_disease": "番茄早疫病", "confidence": 0.80,
                "need_expert": True, "features_detected": ["真实轮纹"], "advice": "专家复核"})
        return await original(name, args, db)

    async def synthesize(*args, **kwargs):
        return {"can_proceed": False, "confidence": 0.95, "conclusion": "复核",
                "need_expert": False, "expert_reason": reason,
                "risks": [], "recommendations": []}

    monkeypatch.setattr(AgriToolRegistry, "execute_tool", staticmethod(execute))
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synthesize)
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query="番茄叶片有斑点，请看看图片",
            field_id=farm_env["field"].id, image_url="http://example.com/leaf.jpg"),
        current_user=farm_env["farmer"])
    assert decision.need_expert is True
    assert isinstance(decision.expert_reason, str) and 0 < len(decision.expert_reason) <= 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("auto,hard_gate,confidence,expected_ticket", [
    (False, False, 0.45, False), (True, False, 0.45, True),
    (False, True, 0.45, True), (False, True, 0.95, True),
    (False, False, 0.95, False),
])
async def test_visual_ticket_switch_preserves_safety_gate(
        auto, hard_gate, confidence, expected_ticket,
        test_session, farm_env, mock_external_services, monkeypatch):
    from sqlalchemy import select
    from app.models.disease_record import DiseaseRecord
    from app.schemas.expert import VisualDiagnosisRequest
    from app.services.agent_service import AgriTools
    from app.services.expert_service import ExpertWorkbenchService

    async def diagnose(**kwargs):
        return {"suspected_disease": "番茄早疫病", "confidence": confidence,
                "need_expert": hard_gate or confidence < 0.70, "mandatory_review": hard_gate,
                "features_detected": ["真实轮纹", "叶面颜色"],
                "description": "待复核的真实叶片", "severity": "medium"}

    monkeypatch.setattr(AgriTools, "diagnose_crop_image", diagnose)
    response = await ExpertWorkbenchService.visual_diagnose(
        test_session, VisualDiagnosisRequest(image_url="http://example.com/leaf.jpg",
            field_id=farm_env["field"].id, auto_create_ticket=auto),
        current_user=farm_env["farmer"])
    record = await test_session.scalar(select(DiseaseRecord).where(DiseaseRecord.id == response.disease_record_id))
    assert record is not None
    assert (response.ticket_id is not None) is expected_ticket
    assert record.status == ("pending_review" if expected_ticket else "preliminary")
    assert response.need_expert is (hard_gate or confidence < 0.70)
    if not auto and not hard_gate and confidence < 0.70:
        assert "未创建" in response.advice and "专家" in response.advice
