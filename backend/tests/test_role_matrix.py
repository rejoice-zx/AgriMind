"""Role capability matrix (P2-ROLES).

Option A: the model's four roles (admin / expert / cs / customer) are unchanged
and no schema migration happened. These tests pin the capability sets so that
converging the scattered inline checks into app.core.roles cannot silently widen
or narrow anyone's access.
"""
from types import SimpleNamespace

import pytest

from app.core.roles import (
    ADMIN_VALUES,
    AGRI_VALUES,
    CHAT_VALUES,
    Role,
    STAFF_VALUES,
    is_admin,
    is_agri_staff,
    is_chat_staff,
    is_staff,
)


def _user(role):
    return SimpleNamespace(role=role)


def test_capability_sets_are_exactly_the_converged_sets():
    assert CHAT_VALUES == {"admin", "cs"}
    assert AGRI_VALUES == {"admin", "expert"}
    assert STAFF_VALUES == {"admin", "expert", "cs"}
    assert ADMIN_VALUES == {"admin"}
    # The two sides stay separate: neither contains the other's specialist.
    assert Role.EXPERT.value not in CHAT_VALUES
    assert Role.CS.value not in AGRI_VALUES


@pytest.mark.parametrize("role, chat, agri, staff, admin", [
    (Role.ADMIN.value, True, True, True, True),
    (Role.CS.value, True, False, True, False),
    (Role.EXPERT.value, False, True, True, False),
    (Role.CUSTOMER.value, False, False, False, False),
])
def test_matrix_matches_the_pre_convergence_behaviour(role, chat, agri, staff, admin):
    user = _user(role)
    assert is_chat_staff(user) is chat
    assert is_agri_staff(user) is agri
    assert is_staff(user) is staff
    assert is_admin(user) is admin


@pytest.mark.parametrize("fn", [is_admin, is_chat_staff, is_agri_staff, is_staff])
def test_missing_user_or_role_is_never_authorised(fn):
    assert fn(None) is False
    assert fn(SimpleNamespace()) is False


def test_unknown_role_has_no_capability():
    user = _user("superuser")
    assert not any([is_admin(user), is_chat_staff(user), is_agri_staff(user), is_staff(user)])


def test_dependency_instances_read_the_same_matrix():
    """AdminRequired / CSOrAdminRequired must not carry their own private sets."""
    from app.api.dependencies import AdminRequired, CSOrAdminRequired

    assert sorted(AdminRequired.allowed_roles) == sorted(ADMIN_VALUES)
    assert sorted(CSOrAdminRequired.allowed_roles) == sorted(CHAT_VALUES)
