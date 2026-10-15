"""Security tests must exercise production FK and explicit DNS behavior."""
import socket

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.farm import Farm
from app.utils.security import validate_safe_external_url


@pytest.mark.asyncio
async def test_fixture_database_rejects_missing_foreign_parent(test_session):
    assert await test_session.scalar(text("PRAGMA foreign_keys")) == 1
    test_session.add(Farm(name="Invalid parent", owner_id=987654))
    with pytest.raises(IntegrityError):
        await test_session.flush()
    await test_session.rollback()
    assert await test_session.scalar(text("SELECT count(*) FROM farms")) == 0


@pytest.mark.parametrize("addresses,expected", [
    (["93.184.216.34"], True),
    (["2606:4700:4700::1111"], True),
    (["10.0.0.1"], False),
    (["127.0.0.1"], False),
    (["169.254.169.254"], False),
    (["240.0.0.1"], False),
    (["224.0.0.1"], False),
    (["::1"], False),
    (["fd00::1"], False),
    (["fe80::1"], False),
    (["::ffff:127.0.0.1"], False),
    (["93.184.216.34", "10.0.0.1"], False),
    (["10.0.0.1", "93.184.216.34"], False),
    (["2606:4700:4700::1111", "fd00::1"], False),
    (["93.184.216.34", "2606:4700:4700::1111"], True),
])
def test_ssrf_evaluates_every_controlled_dns_answer(controlled_dns, addresses, expected):
    queries = controlled_dns({"provider.example.test": addresses})
    safe, reason = validate_safe_external_url("https://provider.example.test/v1")
    assert safe is expected
    assert queries == ["provider.example.test"]
    if not expected:
        assert reason


def test_ssrf_rejects_dns_failure_without_network_fallback(controlled_dns):
    queries = controlled_dns({"provider.example.test": socket.gaierror(socket.EAI_NONAME, "offline test")})
    safe, reason = validate_safe_external_url("https://provider.example.test/v1")
    assert not safe
    assert "无法解析" in reason
    assert queries == ["provider.example.test"]


def test_unconfigured_dns_host_is_not_resolved_on_the_network(controlled_dns):
    queries = controlled_dns({})
    safe, _ = validate_safe_external_url("https://unconfigured.example.test/v1")
    assert not safe
    assert queries == ["unconfigured.example.test"]
