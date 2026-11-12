"""AgriAgent strict model-output contract and weather fail-closed behavior.

DS-10 / NEW-07 / DS-24: a model returning a non-object JSON, a non-boolean can_proceed
(the string "false"), a non-finite or out-of-range confidence, or a non-string risk list
must not crash and must degrade conservatively. A spray decision whose weather tool
fails (no evidence) must be blocked. The agri-agent fixtures already isolate external
services; this file only overrides the model and weather seams.
"""
import pytest

from app.schemas.agent import AgentDecisionRequest
from app.services.agent_service import AgriAgentService
from app.services.weather_service import WeatherService
from tests.test_agri_agent import farm_env, mock_external_services  # noqa: F401  (fixtures)

GENERAL_QUERY = "番茄种植需要注意哪些要点？"      # non-spray → no weather block
SPRAY_QUERY = "番茄现在可以打药防治晚疫病吗？"      # spray → weather block applies


def _synth(payload):
    async def _inner(*args, **kwargs):
        return payload
    return _inner


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 1, 0, [], {}, None, "yes"])
async def test_can_proceed_requires_real_boolean(test_session, farm_env, mock_external_services,
                                                 monkeypatch, value):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": value, "conclusion": "x", "confidence": 0.9,
        "risks": [], "recommendations": [], "need_expert": False,
    }))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.can_proceed is False


async def _add_general_knowledge(test_session, farm_env):
    from app.models.knowledge import KnowledgeItem
    from app.services.settings_service import SettingsService, RAGConfig
    test_session.add(KnowledgeItem(id="general_applicable_fixture", question="番茄种植需要注意哪些要点", answer="测试资料，需现场核对",
        crop=farm_env["crop"].crop, region="全国", source_type="test_fixture", source_name="隔离测试资料", authority_level="L4"))
    await test_session.commit()
    await SettingsService.set_rag_config(test_session, RAGConfig(similarity_threshold=0.0))


@pytest.mark.asyncio
async def test_true_boolean_allows_non_spray_only_with_knowledge(test_session, farm_env, mock_external_services, monkeypatch):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": True, "conclusion": "x", "confidence": 0.9,
        "risks": [], "recommendations": [], "need_expert": False,
    }))
    request = AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id)
    without = await AgriAgentService.decide(test_session, request, current_user=farm_env["farmer"])
    assert without.can_proceed is False and without.evidence_level == "records_only"  # BUG-013
    await _add_general_knowledge(test_session, farm_env)
    decision = await AgriAgentService.decide(test_session, request, current_user=farm_env["farmer"])
    assert decision.can_proceed is True and decision.evidence_level == "knowledge"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 2.0, -0.5, "0.9", None, True])
async def test_confidence_must_be_finite_and_in_range(test_session, farm_env, mock_external_services,
                                                      monkeypatch, value):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": True, "conclusion": "x", "confidence": value,
        "risks": [], "recommendations": [], "need_expert": False,
    }))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    # Invalid self-ratings fall back to 0.60, then the evidence cap (no knowledge → ≤0.5) applies.
    assert decision.confidence <= 0.60 and decision.need_expert is True


@pytest.mark.asyncio
async def test_confidence_valid_value_preserved(test_session, farm_env, mock_external_services, monkeypatch):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": True, "conclusion": "x", "confidence": 0.93,
        "risks": [], "recommendations": [], "need_expert": False,
    }))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.confidence == 0.5  # BUG-011: self-rating capped when no knowledge evidence exists
    await _add_general_knowledge(test_session, farm_env)
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.confidence == 0.93


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], "text", 42, None])
async def test_non_object_model_output_degrades_conservatively(test_session, farm_env, mock_external_services,
                                                               monkeypatch, payload):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth(payload))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.can_proceed is False             # non-object degrades conservatively
    assert decision.confidence <= 0.60


@pytest.mark.asyncio
async def test_non_string_risks_are_dropped(test_session, farm_env, mock_external_services, monkeypatch):
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": False, "conclusion": "x", "confidence": 0.9,
        "risks": ["真实风险", 123, {"nested": "risk"}, None], "recommendations": [], "need_expert": False,
    }))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=GENERAL_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert "真实风险" in decision.risks
    assert all(isinstance(r, str) for r in decision.risks)


@pytest.mark.asyncio
async def test_spray_decision_blocks_when_weather_evidence_missing(test_session, farm_env,
                                                                   mock_external_services, monkeypatch):
    async def failing_weather(**kwargs):
        raise RuntimeError("isolated weather outage")

    monkeypatch.setattr(WeatherService, "get_real_weather", failing_weather)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": True, "conclusion": "可以打药", "confidence": 0.95,
        "risks": [], "recommendations": ["立即喷药"], "need_expert": False,
    }))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=SPRAY_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.can_proceed is False


async def _suitable_weather(**kwargs):
    return {
        "current": {"temperature": 22.0, "humidity": 60.0, "weather": "晴",
                    "rainfall": 0.0, "wind_speed": 2.0, "source": "Open-Meteo"},
        "forecasts": [],
        "spray_safety": {"is_suitable_to_spray": True, "spray_risks": [], "has_rain_soon": False, "is_offline_warning": False},
    }


@pytest.mark.asyncio
async def test_suitable_weather_cannot_replace_label_review(test_session, farm_env,
                                                            mock_external_services, monkeypatch):
    monkeypatch.setattr(WeatherService, "get_real_weather", _suitable_weather)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth({
        "can_proceed": True, "conclusion": "【适宜作业】气象条件适宜", "confidence": 0.92,
        "risks": [], "recommendations": ["按规程喷施"], "need_expert": False,
    }))
    # The approval branch requires applicable retrieval evidence as well as a valid model/weather result.
    from app.models.knowledge import KnowledgeItem
    from app.services.settings_service import SettingsService, RAGConfig
    test_session.add(KnowledgeItem(id="spray_applicable_fixture", question="隔离测试适用依据", answer="测试资料，需现场核对",
        crop=farm_env["crop"].crop, growth_stage=farm_env["crop"].growth_stage, disease='晚疫病', region='全国', source_type="test_fixture", source_name="隔离测试资料", authority_level="L4"))
    await test_session.commit()
    await SettingsService.set_rag_config(test_session, RAGConfig(similarity_threshold=0.0))
    decision = await AgriAgentService.decide(
        test_session, AgentDecisionRequest(query=SPRAY_QUERY, field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])
    assert decision.can_proceed is False
    assert decision.need_expert is True
    assert '不构成作业许可' in decision.conclusion
