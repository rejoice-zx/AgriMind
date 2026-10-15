"""Upgrade only a verified, unused restore copy; never imports runtime configuration."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from app.core.recovery_bundle import file_digest, open_readonly, table_counts, validate_database, _allowed_name
from app.core.schema_revision import CURRENT_SCHEMA_REVISION, schema_fingerprint
from app.migrations.schema_upgrade_v6 import run_schema_upgrade


@contextmanager
def _copy_lock(root):
    # Kernel lock is released on a crash. The inert file may remain for a retry.
    with (root / 'UPGRADE.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_marker(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def upgrade_restore_copy(destination):
    root = Path(destination).absolute()
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError('升级路径必须是无符号链接的独立恢复目录')
    # Refuse links/reparse points even inside the dedicated copy.
    for entry in root.rglob('*'):
        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
            raise ValueError('恢复副本中存在链接，拒绝升级')
    ready_path, database = root / 'READY.json', root / 'app.db'
    ready = json.loads(ready_path.read_text(encoding='utf-8'))
    if (ready.get('verified') is not True or not ready.get('database_sha256') or
            not isinstance(ready.get('table_counts'), dict) or not isinstance(ready.get('files'), list)):
        raise ValueError('恢复核验标记缺少升级依据，请从原备份重新恢复到另一新目录')
    for item in ready['files']:
        name = item['path']
        if not _allowed_name(name):
            raise ValueError('恢复标记含不允许的路径')
        if name != 'app.db' and (file_digest(root / name) != item['sha256'] or (root / name).stat().st_size != item['size_bytes']):
            raise ValueError('恢复后的媒体发生变化，拒绝升级')
    with _copy_lock(root):
        for suffix in ('-wal', '-shm', '-journal'):
            if Path(str(database) + suffix).exists():
                raise ValueError('副本存在活动或未收束的SQLite旁文件，请停用并核查副本')
        started = root / 'UPGRADE_STARTED.json'
        identity = {'ready_sha256': file_digest(ready_path), 'target_version': CURRENT_SCHEMA_REVISION}
        if started.exists():
            if json.loads(started.read_text(encoding='utf-8')) != identity:
                raise ValueError('原恢复核验标记或升级版本已变化')
        else:
            if file_digest(database) != ready['database_sha256']:
                raise ValueError('恢复数据库已发生变更，拒绝自动升级')
            _write_marker(started, identity)
        completed = root / 'UPGRADED.json'
        if completed.exists():
            result = json.loads(completed.read_text(encoding='utf-8'))
            if result.get('database_sha256') != file_digest(database) or result.get('schema_revision') != CURRENT_SCHEMA_REVISION:
                raise ValueError('升级完成后数据库已变化，不能复用旧验收标记')
            return {**result, 'replayed': True}
        import app.models  # registers models without importing app.config/app.database
        from app.core.model_base import Base
        engine = create_engine(URL.create('sqlite', database=str(database)))
        @event.listens_for(engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        try:
            with engine.connect() as connection:
                result = run_schema_upgrade(connection, Base.metadata)
        finally:
            engine.dispose()
        with open_readonly(database) as connection:
            validate_database(connection)
            counts = table_counts(connection)
            for name, count in ready['table_counts'].items():
                if name.startswith('agri_schema_'):
                    continue
                if counts.get(name) != count:
                    raise ValueError('升级前后原业务表记录数发生变化')
            result.update(schema_fingerprint=schema_fingerprint(connection), table_counts=counts)
        result.update(database_sha256=file_digest(database), upgraded_at=datetime.now(timezone.utc).isoformat(),
            action='仅完成独立副本结构升级；尚未验证配置解密、业务运行或执行数据切换')
        _write_marker(completed, result)
        return result
