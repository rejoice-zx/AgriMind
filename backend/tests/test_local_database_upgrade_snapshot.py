"""Upgrade and restore a copy of the observed local SQLite file, never the original."""
from pathlib import Path
import hashlib
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from app import database
from app.database import create_database_engine, get_db
from app.config import settings
from app.api.dependencies import get_current_user
from app.api.statistics import router as statistics_router
from app.api.agri_history import router as history_router
from app.models.user import User


def readonly(path): return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def knowledge_digest(connection):
    columns = [row[1] for row in connection.execute('PRAGMA table_info(knowledge_items)')]
    rows = connection.execute('SELECT * FROM knowledge_items ORDER BY id').fetchall()
    return hashlib.sha256(json.dumps([columns, rows], ensure_ascii=True, default=str).encode()).hexdigest(), len(rows)


@pytest.mark.asyncio
async def test_local_database_copy_upgrade_and_restore(tmp_path, monkeypatch):
    import os
    if os.environ.get('ALLOW_LOCAL_DATABASE_SNAPSHOT_TEST') != 'true':
        pytest.skip('Explicit opt-in required before reading local business data')
    original = Path(__file__).resolve().parents[2] / 'data' / 'app.db'
    if not original.is_file(): pytest.skip('No observed local SQLite file; this is an explicit local-copy check')
    before_file_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    copied = tmp_path / 'upgrade-copy.db'
    restored = tmp_path / 'restored-copy.db'
    with readonly(original) as source, sqlite3.connect(copied) as target:
        source.backup(target)
        before_knowledge = knowledge_digest(source)
    with readonly(copied) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        old_schema = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    engine = create_database_engine('sqlite+aiosqlite:///' + copied.as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'AsyncSessionLocal', sessions)
    monkeypatch.setattr(settings, 'DATABASE_URL', 'sqlite+aiosqlite:///' + copied.as_posix())
    monkeypatch.setenv('ALLOW_DEMO_SEEDING', 'false')
    try:
        await database.init_db()
        await database.init_db()  # Current schema upgrade remains idempotent.
        with readonly(copied) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert old_schema <= tables
            assert {'agri_conversations', 'agri_turns', 'agri_consultations', 'diagnosis_media', 'diagnosis_requests', 'disease_reviews'} <= tables
            assert knowledge_digest(connection) == before_knowledge
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        app = FastAPI(); app.include_router(statistics_router); app.include_router(history_router)
        async def db():
            async with sessions() as session: yield session
        async def admin(): return User(id=-999, username='synthetic-local-admin', password_hash='unused', role='admin', is_active=True)
        app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = admin
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local') as client:
            assert (await client.get('/api/statistics/snapshot?days=1')).status_code == 200
            assert (await client.get('/api/agent/conversations')).status_code == 200
    finally:
        await engine.dispose()
    with readonly(copied) as source, sqlite3.connect(restored) as target: source.backup(target)
    with readonly(restored) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert knowledge_digest(connection) == before_knowledge
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before_file_hash
