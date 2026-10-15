"""Persisted agricultural RAG settings, complete eligible candidates and trace contracts."""
import json
from datetime import date
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.database import get_db
from app.models import KnowledgeItem, User
from app.api.dependencies import get_current_user
from app.api.settings import router as settings_router
from app.api.knowledge import router as knowledge_router
from app.services.settings_service import SettingsService, RAGConfig
from app.services.agri_retrieval_service import AgriRetrievalService as Retrieval
from app.services.rag_service import RAGService
from app.services.agent_service import AgriTools, AgriAgentService
from app.schemas.agent import AgentDecisionRequest


@pytest.fixture(autouse=True)
def offline_vectors(monkeypatch):
    async def none(*args, **kwargs): return [], {}
    monkeypatch.setattr(RAGService, "_vector_search_multi", none)
    monkeypatch.setattr("app.services.agri_retrieval_service.agriculture_today", lambda: date(2026, 10, 9))
    # Simulate midnight between admission and RAG: the captured admission date must remain consistent.
    monkeypatch.setattr("app.services.rag_service.agriculture_today", lambda: date(2026, 10, 10))


async def configured(db, **overrides):
    values = dict(top_k=5, similarity_threshold=0.0, use_rewrite=True, max_context_length=500, chunk_size=500, chunk_overlap=50)
    values.update(overrides)
    await SettingsService.set_rag_config(db, RAGConfig(**values))
    return values


def item(id, **values):
    return KnowledgeItem(id=id, question="番茄资料核对", answer="根据现场资料逐项核对" * 120,
        keywords=["番茄"], crop="番茄", source_type="test_fixture", source_name="测试标注来源", authority_level="L4", **values)


async def client_for(db):
    app = FastAPI(); app.include_router(settings_router); app.include_router(knowledge_router)
    async def database(): yield db
    async def admin(): return User(id=90, username="retrieval_admin", role="admin", is_active=True)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = admin
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_no_200_row_limit_and_only_current_eligible_versions(test_session):
    await configured(test_session, top_k=20)
    test_session.add_all([KnowledgeItem(id=f"noise_{i}", question=f"不相关记录{i}", answer="无农业属性", crop="黄瓜") for i in range(205)])
    test_session.add_all([
        item("target"), item("old", is_current=0), item("expired", effective_to="2026-10-08"),
        item("future", effective_from="2026-10-10"), item("malformed", effective_from="2026-13-01"),
        item("reversed", effective_from="2026-10-09", effective_to="2026-10-08"),
        item("boundary", effective_from="2026-10-09", effective_to="2026-10-09"),
    ])
    await test_session.commit()
    result, trace = await Retrieval.search(test_session, "番茄资料核对", {"crop": "番茄"})
    assert {row["id"] for row in result.retrieved_items} == {"target", "boundary"}
    assert trace["current_rows_in_scope"] == 6 and trace["excluded_by_dates"] == 4
    assert trace["eligible_candidates"] == 2 and trace["retrieval_path"] == "bm25"
    assert trace["eligibility_date"] == "2026-10-09" and trace["config_origin"] == "saved"
    assert len(result.context_text) <= 500 and "未自动核验" in result.context_text
    assert all(not row["source_verified"] for row in result.retrieved_items)


@pytest.mark.asyncio
async def test_full_general_candidate_set_is_not_cut_to_first_page(test_session):
    await configured(test_session, top_k=20)
    test_session.add_all([KnowledgeItem(id=f"record_{i:03}", question=f"资料{i}", answer="记录", source_type="test_fixture") for i in range(225)])
    await test_session.commit()
    _, trace = await Retrieval.search(test_session, "资料")
    assert trace["eligible_candidates"] == 225


