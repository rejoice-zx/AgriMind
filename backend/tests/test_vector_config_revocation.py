"""Resolved empty settings and unreadable settings revoke stale provider state."""
from types import SimpleNamespace

import pytest

from app.core.embedding import EmbeddingClient
from app.services import vector_service as module
from app.services.vector_service import VectorService, EmbeddingService
from app.services.settings_service import SettingsService, SettingsCipherUnavailable


@pytest.fixture
def cached_provider(monkeypatch):
    client = object.__new__(EmbeddingClient)
    client._override_api_key = "synthetic-old-provider"
    client._override_api_url = "https://old.mock.invalid/v1"
    client._override_model = "old-model"
    client._override_dimension = 2
    client._dimension = 2
    # A fallback exists to prove that read/decrypt failure cannot activate it.
    client.config = SimpleNamespace(get=lambda key, default=None:
                                    "synthetic-fallback" if key == "api_key" else default)
    monkeypatch.setattr(module, "_embedding_client", client)
    cache = {"cached": [1.0, 0.0]}
    monkeypatch.setattr(VectorService, "_query_embed_cache", cache)
    service = VectorService()
    service._config_synced = True
    calls = []
    def unexpected(*args):
        calls.append(args)
        raise AssertionError("No provider call after configuration revocation")
    monkeypatch.setattr(client, "embed_text", unexpected)
    monkeypatch.setattr(client, "embed_texts", unexpected)
    return client, service, cache, calls


@pytest.mark.asyncio
async def test_resolved_empty_config_clears_old_overrides(cached_provider, monkeypatch):
    client, service, cache, calls = cached_provider
    async def empty(_db):
        return SimpleNamespace(api_key="", api_url="", model="", dimension=2)
    async def no_llm(_db):
        return SimpleNamespace(api_key="", api_url="")
    monkeypatch.setattr(SettingsService, "get_embedding_config_with_env", empty)
    monkeypatch.setattr(SettingsService, "get_llm_config", no_llm)
    assert await service.sync_config_from_db(None) is False
    assert client._override_api_key == "" and client._override_api_url == ""
    assert client._override_model == "" and cache == {}
    assert service._config_synced is False
    assert service.is_embedding_available() is False
    assert await service.embed_text("cached") is None
    assert await service.embed_texts(["fresh"]) is None
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [SettingsCipherUnavailable("synthetic unreadable ciphertext"),
                                     RuntimeError("synthetic DB read failure")])
async def test_read_or_decrypt_failure_revokes_credentials_and_cache(cached_provider,
                                                                     monkeypatch, failure):
    client, service, cache, calls = cached_provider
    async def unavailable(_db):
        raise failure
    monkeypatch.setattr(SettingsService, "get_embedding_config_with_env", unavailable)
    assert await service.sync_config_from_db(None) is False
    assert client._override_api_key == "" and client._override_api_url == ""
    assert client._override_model == "" and client._override_dimension is None
    assert service._config_synced is False and client._config_sync_failed is True
    assert cache == {} and service.is_embedding_available() is False
    assert EmbeddingService().is_available() is False
    assert await service.embed_text("cached") is None
    assert await EmbeddingService().embed_texts(["fresh"]) is None
    assert not calls


@pytest.mark.asyncio
async def test_subsequent_valid_sync_recovers_provider(cached_provider, monkeypatch):
    client, service, cache, _ = cached_provider
    async def unavailable(_db):
        raise SettingsCipherUnavailable("synthetic unreadable ciphertext")
    monkeypatch.setattr(SettingsService, "get_embedding_config_with_env", unavailable)
    assert await service.sync_config_from_db(None) is False
    async def recovered(_db):
        return SimpleNamespace(api_key="synthetic-new-provider", api_url="https://new.mock.invalid/v1",
                               model="new-model", dimension=3)
    monkeypatch.setattr(SettingsService, "get_embedding_config_with_env", recovered)
    assert await service.sync_config_from_db(None) is True
    assert client._config_sync_failed is False and service._config_synced is True
    assert client._override_api_key == "synthetic-new-provider"
    assert client._override_api_url == "https://new.mock.invalid/v1"
    assert client._override_model == "new-model" and client.dimension == 3
    assert service.is_embedding_available() is True and cache == {}

