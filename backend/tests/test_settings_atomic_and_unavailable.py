"""Real persisted cipher errors cannot become defaults or partial updates."""
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.settings import SystemSettings
from app.services import settings_service as module
from app.services import vector_service as vectors


@pytest.mark.asyncio
@pytest.mark.parametrize("key,field,getter", [
    ("llm_config", "api_key", "get_llm_config"),
    ("embedding_config", "api_key", "get_embedding_config"),
    ("agri_ai_config", "vision_api_key", "get_agri_ai_config"),
])
async def test_real_getters_propagate_unavailable_cipher(test_session, key, field, getter):
    await module.SettingsService.set_setting(test_session, key, json.dumps({field: "v3:invalid-cipher"}))
    with pytest.raises(module.SettingsCipherUnavailable):
        await getattr(module.SettingsService, getter)(test_session)


@pytest.mark.asyncio
async def test_real_bad_embedding_cipher_never_uses_llm_or_env_fallback(test_session, monkeypatch):
    from app.core.embedding import EmbeddingClient
    client = object.__new__(EmbeddingClient)
    client._initialized = False
    EmbeddingClient.__init__(client)
    client.configure(api_key="synthetic-old-key", model="old-model")
    monkeypatch.setattr(vectors, "_embedding_client", client)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "synthetic-env-fallback")
    await module.SettingsService.set_setting(test_session, "embedding_config", json.dumps({"api_key": "v3:invalid-cipher"}))
    with pytest.raises(module.SettingsCipherUnavailable):
        await module.SettingsService.get_embedding_config_with_env(test_session)
    service = vectors.VectorService()
    assert await service.sync_config_from_db(test_session) is False
    assert client._override_api_key == "" and client._config_sync_failed
    assert await service.embed_text("must-not-call-provider") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["url", "cipher", "database"])
async def test_failed_later_section_never_commits_earlier_section(test_session, monkeypatch, failure):
    service = module.SettingsService
    await service.set_llm_config(test_session, module.LLMConfig(model="original"))
    before = await service.get_setting(test_session, service.KEY_LLM_CONFIG)
    if failure == "url":
        embedding = module.EmbeddingConfig(api_url="http://127.0.0.1/private")
        exception = ValueError
    elif failure == "cipher":
        monkeypatch.setattr(settings, "SETTINGS_ENCRYPTION_KEY", "")
        embedding = module.EmbeddingConfig(api_key="synthetic-only")
        exception = module.SettingsEncryptionError
    else:
        embedding = module.EmbeddingConfig()
        original = service.set_setting

        async def fail_late(db, key, value, *, commit=True):
            if key == service.KEY_EMBEDDING_CONFIG:
                raise RuntimeError("synthetic second-section DB failure")
            return await original(db, key, value, commit=commit)

        monkeypatch.setattr(service, "set_setting", fail_late)
        exception = RuntimeError
    with pytest.raises(exception):
        await service.update_settings(test_session,
            llm_config=module.LLMConfig(model="must-not-commit"), embedding_config=embedding)
    assert await service.get_setting(test_session, service.KEY_LLM_CONFIG) == before
    assert await service.get_setting(test_session, service.KEY_EMBEDDING_CONFIG) is None


@pytest.mark.asyncio
async def test_valid_update_publishes_all_sections_together(test_session, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_ENCRYPTION_KEY", "synthetic-independent-key-0123456789")
    await module.SettingsService.update_settings(test_session,
        llm_config=module.LLMConfig(api_key="synthetic-llm", model="new"),
        embedding_config=module.EmbeddingConfig(api_key="synthetic-embedding"))
    rows = (await test_session.scalars(select(SystemSettings))).all()
    assert len(rows) == 2
    assert all(json.loads(row.value)["api_key"].startswith("v3:") for row in rows)
    assert (await module.SettingsService.get_llm_config(test_session)).model == "new"