@pytest.mark.asyncio
async def test_config_changes_apply_on_next_request_from_new_session(test_session, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    test_session.add_all([item("a"), item("b"), item("c")]); await test_session.commit()
    await configured(test_session, top_k=1, use_rewrite=False)
    first = await AgriTools.search_agri_knowledge(test_session, "请问番茄资料核对", crop="番茄")
    assert len(first["items"]) == 1
    assert first["retrieval_trace"]["effective_parameters"]["use_rewrite"] is False
    fingerprint = first["retrieval_trace"]["config_fingerprint"]
    await configured(test_session, top_k=3, use_rewrite=True)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as fresh:
        second = await AgriTools.search_agri_knowledge(fresh, "请问番茄资料核对", crop="番茄")
        assert len(second["items"]) == 3
        assert second["retrieval_trace"]["config_fingerprint"] != fingerprint
        assert len(second["context_text"]) <= 500


@pytest.mark.asyncio
async def test_top_k_override_cannot_exceed_saved_cap(test_session):
    await configured(test_session, top_k=2)
    test_session.add_all([item("a"), item("b"), item("c")]); await test_session.commit()
    result, trace = await Retrieval.search(test_session, "番茄", top_k=20)
    assert len(result.retrieved_items) == 2 and trace["effective_parameters"]["top_k"] == 2
    with pytest.raises(HTTPException) as error: await Retrieval.search(test_session, "番茄", top_k=True)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_strict_fusion_gate_cannot_be_bypassed_by_offline_bm25_policy(monkeypatch):
    rag = RAGService()
    monkeypatch.setattr(rag, "_bm25_search", lambda *args: {"a": 0.2})
    rows = [{"id": "a", "question": "资料", "answer": "内容", "authority_level": "L4", "source_type": "test_fixture", "is_current": 1}]
    legacy = await rag.hybrid_search("资料", rows, threshold=0.95)
    strict = await rag.hybrid_search("资料", rows, threshold=0.95, strict_threshold=True)
    assert len(legacy.retrieved_items) == 1 and strict.retrieved_items == [] and strict.need_expert


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{bad-json', '[]', '{"top_k":0}', '{"use_rewrite":"false"}', '{"similarity_threshold":2}', '{"chunk_overlap":500}'])
async def test_corrupt_config_fails_closed_without_silent_defaults(test_session, raw):
    await SettingsService.set_setting(test_session, SettingsService.KEY_RAG_CONFIG, raw)
    with pytest.raises(HTTPException) as error: await Retrieval.search(test_session, "番茄")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_partial_chunk_update_preserves_retrieval_settings_and_validates_merged_values(test_session):
    original = await configured(test_session, top_k=13, similarity_threshold=0.0, use_rewrite=False, max_context_length=1500, chunk_size=1000)
    async with await client_for(test_session) as client:
        response = await client.put("/api/settings", json={"rag": {"chunk_overlap": 600}})
        assert response.status_code == 200
        saved = response.json()["rag"]
        for key in ("top_k", "similarity_threshold", "use_rewrite", "max_context_length", "chunk_size"):
            assert saved[key] == original[key]
        assert saved["chunk_overlap"] == 600
        before = await SettingsService.get_setting(test_session, SettingsService.KEY_RAG_CONFIG)
        failed = await client.put("/api/settings", json={"rag": {"chunk_size": 500}})
        assert failed.status_code == 422
        assert await SettingsService.get_setting(test_session, SettingsService.KEY_RAG_CONFIG) == before


@pytest.mark.asyncio
async def test_admin_preview_and_agent_tool_use_same_config_and_scope(test_session):
    await configured(test_session, top_k=2, use_rewrite=False)
    test_session.add_all([item("a"), item("b"), item("old", is_current=0)]); await test_session.commit()
    tool = await AgriTools.search_agri_knowledge(test_session, "请问番茄资料核对", crop="番茄")
    async with await client_for(test_session) as client:
        preview = await client.post("/api/knowledge/hybrid-search", json={"query": "请问番茄资料核对", "crop": "番茄"})
        assert preview.status_code == 200
        result = preview.json()
        assert {row["id"] for row in result["retrieved_items"]} == {row["id"] for row in tool["items"]}
        assert result["rewritten_query"] == "请问番茄资料核对"
        assert result["retrieval_trace"] == tool["retrieval_trace"]


def test_context_limit_includes_block_separators():
    rag = RAGService(context_max_chars=500, context_top_n=2)
    rows = [({"id": str(i), "question": "q", "answer": "x" * 238}, 0.5) for i in range(2)]
    assert len(rag._build_context(rows)) <= 500


@pytest.mark.asyncio
async def test_agent_archivable_trace_and_llm_knowledge_context_are_bounded(test_session, monkeypatch):
    await configured(test_session, top_k=2, max_context_length=500)
    test_session.add(item("a")); await test_session.commit()
    captured = {}
    async def synthesize(**kwargs):
        captured.update(kwargs)
        return {"conclusion": "资料核对", "can_proceed": False, "confidence": 0.8, "recommendations": [], "risks": []}
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synthesize)
    result = await AgriAgentService.decide(test_session, AgentDecisionRequest(query="番茄资料核对"))
    trace = next(row for row in result.tool_traces if row.tool_name == "search_agri_knowledge")
    assert trace.execution_metadata["effective_parameters"]["top_k"] == 2
    assert trace.execution_metadata["context_chars"] <= 500
    assert len(captured["rag_info"]["context_text"]) <= 500
    evidence = next(row for row in result.evidences if "知识库" in row["source"])
    assert len(evidence["content"]) <= 500 and "未自动核验" in evidence["content"]
    assert "国家/行业规程" not in evidence["source"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,blocked", [("现在可以施肥吗？", True), ("叶片发黄需要诊断", True), ("一般农业资料是什么？", False)])
async def test_missing_applicable_knowledge_cannot_become_risky_model_approval(test_session, monkeypatch, query, blocked):
    await configured(test_session)
    async def synthesize(**kwargs):
        return {"conclusion": "模型建议执行", "can_proceed": True, "confidence": 0.99, "need_expert": False,
                "recommendations": ["立即执行"], "risks": []}
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synthesize)
    result = await AgriAgentService.decide(test_session, AgentDecisionRequest(query=query))
    assert result.can_proceed is (not blocked)
    if blocked:
        assert result.need_expert and "知识依据不足" in result.conclusion
        assert "立即执行" not in result.recommendations


@pytest.mark.asyncio
async def test_real_agent_history_keeps_config_trace_and_missing_evidence_creates_one_consultation(test_session, monkeypatch):
    from uuid import uuid4
    from app.api.agri_agent import router as agent_router
    from app.api.agri_history import router as history_router
    await configured(test_session, top_k=2)
    user = User(username="retrieval_farmer", role="customer", password_hash="unused", is_active=True)
    test_session.add(user); await test_session.commit()
    calls = []
    async def synthesize(**kwargs):
        calls.append(kwargs)
        return {"conclusion": "模型建议施肥", "can_proceed": True, "confidence": 0.99, "need_expert": False, "risks": [], "recommendations": ["立即执行"]}
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", synthesize)
    app = FastAPI(); app.include_router(agent_router); app.include_router(history_router)
    async def database(): yield test_session
    async def authenticated(): return user
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = authenticated
    body = {"query": "现在能施肥吗？", "request_id": str(uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/agent/decide", json=body)
        assert first.status_code == 200
        saved = first.json()
        assert not saved["can_proceed"] and saved["need_expert"] and saved["consultation_id"]
        metadata = next(row["execution_metadata"] for row in saved["tool_traces"] if row["tool_name"] == "search_agri_knowledge")
        assert metadata["effective_parameters"]["top_k"] == 2 and metadata["eligible_candidates"] == 0
        await configured(test_session, top_k=10)
        retry = (await client.post("/api/agent/decide", json=body)).json()
        assert retry["consultation_id"] == saved["consultation_id"] and len(calls) == 1
        history = (await client.get(f'/api/agent/conversations/{saved["conversation_id"]}')).json()
        recovered = next(row["execution_metadata"] for row in history["items"][0]["decision"]["tool_traces"] if row["tool_name"] == "search_agri_knowledge")
        assert recovered == metadata
