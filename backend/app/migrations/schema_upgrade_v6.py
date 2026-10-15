"""Revision 6 adds signed review history without changing the published v5 step plan."""
import hashlib
import inspect
from sqlalchemy import MetaData
from app.core.schema_revision import (schema_revision, schema_fingerprint, guard_schema_revision,
    record_schema_revision, verify_model_columns, CURRENT_SCHEMA_REVISION)
from app.migrations.schema_upgrade import run_schema_upgrade as upgrade_v5, _integrity


def install_review_schema(connection, metadata):
    metadata.tables['agri_consultation_reviews'].create(connection, checkfirst=True)
    connection.exec_driver_sql('CREATE TRIGGER IF NOT EXISTS protect_signed_consultation_review '
        'BEFORE UPDATE ON agri_consultation_reviews WHEN OLD.resolved_payload IS NOT NULL '
        "BEGIN SELECT RAISE(ABORT,'Signed consultation reviews are immutable'); END")
    connection.exec_driver_sql('CREATE TRIGGER IF NOT EXISTS protect_consultation_review_delete '
        'BEFORE DELETE ON agri_consultation_reviews '
        "BEGIN SELECT RAISE(ABORT,'Consultation review history is protected'); END")
    connection.exec_driver_sql('CREATE TRIGGER IF NOT EXISTS protect_resolved_consultation '
        "BEFORE UPDATE OF status,answer,turn_id,created_at ON agri_consultations WHEN OLD.status='resolved' "
        "BEGIN SELECT RAISE(ABORT,'Resolved consultations are immutable'); END")


def run_schema_upgrade(connection, metadata):
    if connection.dialect.name != 'sqlite' or connection.in_transaction():
        raise ValueError('编号升级要求独立SQLite连接')
    if CURRENT_SCHEMA_REVISION != 6: raise ValueError('迁移计划与当前版本不匹配')
    version = schema_revision(connection)
    journal_exists = connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='agri_schema_migrations' AND type='table'").first()
    pending = connection.exec_driver_sql('SELECT * FROM agri_schema_migrations WHERE target_version=6').mappings().first() if journal_exists else None
    connection.commit()
    if version < 5 or (version == 5 and pending is None):
        legacy = MetaData()
        for table in metadata.sorted_tables:
            if table.name != 'agri_consultation_reviews': table.to_metadata(legacy)
        upgrade_v5(connection, legacy)
    digest = hashlib.sha256(('revision6-plan1\n' + inspect.getsource(install_review_schema)).encode()).hexdigest()
    try:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        version = schema_revision(connection)
        if version > 6: raise ValueError('数据库版本高于当前程序，拒绝降级')
        row = connection.exec_driver_sql('SELECT * FROM agri_schema_migrations WHERE target_version=6').mappings().first()
        if version == 6:
            guard_schema_revision(connection); verify_model_columns(connection, metadata); _integrity(connection)
            if not row or row['state'] != 'completed' or row['plan_digest'] != digest:
                raise ValueError('版本6缺少匹配的完成账本')
            connection.commit()
            return {'schema_revision': 6, 'steps': 10, 'replayed': True}
        if version != 5: raise ValueError('版本6必须从完成的版本5升级')
        if row:
            if row['source_version'] != 5 or row['plan_digest'] != digest or row['fingerprint'] != schema_fingerprint(connection) or row['state'] not in {'running', 'failed'}:
                raise ValueError('版本6升级计划或结构进度已变化')
            source = connection.exec_driver_sql('SELECT fingerprint FROM agri_schema_revisions WHERE version=5').scalar_one()
            if source != row['source_fingerprint']: raise ValueError('原版本5账本已变化')
        else:
            guard_schema_revision(connection)
            fingerprint = schema_fingerprint(connection)
            connection.exec_driver_sql('INSERT INTO agri_schema_migrations '
                '(target_version,source_version,source_fingerprint,plan_digest,fingerprint,state,started_at) '
                "VALUES(6,5,?,?,?,'running',strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (fingerprint, digest, fingerprint))
        # One additive transaction: table, constraints, full model/FK verification and stamp.
        install_review_schema(connection, metadata)
        verify_model_columns(connection, metadata); _integrity(connection)
        fingerprint = schema_fingerprint(connection)
        connection.exec_driver_sql('INSERT INTO agri_schema_migration_steps VALUES '
            "(6,'001.signed_consultation_review',?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (fingerprint,))
        record_schema_revision(connection, metadata, version=6)
        connection.exec_driver_sql("UPDATE agri_schema_migrations SET state='completed',fingerprint=?,current_step='001.signed_consultation_review',error_type=NULL,completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE target_version=6", (fingerprint,))
        connection.commit()
    except BaseException:
        connection.rollback()
        # All revision 6 DDL/data was rolled back; v5 stays valid for a clean retry.
        raise
    return {'schema_revision': 6, 'steps': 10, 'replayed': False}
