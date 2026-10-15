# -*- coding: utf-8 -*-
"""用户角色与权限矩阵 —— 单一事实来源（P2-ROLES）。

角色仍是模型中既有的四个（`models/user.py`：admin / expert / cs / customer）。
本模块**不新增角色、不改 schema、不改任何一处既有授权集合**，只把散落在各
API 里的内联 ``user.role in (...)`` 判断收敛到一处，避免同一个集合在多处各写
一遍、改一处漏一处。

能力集合（与收敛前的内联集合逐个对齐）：
- CHAT_ROLES  = {admin, cs}              会话 / 客服侧
- AGRI_ROLES  = {admin, expert}          农业侧
- STAFF_ROLES = CHAT_ROLES | AGRI_ROLES  任一后台角色
- ADMIN_ROLES = {admin}
"""
from enum import Enum
from typing import FrozenSet, Iterable, Optional


class Role(str, Enum):
    ADMIN = "admin"
    EXPERT = "expert"
    CS = "cs"
    CUSTOMER = "customer"


CHAT_ROLES: FrozenSet[Role] = frozenset({Role.ADMIN, Role.CS})
AGRI_ROLES: FrozenSet[Role] = frozenset({Role.ADMIN, Role.EXPERT})
STAFF_ROLES: FrozenSet[Role] = CHAT_ROLES | AGRI_ROLES
ADMIN_ROLES: FrozenSet[Role] = frozenset({Role.ADMIN})

CHAT_VALUES: FrozenSet[str] = frozenset(r.value for r in CHAT_ROLES)
AGRI_VALUES: FrozenSet[str] = frozenset(r.value for r in AGRI_ROLES)
STAFF_VALUES: FrozenSet[str] = frozenset(r.value for r in STAFF_ROLES)
ADMIN_VALUES: FrozenSet[str] = frozenset(r.value for r in ADMIN_ROLES)


def role_value(user) -> Optional[str]:
    """用户的角色字符串；无用户 / 无角色时为 None。"""
    return getattr(user, "role", None) if user is not None else None


def _within(user, allowed: Iterable[str]) -> bool:
    role = role_value(user)
    return role is not None and role in allowed


def is_admin(user) -> bool:
    """仅 admin。"""
    return _within(user, ADMIN_VALUES)


def is_chat_staff(user) -> bool:
    """会话 / 客服侧：admin 与 cs。"""
    return _within(user, CHAT_VALUES)


def is_agri_staff(user) -> bool:
    """农业侧：admin 与 expert。"""
    return _within(user, AGRI_VALUES)


def is_staff(user) -> bool:
    """任一后台角色（会话侧 ∪ 农业侧）。"""
    return _within(user, STAFF_VALUES)
