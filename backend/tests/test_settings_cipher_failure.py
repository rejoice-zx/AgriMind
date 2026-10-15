"""Config-encryption failure handling and migratable key (DS-14 / NEW-04).

Encryption must never fall back to plaintext; decryption must never hand the ciphertext
back as if it were the secret; and an independent SETTINGS_ENCRYPTION_KEY with a `v3:`
ciphertext version keeps legacy (JWT-derived) ciphertexts readable.
"""
import pytest
from cryptography.fernet import Fernet

from app.config import settings as app_settings
from app.services import settings_service as svc


@pytest.fixture(autouse=True)
def independent_cipher_key(monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "test-only-independent-key-0123456789")


def test_encrypt_round_trips_with_v3_prefix():
    token = svc._encrypt_value("sk-secret-value")
    assert token.startswith("v3:")
    assert svc._decrypt_value(token) == "sk-secret-value"


def test_empty_values_pass_through():
    assert svc._encrypt_value("") == ""
    assert svc._decrypt_value("") == ""


def test_encrypt_failure_raises_instead_of_returning_plaintext(monkeypatch):
    def boom(legacy=False):
        raise RuntimeError("kdf unavailable")

    monkeypatch.setattr(svc, "_get_encryption_key", boom)
    with pytest.raises(svc.SettingsEncryptionError):
        svc._encrypt_value("sk-must-not-leak")


def test_decrypt_failure_raises_instead_of_returning_ciphertext():
    forged = "v2:" + "gAAAAAB" + "x" * 80   # well-formed-looking but undecryptable
    with pytest.raises(svc.SettingsCipherUnavailable):
        svc._decrypt_value(forged)


def test_independent_key_decouples_from_jwt_secret(monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "independent-key-01234567890123456789")
    token = svc._encrypt_value("sk-decoupled")
    assert token.startswith("v3:")
    # Rotating the JWT secret must not break independent ciphertext.
    monkeypatch.setattr(app_settings, "JWT_SECRET_KEY", "a-completely-different-jwt-secret")
    assert svc._decrypt_value(token) == "sk-decoupled"


def test_legacy_v1_ciphertext_still_readable(monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "")
    legacy_v1 = Fernet(svc._get_encryption_key(legacy=True)).encrypt(b"sk-legacy").decode()
    assert svc._decrypt_value(legacy_v1) == "sk-legacy"


def test_new_writes_require_independent_key(monkeypatch):
    monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "")
    with pytest.raises(svc.SettingsEncryptionError):
        svc._encrypt_value("synthetic-input")


@pytest.mark.parametrize("legacy", [False, True])
def test_historical_v2_reads_then_upgrades(legacy, monkeypatch):
    old = "v2:" + Fernet(svc._get_encryption_key(legacy=legacy)).encrypt(b"historical").decode()
    if legacy:
        monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "new-independent-key-0123456789abcd")
    assert svc._decrypt_value(old) == "historical"
    upgraded = svc._encrypt_value(svc._decrypt_value(old))
    assert upgraded.startswith("v3:")
    monkeypatch.setattr(app_settings, "JWT_SECRET_KEY", "a-different-test-jwt-key-0123456789")
    assert svc._decrypt_value(upgraded) == "historical"


def test_v3_never_falls_back_to_jwt(monkeypatch):
    forged = "v3:" + Fernet(svc._get_encryption_key(legacy=True)).encrypt(b"historical").decode()
    with pytest.raises(svc.SettingsCipherUnavailable):
        svc._decrypt_value(forged)


def test_independent_key_rotation_is_not_silently_accepted(monkeypatch):
    ciphertext = svc._encrypt_value("synthetic-input")
    monkeypatch.setattr(app_settings, "SETTINGS_ENCRYPTION_KEY", "different-independent-key-0123456789")
    with pytest.raises(svc.SettingsCipherUnavailable):
        svc._decrypt_value(ciphertext)
