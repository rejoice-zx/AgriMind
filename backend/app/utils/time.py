# -*- coding: utf-8 -*-
"""公共时间工具函数"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, TypeDecorator


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区信息）"""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLAlchemy TypeDecorator: 确保从数据库读取的 datetime 始终带 UTC 时区。

    SQLite 存储 datetime 时会丢失 tzinfo，导致 ORM 返回 naive datetime，
    Pydantic 序列化时不带 ``Z`` 后缀，前端 ``new Date()`` 会按本地时间解析，
    产生时区偏移。此装饰器在 ``process_result_value`` 阶段自动补回 UTC tzinfo。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # 写入时保持原样（SQLite 会存为文本）
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
