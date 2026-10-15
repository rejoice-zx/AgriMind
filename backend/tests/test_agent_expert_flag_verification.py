"""Expert-flag inheritance in decide() (NEW-08 / P1-EXPERT-FLAG).

A vision tool that reports need_expert=True ("建议转交农技专家复核") is a hard gate:
the final decision must route to the expert workbench even when the LLM answers
need_expert=False at high confidence. The RAG tool's need_expert is advisory and
must NOT be mechanically OR-ed into the decision.

Before P1-EXPERT-FLAG this file documented the defect (the flag was dropped);
it now asserts the fixed behaviour and guards against over-triggering.
"""
from types import SimpleNamespace

import pytest

from app.schemas.agent import AgentDecisionRequest
from app.services.agent_service import AgriAgentService, AgriToolRegistry
from tests.test_agri_agent import farm_env, mock_external_services  # noqa: F401  (fixtures)


def _synth(payload):
    async def _inner(*args, **kwargs):
        return payload
    return _inner


_HIGH_CONF_NO_EXPERT = {
    "can_proceed": True,
    "conclusion": "按规程作业",
    "confidence": 0.95,
    "risks": [],
    "recommendations": ["常规防治"],
    "need_expert": False,
}


def _vision_result(need_expert):
    return SimpleNamespace(status="success", result={
        "suspected_disease": "番茄早疫病",
        "confidence": 0.80,
        "need_expert": need_expert,
        "features_detected": ["同心轮纹病斑"],
        "advice": "建议转交农技专家复核" if need_expert else "常规防治",
    })


def _available_rag(need_expert=False):
    # Isolate the advisory flag from the independent missing-knowledge hard gate.
    return SimpleNamespace(status='success', result={
        'items': [{'question': 'controlled knowledge', 'answer': 'controlled supporting evidence'}],
        'context_text': 'controlled supporting evidence', 'need_expert': need_expert})


@pytest.mark.asyncio
async def test_vision_need_expert_is_inherited_as_hard_gate(
        test_session, farm_env, mock_external_services, monkeypatch):
    original = AgriToolRegistry.execute_tool

    async def fake_execute(tool_name, args, db):
        if tool_name == "diagnose_crop_image":
            return _vision_result(True)
        if tool_name == 'search_agri_knowledge': return _available_rag()
        return await original(tool_name, args, db)

    monkeypatch.setattr(AgriToolRegistry, "execute_tool", staticmethod(fake_execute))
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth(dict(_HIGH_CONF_NO_EXPERT)))

    decision = await AgriAgentService.decide(
        test_session,
        AgentDecisionRequest(query="番茄叶片有斑点，请看看图片",
                             field_id=farm_env["field"].id,
                             image_url="http://example.com/leaf.jpg"),
        current_user=farm_env["farmer"])

    # Tool said "transfer to expert"; the high-confidence LLM "no expert" must not win.
    assert decision.need_expert is True
    assert decision.expert_reason


@pytest.mark.asyncio
async def test_rag_need_expert_is_not_mechanically_or_ed(
        test_session, farm_env, mock_external_services, monkeypatch):
    """Only the vision flag is a hard gate; the advisory RAG flag must not force expert."""
    original = AgriToolRegistry.execute_tool

    async def fake_execute(tool_name, args, db):
        if tool_name == "search_agri_knowledge":
            return _available_rag(True)
        return await original(tool_name, args, db)

    monkeypatch.setattr(AgriToolRegistry, "execute_tool", staticmethod(fake_execute))
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth(dict(_HIGH_CONF_NO_EXPERT)))

    decision = await AgriAgentService.decide(
        test_session,
        AgentDecisionRequest(query="番茄施肥建议", field_id=farm_env["field"].id),
        current_user=farm_env["farmer"])

    assert decision.need_expert is False


@pytest.mark.asyncio
async def test_no_expert_signal_stays_false(
        test_session, farm_env, mock_external_services, monkeypatch):
    original = AgriToolRegistry.execute_tool

    async def fake_execute(tool_name, args, db):
        if tool_name == "diagnose_crop_image":
            return _vision_result(False)
        if tool_name == 'search_agri_knowledge': return _available_rag()
        return await original(tool_name, args, db)

    monkeypatch.setattr(AgriToolRegistry, "execute_tool", staticmethod(fake_execute))
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", _synth(dict(_HIGH_CONF_NO_EXPERT)))

    decision = await AgriAgentService.decide(
        test_session,
        AgentDecisionRequest(query="番茄叶片有斑点，请看看图片",
                             field_id=farm_env["field"].id,
                             image_url="http://example.com/leaf.jpg"),
        current_user=farm_env["farmer"])

    assert decision.need_expert is False


@pytest.mark.asyncio
async def test_missing_knowledge_is_an_independent_hard_gate(test_session, farm_env, mock_external_services, monkeypatch):
    original = AgriToolRegistry.execute_tool
    async def fake_execute(tool_name, args, db):
        if tool_name == 'search_agri_knowledge':
            return SimpleNamespace(status='success', result={'items': [], 'need_expert': False})
        return await original(tool_name, args, db)
    monkeypatch.setattr(AgriToolRegistry, 'execute_tool', staticmethod(fake_execute))
    monkeypatch.setattr(AgriAgentService, '_synthesize_with_llm', _synth(dict(_HIGH_CONF_NO_EXPERT)))
    decision = await AgriAgentService.decide(test_session,
        AgentDecisionRequest(query='番茄施肥建议', field_id=farm_env['field'].id), current_user=farm_env['farmer'])
    assert decision.need_expert is True and decision.can_proceed is False
