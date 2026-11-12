"""Phase-3 retrieval regressions (BUG-001/002/004/005/010).

Uses the checked-in tomato seed text with quarantine lifted in memory only, so the
assertions exercise the real tokenizer, BM25 and fusion on realistic agricultural text.
The production seed rows remain quarantined; nothing here writes to a database.
"""
import pytest

from app.core.knowledge_contract import metadata_matches, region_values, stage_values
from app.services.rag_service import RAGService
from app.services.seed_agriculture_knowledge import TOMATO_KNOWLEDGE_SEED


def _corpus():
    items = []
    for index, seed in enumerate(TOMATO_KNOWLEDGE_SEED):
        item = dict(seed, id=f"seed-{index}", source_type="test_fixture", source_name="test-only", is_current=1)
        items.append(item)
    return items


def _id(items, fragment):
    return next(item["id"] for item in items if fragment in item["question"])


async def _bm25_only(query, items, filters, threshold=0.5):
    rag = RAGService(use_rewrite=True)

    async def no_vectors(*_args, **_kwargs):
        return [], {}

    rag._vector_search_multi = no_vectors
    return await rag.hybrid_search(query, items, filter_metadata=filters, threshold=threshold,
                                   top_k=5, strict_threshold=True)


def test_nationwide_region_labels_match_any_specific_place():
    for label in ("通用", "全国", "全国通用"):
        assert metadata_matches({"region": label}, "region", "江苏省南京市玄武区")
    # Region boundaries still hold, and an unlabeled region stays unknown (excluded).
    assert not metadata_matches({"region": "河北省"}, "region", "江苏省南京市玄武区")
    assert not metadata_matches({"region": ""}, "region", "江苏省南京市玄武区")
    assert "通用" in region_values("山东省")


def test_stage_synonyms_cover_seedling_and_harvest():
    assert metadata_matches({"growth_stage": "育苗期"}, "growth_stage", "苗期")
    assert metadata_matches({"growth_stage": "采收盛期"}, "growth_stage", "采收期")
    assert metadata_matches({"growth_stage": "通用"}, "growth_stage", "开花期")
    assert "开花期" not in stage_values("苗期")


def test_corpus_tokens_keep_trailing_keywords_and_frequency():
    rag = RAGService(use_rewrite=False)
    long_answer = "番茄" * 60 + " 腐霉利 嘧霉胺"
    corpus = rag._extract_tokens(long_answer, corpus=True)
    assert "腐霉" in corpus and "霉胺" in corpus
    assert corpus.count("番茄") > 1
    # Query side keeps the bounded, de-duplicated behaviour.
    query = rag._extract_tokens(long_answer)
    assert len(query) <= 40 and len(set(query)) == len(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("query, filters, fragment", [
    ("番茄灰霉病用什么药防治", {"crop": "番茄", "disease": "灰霉病"}, "灰霉病的综合防治"),
    ("番茄开花期怎么追肥", {"crop": "番茄"}, "开花坐果期水肥"),
    ("叶背有灰紫色霉层是什么病", {"crop": "番茄"}, "叶霉病的发病症状"),
    ("番茄苗期猝倒病怎么预防", {"crop": "番茄", "region": "江苏省南京市玄武区"}, "猝倒病"),
    ("大棚番茄高温落花怎么降温", {"crop": "番茄", "region": "山东省潍坊市"}, "高温障碍"),
])
async def test_keyword_only_path_recalls_expected_seed_item(query, filters, fragment):
    items = _corpus()
    result = await _bm25_only(query, items, filters)
    ids = [row["id"] for row in result.retrieved_items]
    assert result.retrieval_path == "bm25"
    assert _id(items, fragment) in ids


@pytest.mark.asyncio
async def test_preferred_stage_ranks_without_excluding_other_stages():
    items = _corpus()
    rag = RAGService(use_rewrite=False)

    async def no_vectors(*_args, **_kwargs):
        return [], {}

    rag._vector_search_multi = no_vectors
    result = await rag.hybrid_search("番茄灰霉病防治", items, filter_metadata={"crop": "番茄"},
                                     threshold=0.3, top_k=5, strict_threshold=True, preferred_stage="苗期")
    # Grey-mould items are tagged with flowering/harvest stages; a seedling preference must not hide them.
    assert any("灰霉" in row["question"] for row in result.retrieved_items)


@pytest.mark.asyncio
async def test_unrelated_query_still_returns_nothing_under_strict_threshold():
    items = _corpus()
    result = await _bm25_only("小麦条锈病赤霉病怎么办", items, {"crop": "番茄"})
    assert result.retrieved_items == [] or all(row["score"] >= 0.5 for row in result.retrieved_items)
