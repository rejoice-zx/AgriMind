"""Small numbered-upgrade/copy checks; never opens deployment config or business DBs."""
import json
import sqlite3
import pytest
from sqlalchemy import create_engine, MetaData
from app.core.model_base import Base
import app.models
from app.core.recovery_bundle import create_bundle, prepare_restore, file_digest
from app.core.schema_revision import schema_fingerprint, guard_schema_revision, CURRENT_SCHEMA_REVISION
from app.core.copy_upgrade import upgrade_restore_copy
from app.migrations import schema_upgrade
from app.migrations import schema_upgrade_v6


def metadata_v5():
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name != 'agri_consultation_reviews': table.to_metadata(metadata)
    return metadata


def test_legacy_step_rollback_and_verified_resume_preserve_account(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    state = {'fail': True}
    original = schema_upgrade.migrate_issue2
    def injected_failure(connection):
        original(connection)
        if state['fail']:
            connection.exec_driver_sql('CREATE TABLE synthetic_failed_ddl(id INTEGER)')
            raise RuntimeError('synthetic interruption')
    steps = list(schema_upgrade.STEPS); steps[2] = (steps[2][0], injected_failure)
    monkeypatch.setattr(schema_upgrade, 'STEPS', tuple(steps))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY,username VARCHAR(50) NOT NULL,password_hash VARCHAR(128) NOT NULL,'
                'role VARCHAR(20),display_name VARCHAR(100),email VARCHAR(100),is_active BOOLEAN,created_at DATETIME,last_login DATETIME)')
            connection.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(7,'synthetic-legacy','synthetic','customer',1)")
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match='interruption'):
                schema_upgrade.run_schema_upgrade(connection, metadata_v5())
            assert connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='synthetic_failed_ddl'").first() is None
            assert connection.exec_driver_sql('SELECT count(*) FROM agri_schema_migration_steps').scalar() == 2
            subject = connection.exec_driver_sql('SELECT auth_subject FROM users WHERE id=7').scalar_one()
            with pytest.raises(ValueError, match='未完成'): guard_schema_revision(connection)
            connection.commit(); state['fail'] = False
            result = schema_upgrade.run_schema_upgrade(connection, metadata_v5())
            assert result['steps'] == 9
            assert connection.exec_driver_sql('SELECT auth_subject FROM users WHERE id=7').scalar_one() == subject
            assert connection.exec_driver_sql('SELECT count(*) FROM users').scalar() == 1
            assert guard_schema_revision(connection) == 5
            connection.commit()
            assert schema_upgrade.run_schema_upgrade(connection, metadata_v5())['replayed']
    finally: engine.dispose()


def test_verified_v4_restore_copy_upgrades_without_touching_original(tmp_path):
    source, bundle, target = tmp_path / 'source.db', tmp_path / 'backup.zip', tmp_path / 'restore'
    engine = create_engine('sqlite:///' + source.as_posix())
    try:
        with engine.begin() as connection:
            metadata_v5().create_all(connection)
            connection.exec_driver_sql("INSERT INTO users(id,auth_subject,username,password_hash,role,is_active) VALUES(7,'synthetic-subject','synthetic-source','synthetic','customer',1)")
            connection.exec_driver_sql('CREATE TABLE agri_schema_revisions(version INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL,applied_at TEXT NOT NULL)')
            connection.exec_driver_sql('INSERT INTO agri_schema_revisions VALUES(4,?,?)', (schema_fingerprint(connection), 'synthetic-time'))
            connection.exec_driver_sql('PRAGMA user_version=4')
    finally: engine.dispose()
    original_hash = file_digest(source)
    create_bundle(source, tmp_path, bundle, app_version='synthetic')
    prepare_restore(bundle, target)
    result = upgrade_restore_copy(target)
    assert result['schema_revision'] == CURRENT_SCHEMA_REVISION == 6 and result['steps'] == 10
    assert file_digest(source) == original_hash
    with sqlite3.connect(target / 'app.db') as connection:
        assert connection.execute('SELECT auth_subject FROM users WHERE id=7').fetchone()[0] == 'synthetic-subject'
        assert connection.execute('SELECT count(*) FROM agri_schema_migration_steps').fetchone()[0] == 10
    assert json.loads((target / 'UPGRADED.json').read_text(encoding='utf-8'))['database_sha256'] == file_digest(target / 'app.db')
    assert upgrade_restore_copy(target)['replayed']


def test_drift_and_future_revision_fail_before_any_upgrade_mutation():
    for mode in ('drift', 'future'):
        engine = create_engine('sqlite:///:memory:')
        try:
            with engine.begin() as connection:
                Base.metadata.create_all(connection)
                connection.exec_driver_sql('CREATE TABLE agri_schema_revisions(version INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL,applied_at TEXT NOT NULL)')
                connection.exec_driver_sql('INSERT INTO agri_schema_revisions VALUES(4,?,?)', (schema_fingerprint(connection), 'synthetic'))
                connection.exec_driver_sql('PRAGMA user_version=' + str(99 if mode == 'future' else 4))
                if mode == 'drift': connection.exec_driver_sql('CREATE TABLE synthetic_drift(id INTEGER)')
            with engine.connect() as connection:
                before = schema_fingerprint(connection); connection.commit()
                with pytest.raises(ValueError): schema_upgrade_v6.run_schema_upgrade(connection, Base.metadata)
                assert schema_fingerprint(connection) == before
                assert connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='agri_schema_migrations'").first() is None
        finally: engine.dispose()


def test_v5_signed_review_upgrade_rolls_back_addition_and_retries_without_rewriting_history(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    original = schema_upgrade_v6.install_review_schema
    def interrupted(connection, metadata):
        original(connection, metadata)
        raise RuntimeError('synthetic revision6 interruption')
    try:
        with engine.connect() as connection:
            schema_upgrade.run_schema_upgrade(connection, metadata_v5())
            connection.execute(Base.metadata.tables['agri_conversations'].insert().values(id='synthetic-conversation', title='synthetic'))
            connection.execute(Base.metadata.tables['agri_turns'].insert().values(id='synthetic-turn', conversation_id='synthetic-conversation',
                request_id='synthetic-request', request_payload={}, query='synthetic', status='completed'))
            connection.execute(Base.metadata.tables['agri_consultations'].insert().values(id='synthetic-ticket', turn_id='synthetic-turn', status='resolved', answer='synthetic old answer'))
            connection.commit()
            before = schema_fingerprint(connection); connection.commit()
            monkeypatch.setattr(schema_upgrade_v6, 'install_review_schema', interrupted)
            with pytest.raises(RuntimeError, match='revision6 interruption'):
                schema_upgrade_v6.run_schema_upgrade(connection, Base.metadata)
            assert schema_fingerprint(connection) == before
            assert guard_schema_revision(connection) == 5
            assert connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='agri_consultation_reviews'").first() is None
            connection.commit()
            monkeypatch.setattr(schema_upgrade_v6, 'install_review_schema', original)
            result = schema_upgrade_v6.run_schema_upgrade(connection, Base.metadata)
            assert result['schema_revision'] == 6
            assert connection.exec_driver_sql('SELECT answer FROM agri_consultations').scalar() == 'synthetic old answer'
            assert connection.exec_driver_sql('SELECT count(*) FROM agri_consultation_reviews').scalar() == 0
            connection.commit()
            assert schema_upgrade_v6.run_schema_upgrade(connection, Base.metadata)['replayed']
    finally: engine.dispose()
