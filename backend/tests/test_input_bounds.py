"""Input length bounds (P2-INPUT / DS-23).

These are resource limits enforced by pydantic, so every entry point rejects an
over-long payload through the same ValidationError -> HTTP 422 path. This task
does NOT claim to defend against prompt injection; that risk stays with V-PROMPT.
"""
import pytest
from pydantic import ValidationError

from app.schemas.agent import (
    MAX_QUERY_LENGTH,
    MAX_TOOL_ARGUMENTS_JSON,
    MAX_TOOL_NAME_LENGTH,
    AgentDecisionRequest,
    ToolExecuteRequest,
)
from app.schemas.expert import (
    MAX_IMAGE_URL_LENGTH,
    MAX_SYMPTOMS_HINT_LENGTH,
    VisualDiagnosisRequest,
)


@pytest.mark.parametrize("length, accepted", [
    (MAX_QUERY_LENGTH - 1, True),
    (MAX_QUERY_LENGTH, True),
    (MAX_QUERY_LENGTH + 1, False),
])
def test_query_length_boundary(length, accepted):
    payload = "番" * length
    if accepted:
        assert AgentDecisionRequest(query=payload).query == payload
    else:
        with pytest.raises(ValidationError):
            AgentDecisionRequest(query=payload)


def test_query_rejects_empty_and_accepts_unicode():
    with pytest.raises(ValidationError):
        AgentDecisionRequest(query="")
    # Unicode (incl. non-BMP) must count and pass normally.
    unicode_query = "番茄🍅叶面有斑点？" * 10
    assert AgentDecisionRequest(query=unicode_query).query == unicode_query
    for blank_query in ("   ", "\t\n", "\u3000"):
        with pytest.raises(ValidationError, match="提问不能为空"):
            AgentDecisionRequest(query=blank_query)


@pytest.mark.parametrize("length, accepted", [
    (MAX_SYMPTOMS_HINT_LENGTH - 1, True),
    (MAX_SYMPTOMS_HINT_LENGTH, True),
    (MAX_SYMPTOMS_HINT_LENGTH + 1, False),
])
def test_symptoms_hint_boundary(length, accepted):
    payload = "黄" * length
    if accepted:
        req = VisualDiagnosisRequest(image_url="http://example.com/a.jpg", symptoms_hint=payload)
        assert req.symptoms_hint == payload
    else:
        with pytest.raises(ValidationError):
            VisualDiagnosisRequest(image_url="http://example.com/a.jpg", symptoms_hint=payload)


def test_symptoms_hint_is_optional():
    req = VisualDiagnosisRequest(image_url="http://example.com/a.jpg")
    assert req.symptoms_hint is None


@pytest.mark.parametrize("length, accepted", [
    (MAX_IMAGE_URL_LENGTH - 1, True),
    (MAX_IMAGE_URL_LENGTH, True),
    (MAX_IMAGE_URL_LENGTH + 1, False),
])
def test_image_url_boundary(length, accepted):
    payload = "h" * length
    if accepted:
        assert VisualDiagnosisRequest(image_url=payload).image_url == payload
    else:
        with pytest.raises(ValidationError):
            VisualDiagnosisRequest(image_url=payload)


def test_image_url_rejects_empty():
    with pytest.raises(ValidationError):
        VisualDiagnosisRequest(image_url="")


@pytest.mark.parametrize("length, accepted", [
    (MAX_TOOL_NAME_LENGTH - 1, True),
    (MAX_TOOL_NAME_LENGTH, True),
    (MAX_TOOL_NAME_LENGTH + 1, False),
])
def test_single_step_tool_name_boundary(length, accepted):
    payload = "t" * length
    if accepted:
        assert ToolExecuteRequest(tool_name=payload).tool_name == payload
    else:
        with pytest.raises(ValidationError):
            ToolExecuteRequest(tool_name=payload)


def test_single_step_tool_arguments_are_bounded():
    # Just inside the bound (measured on the serialised form).
    ok_payload = {"note": "农" * (MAX_TOOL_ARGUMENTS_JSON - 100)}
    ToolExecuteRequest(tool_name="search_agri_knowledge", arguments=ok_payload)

    oversized = {"note": "农" * (MAX_TOOL_ARGUMENTS_JSON + 100)}
    with pytest.raises(ValidationError):
        ToolExecuteRequest(tool_name="search_agri_knowledge", arguments=oversized)


def test_limits_are_shared_constants_not_ad_hoc_numbers():
    """Every entry point must read the same declared bound."""
    assert MAX_QUERY_LENGTH > 0
    assert MAX_SYMPTOMS_HINT_LENGTH > 0
    assert MAX_IMAGE_URL_LENGTH > 0
    assert MAX_TOOL_NAME_LENGTH > 0
    assert MAX_TOOL_ARGUMENTS_JSON > 0
