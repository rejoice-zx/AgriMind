"""Revision 5 adopts legacy 0–4 databases with committed, resumable numbered steps.

Own a fresh connection. Each step has an explicit SQLite transaction, including
DDL; a failed step rolls back, earlier checkpoints remain for a verified retry.
No settings, seeds, accounts, statistics bootstrap or live cutover runs here.
"""
import hashlib
import inspect
from app.core.schema_revision import (CURRENT_SCHEMA_REVISION, guard_schema_revision,
    schema_revision, schema_fingerprint, verify_model_columns, record_schema_revision, install_history_guards)
from app.migrations.migrate_auth_subject import migrate_auth_subjects
from app.migrations.migrate_issue2 import migrate_issue2
from app.migrations.migrate_registration_review import migrate_registration_review
from app.migrations.migrate_field_environment import migrate_field_environment
from app.migrations.migrate_legacy_columns import migrate_legacy_columns
from app.core.agri_statistics_indexes import migrate_statistics_indexes

TARGET = 5
STEPS = (
    ('001.create_tables', None),
    ('002.account_subjects', migrate_auth_subjects),
    ('003.issue2_columns', migrate_issue2),
    ('004.registration_review', migrate_registration_review),
    ('005.legacy_columns', migrate_legacy_columns),
    ('006.field_environment', migrate_field_environment),
    ('007.statistics_indexes', migrate_statistics_indexes),
    ('008.history_guards', install_history_guards),
    ('009.verify_models', None),
)


def plan_digest():
    return hashlib.sha256(('revision5-plan1\n' + '\n'.join(
        name + (inspect.getsource(handler) if handler else '') for name, handler in STEPS)).encode()).hexdigest()


def _integrity(connection):
    if connection.exec_driver_sql('PRAGMA integrity_check').scalar() != 'ok':
        raise ValueError('数据库完整性检查失败')
    if connection.exec_driver_sql('PRAGMA foreign_key_check').first():
        raise ValueError('数据库存在外键断链，不能完成升级')


def run_schema_upgrade(connection, metadata):
    if connection.dialect.name != 'sqlite' or connection.in_transaction():
        raise ValueError('编号升级仅支持独立SQLite连接，不能嵌入业务事务')
    if CURRENT_SCHEMA_REVISION < TARGET:
        raise ValueError('迁移目标版本与程序不匹配')
    digest = plan_digest()
    try:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        version = schema_revision(connection)
        if version > TARGET:
            raise ValueError('数据库版本高于当前程序，拒绝降级')
        has_journal = connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agri_schema_migrations'").first()
        journal = connection.exec_driver_sql('SELECT * FROM agri_schema_migrations WHERE target_version=?', (TARGET,)).mappings().first() if has_journal else None
        if version == TARGET:
            guard_schema_revision(connection)
            verify_model_columns(connection, metadata)
            if not journal or journal['state'] != 'completed' or journal['plan_digest'] != digest:
                raise ValueError('当前版本缺少匹配的编号升级完成记录')
            _integrity(connection)
            connection.commit()
            return {'schema_revision': TARGET, 'steps': len(STEPS), 'replayed': True}
        if journal:
            if (journal['source_version'] != version or journal['plan_digest'] != digest or
                    journal['state'] not in {'running', 'failed'} or journal['fingerprint'] != schema_fingerprint(connection)):
                raise ValueError('升级进度、迁移计划或结构已变化，拒绝自动继续')
            if version:
                prior = connection.exec_driver_sql('SELECT fingerprint FROM agri_schema_revisions WHERE version=?', (version,)).first()
                if not prior or prior[0] != journal['source_fingerprint']:
                    raise ValueError('原版本账本与升级起点不一致')
        else:
            guard_schema_revision(connection)
            if version:
                ledger = connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agri_schema_revisions'").first()
                if not ledger or not connection.exec_driver_sql('SELECT 1 FROM agri_schema_revisions WHERE version=?', (version,)).first():
                    raise ValueError('非零版本缺少原结构账本')
            source = schema_fingerprint(connection)
            _integrity(connection)
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS agri_schema_migrations ('
                'target_version INTEGER PRIMARY KEY, source_version INTEGER NOT NULL, source_fingerprint TEXT NOT NULL, '
                'plan_digest TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, current_step TEXT, '
                'error_type TEXT, started_at TEXT NOT NULL, completed_at TEXT)')
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS agri_schema_migration_steps ('
                'target_version INTEGER NOT NULL, step_id TEXT NOT NULL, fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL, '
                'PRIMARY KEY(target_version,step_id))')
            connection.exec_driver_sql('INSERT INTO agri_schema_migrations '
                '(target_version,source_version,source_fingerprint,plan_digest,fingerprint,state,started_at) '
                "VALUES (?,?,?,?,?,'running',strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (TARGET, version, source, digest, schema_fingerprint(connection)))
        done = [row[0] for row in connection.exec_driver_sql('SELECT step_id FROM agri_schema_migration_steps WHERE target_version=? ORDER BY step_id', (TARGET,))]
        if done != [name for name, _ in STEPS[:len(done)]]:
            raise ValueError('编号迁移记录不连续，拒绝跳步')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    for step_id, handler in STEPS[len(done):]:
        try:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            expected = connection.exec_driver_sql('SELECT fingerprint FROM agri_schema_migrations WHERE target_version=?', (TARGET,)).scalar_one()
            if expected != schema_fingerprint(connection):
                raise ValueError('迁移期间结构发生外部变化')
            if step_id == '001.create_tables':
                metadata.create_all(connection)
            elif step_id == '009.verify_models':
                verify_model_columns(connection, metadata)
                _integrity(connection)
            else:
                handler(connection)
            fingerprint = schema_fingerprint(connection)
            connection.exec_driver_sql('INSERT INTO agri_schema_migration_steps VALUES '
                "(?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (TARGET, step_id, fingerprint))
            connection.exec_driver_sql("UPDATE agri_schema_migrations SET fingerprint=?,state='running',current_step=?,error_type=NULL WHERE target_version=?",
                (fingerprint, step_id, TARGET))
            connection.commit()
        except BaseException as error:
            connection.rollback()
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            connection.exec_driver_sql("UPDATE agri_schema_migrations SET state='failed',current_step=?,error_type=? WHERE target_version=?",
                (step_id, type(error).__name__, TARGET))
            connection.commit()
            raise
    try:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        expected = connection.exec_driver_sql('SELECT fingerprint FROM agri_schema_migrations WHERE target_version=?', (TARGET,)).scalar_one()
        if expected != schema_fingerprint(connection):
            raise ValueError('最终登记前结构发生变化')
        record_schema_revision(connection, metadata, version=TARGET)
        connection.exec_driver_sql("UPDATE agri_schema_migrations SET state='completed',error_type=NULL,completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE target_version=?", (TARGET,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {'schema_revision': TARGET, 'steps': len(STEPS), 'replayed': False}
