"""SQLite schema checkpoint shared by startup and offline recovery (no configuration reads)."""
import hashlib
import json

CURRENT_SCHEMA_REVISION = 6
LEDGER_TABLE = 'agri_schema_revisions'


def _execute(connection, statement, parameters=()):
    if hasattr(connection, 'exec_driver_sql'):
        return connection.exec_driver_sql(statement, parameters)
    return connection.execute(statement, parameters)


def schema_fingerprint(connection):
    rows = _execute(connection, "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name != 'agri_schema_revisions' "
        "AND tbl_name != 'agri_schema_revisions' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps([list(row) for row in rows], ensure_ascii=False).encode()).hexdigest()


def schema_revision(connection):
    return int(_execute(connection, 'PRAGMA user_version').fetchone()[0])


def guard_schema_revision(connection):
    version = schema_revision(connection)
    if version > CURRENT_SCHEMA_REVISION:
        raise ValueError('数据库版本高于当前程序，拒绝降级写入；请使用匹配版本在独立副本核验')
    journal = _execute(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agri_schema_migrations'").fetchone()
    if journal and _execute(connection, "SELECT 1 FROM agri_schema_migrations WHERE state != 'completed' LIMIT 1").fetchone():
        raise ValueError('数据库编号升级未完成，请在独立副本按原计划继续；禁止运行或备份为完成状态')
    exists = _execute(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agri_schema_revisions'").fetchone()
    if exists:
        latest = _execute(connection, 'SELECT version,fingerprint FROM agri_schema_revisions ORDER BY version DESC LIMIT 1').fetchone()
        if latest and (latest[0] != version or latest[0] > CURRENT_SCHEMA_REVISION):
            raise ValueError('数据库版本标记与迁移账本不一致')
        if latest and latest[1] != schema_fingerprint(connection):
            raise ValueError('数据库结构与迁移账本不一致，请先在独立副本检查结构变更')
    return version


def verify_model_columns(connection, metadata):
    for table in metadata.sorted_tables:
        actual = {row[1] for row in _execute(connection, 'PRAGMA table_info("' + table.name.replace('"', '""') + '")').fetchall()}
        missing = set(table.columns.keys()) - actual
        if missing:
            raise ValueError('数据库升级未完成，缺少字段：' + table.name + '.' + ','.join(sorted(missing)))


def verify_runtime_schema(connection, metadata):
    """Read only: a version number alone is not a completed, fingerprinted upgrade."""
    if guard_schema_revision(connection) != CURRENT_SCHEMA_REVISION:
        raise ValueError('数据库版本尚未升级至当前程序')
    ledger = _execute(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agri_schema_revisions'").fetchone()
    if not ledger or not _execute(connection, 'SELECT 1 FROM agri_schema_revisions WHERE version=?', (CURRENT_SCHEMA_REVISION,)).fetchone():
        raise ValueError('缺少当前结构检查点')
    verify_model_columns(connection, metadata)


def record_schema_revision(connection, metadata, version=None):
    """Stamp only after all existing idempotent migrations produced every model column."""
    version = CURRENT_SCHEMA_REVISION if version is None else version
    if not 0 < version <= CURRENT_SCHEMA_REVISION:
        raise ValueError('不能登记未知或未来结构版本')
    verify_model_columns(connection, metadata)
    fingerprint = schema_fingerprint(connection)
    _execute(connection, 'CREATE TABLE IF NOT EXISTS agri_schema_revisions ('
        'version INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL)')
    prior = _execute(connection, 'SELECT fingerprint FROM agri_schema_revisions WHERE version=?', (version,)).fetchone()
    if prior and prior[0] != fingerprint:
        raise ValueError('当前版本出现未登记的结构变更，需要新增迁移版本')
    _execute(connection, 'INSERT OR IGNORE INTO agri_schema_revisions(version,fingerprint,applied_at) '
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (version, fingerprint))
    _execute(connection, 'PRAGMA user_version=' + str(version))


def install_history_guards(connection):
    """Protect parent history even when a bulk DELETE bypasses ORM services."""
    for table in ('farms', 'fields', 'crop_plantings'):
        _execute(connection, 'CREATE TRIGGER IF NOT EXISTS protect_agri_' + table + '_delete '
            'BEFORE DELETE ON ' + table + " BEGIN SELECT RAISE(ABORT,'Agricultural history is protected; archive instead'); END")
    for table in ('crop_plantings', 'farming_records', 'disease_records', 'weather_snapshots'):
        for operation, row in (('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')):
            condition = 'EXISTS(SELECT 1 FROM fields f JOIN farms a ON a.id=f.farm_id WHERE f.id=' + row + '.field_id AND (f.archived_at IS NOT NULL OR a.archived_at IS NOT NULL))'
            if operation == 'UPDATE':
                condition += ' OR EXISTS(SELECT 1 FROM fields f JOIN farms a ON a.id=f.farm_id WHERE f.id=OLD.field_id AND (f.archived_at IS NOT NULL OR a.archived_at IS NOT NULL))'
            _execute(connection, 'CREATE TRIGGER IF NOT EXISTS archive_guard_' + table + '_' + operation.lower() + ' BEFORE ' + operation + ' ON ' + table +
                ' WHEN ' + condition + " BEGIN SELECT RAISE(ABORT,'Agricultural archive is read only'); END")
    if _execute(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_environment_observations'").fetchone():
        for operation in ('UPDATE', 'DELETE'):
            _execute(connection, 'CREATE TRIGGER IF NOT EXISTS protect_environment_' + operation.lower() +
                ' BEFORE ' + operation + " ON field_environment_observations BEGIN SELECT RAISE(ABORT,'Field observations are append only'); END")
        _execute(connection, 'CREATE TRIGGER IF NOT EXISTS archive_guard_environment_insert BEFORE INSERT ON field_environment_observations '
            'WHEN EXISTS(SELECT 1 FROM fields f JOIN farms a ON a.id=f.farm_id WHERE f.id=NEW.field_id '
            "AND (f.archived_at IS NOT NULL OR a.archived_at IS NOT NULL)) BEGIN SELECT RAISE(ABORT,'Agricultural archive is read only'); END")
    if _execute(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crop_status_events'").fetchone():
        for operation in ('UPDATE', 'DELETE'):
            _execute(connection, 'CREATE TRIGGER IF NOT EXISTS protect_crop_status_' + operation.lower() +
                ' BEFORE ' + operation + " ON crop_status_events BEGIN SELECT RAISE(ABORT,'Crop status history is append only'); END")
        _execute(connection, 'CREATE TRIGGER IF NOT EXISTS archive_guard_crop_status_insert BEFORE INSERT ON crop_status_events '
            'WHEN EXISTS(SELECT 1 FROM crop_plantings c JOIN fields f ON f.id=c.field_id JOIN farms a ON a.id=f.farm_id '
            "WHERE c.id=NEW.crop_planting_id AND (f.archived_at IS NOT NULL OR a.archived_at IS NOT NULL)) BEGIN SELECT RAISE(ABORT,'Agricultural archive is read only'); END")
