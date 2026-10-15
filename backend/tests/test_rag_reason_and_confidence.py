"""RAG reason/confidence contract (P2-RAG-DATA / DS-09, DS-53).

- DS-09: `expert_reason` must be a real field of RAGSearchResult and must survive
  `to_dict()` (which feeds the SSE rag_trace / HTTP payloads), instead of living
  as a dynamic attribute that serialisation drops.
- DS-53: the no-matching-knowledge branch must set `confidence` — the field
  `to_dict()` reads — not a stray `confidence_score`.
"""
import pytest

from app.services.rag_service import RAGSearchResult, RAGService

TOMATO_KB = [
    {
        "id": "kb_tomato_1",
        "crop": "番茄",
        "title": "番茄晚疫病防治",
        "answer": "使用烯酰吗啉喷雾",
        "source_name": "GB/T 23416.2",
        "source_type": "test_fixture", "is_current": 1,
    },
    {
        "id": "kb_tomato_2",
        "crop": "番茄",
        "title": "番茄开花期水肥",
        "answer": "控制氮肥增施钾肥",
        "source_name": "NY/T 393",
        "source_type": "test_fixture", "is_current": 1,
    },
]


def test_to_dict_serialises_expert_reason_with_a_none_default():
    result = RAGSearchResult(query="q", confidence=0.42)
    payload = result.to_dict()
    assert "expert_reason" in payload
    assert payload["expert_reason"] is None
    assert payload["confidence"] == 0.42


@pytest.mark.asyncio
async def test_no_matching_knowledge_sets_the_real_confidence_field_and_a_reason():
    rag = RAGService()
    result = await rag.hybrid_search(
        query="火龙果溃疡病怎么防治？",
        knowledge_items=TOMATO_KB,
        filter_metadata={"crop": "火龙果"},
    )

    assert result.retrieved_items == []
    assert result.need_expert is True
    assert result.expert_reason and "火龙果" in result.expert_reason
    # DS-53: the serialised confidence must reflect the "no applicable knowledge" state.
    assert result.confidence == 0.0
    assert result.to_dict()["confidence"] == 0.0
    # DS-09: the reason reaches the serialised payload (SSE rag_trace / HTTP).
    assert "火龙果" in result.to_dict()["expert_reason"]
    # The stray write is gone: nothing may depend on this non-field name.
    assert not hasattr(result, "confidence_score")


@pytest.mark.asyncio
async def test_normal_result_keeps_payload_and_field_in_sync():
    rag = RAGService()
    result = await rag.hybrid_search(
        query="番茄晚疫病怎么防治？",
        knowledge_items=TOMATO_KB,
        filter_metadata={"crop": "番茄"},
    )

    payload = result.to_dict()
    assert payload["expert_reason"] == result.expert_reason
    assert payload["confidence"] == result.confidence
    assert not hasattr(result, "confidence_score")


@pytest.mark.asyncio
async def test_knowledge_past_the_200th_candidate_is_still_recalled():
    """201+ candidates: a uniquely matching tail item must still be retrievable."""
    rag = RAGService()
    # The retriever indexes `question`/`answer` (see rag_service._bm25_search), so the
    # fixture must use that shape for recall to be exercised at all.
    knowledge_items = [
        {
            "id": f"kb_bulk_{i}",
            "crop": "番茄",
            "question": f"番茄常规条目 {i}",
            "answer": "常规水肥管理与田间巡查",
            "source_name": "NY/T 393",
            "source_type": "test_fixture", "is_current": 1,
        }
        for i in range(200)
    ]
    knowledge_items.append({
        "id": "kb_tail_unique",
        "crop": "番茄",
        "question": "番茄脐腐病怎么补钙",
        "answer": "坐果期叶面喷施氯化钙可缓解脐腐",
        "source_name": "NY/T 393",
        "source_type": "test_fixture", "is_current": 1,
    })

    result = await rag.hybrid_search(
        query="番茄脐腐病怎么补钙",
        knowledge_items=knowledge_items,
        filter_metadata={"crop": "番茄"},
    )

    recalled = [item.get("id") for item in result.retrieved_items]
    assert "kb_tail_unique" in recalled, (
        f"an item past the 200th candidate must still be recalled, got {recalled}"
    )
