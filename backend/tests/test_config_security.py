"""Config security tests."""

import pytest

from app.config import DEFAULT_JWT_SECRET, is_insecure_jwt_secret, is_production_environment


def test_is_insecure_jwt_secret_detects_default_and_empty():
    assert is_insecure_jwt_secret(DEFAULT_JWT_SECRET) is True
    assert is_insecure_jwt_secret("") is True
    assert is_insecure_jwt_secret("   ") is True
    assert is_insecure_jwt_secret("agrimind-dev-jwt-secret-key-2026") is True
    assert is_insecure_jwt_secret("short") is True
    assert is_insecure_jwt_secret("very-strong-secret-1234567890") is False


@pytest.mark.parametrize("env_key", ["APP_ENV", "ENV"])
@pytest.mark.parametrize("env_value", ["production", "prod", "PRODUCTION", " Prod "])
def test_is_production_environment_true(monkeypatch, env_key, env_value):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv(env_key, env_value)
    assert is_production_environment() is True


def test_is_production_environment_false(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENV", "")
    assert is_production_environment() is False
