"""Embedding external-call URL is validated on save and before use (NEW-05).

The same SSRF guard used for LLM/vision must cover the embedding config path, including
values inherited from the LLM config or left over in the DB. A rejection happens before
any HTTP request is made. DNS is isolated via the `controlled_dns` fixture.
"""
import pytest

from app.core.embedding import EmbeddingClient
from app.services.settings_service import SettingsService, EmbeddingConfig


@pytest.mark.asyncio
async def test_set_embedding_config_rejects_unsafe_url(test_session):
    with pytest.raises(ValueError):
        await SettingsService.set_embedding_config(
            test_session, EmbeddingConfig(api_url="http://127.0.0.1:8000/v1"))


@pytest.mark.asyncio
async def test_set_embedding_config_accepts_public_url(test_session, controlled_dns):
    controlled_dns({"api.example.com": ["93.184.216.34"]})
    await SettingsService.set_embedding_config(
        test_session, EmbeddingConfig(api_url="https://api.example.com/v1"))
    loaded = await SettingsService.get_embedding_config(test_session)
    assert loaded.api_url == "https://api.example.com/v1"


def test_embedding_use_side_rejects_unsafe_url_before_any_http(monkeypatch):
    client = EmbeddingClient()
    monkeypatch.setattr(client, "_override_api_url",
                        "http://169.254.169.254/latest/meta-data/", raising=False)
    calls = {"post": 0}

    def fail_post(*args, **kwargs):
        calls["post"] += 1
        raise AssertionError("HTTP must not be attempted for an unsafe URL")

    monkeypatch.setattr("app.core.embedding.requests.post", fail_post)
    assert client._embed_batch(["hello"]) is None
    assert calls["post"] == 0
