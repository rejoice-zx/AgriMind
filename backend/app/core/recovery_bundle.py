"""Consistent DB + local diagnosis media bundles; recovery only into a new directory."""
import hashlib
import json
import re
import sqlite3
import stat
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from app.core.schema_revision import guard_schema_revision, schema_fingerprint, schema_revision, CURRENT_SCHEMA_REVISION

BUNDLE_VERSION = 1
MAX_TOTAL_BYTES = 32 * 1024**3
MEDIA_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\.(?:jpeg|jpg|png|webp)')


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def open_readonly(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True))


def validate_database(connection):
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('数据库完整性检查失败')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('数据库存在外键断链，需要先核对，不能作为已验收恢复副本')
    guard_schema_revision(connection)


def snapshot_database(source, destination):
    destination = Path(destination)
    with destination.open('xb'):
        pass
    with open_readonly(source) as live, closing(sqlite3.connect(destination)) as snapshot:
        live.backup(snapshot, pages=256, sleep=0.01)
        validate_database(snapshot)


def table_counts(connection):
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}


def _media_inventory(connection):
    """Use the frozen snapshot's references, never a second live DB read."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    references, expected = set(), {}
    if 'diagnosis_media' in tables:
        for name, digest, size in connection.execute('SELECT id,sha256,size_bytes FROM diagnosis_media'):
            if not re.fullmatch(r'[0-9a-f]{32}\.(?:jpeg|jpg|png|webp)', name):
                raise ValueError('媒体登记名称不合法')
            relative = 'private_uploads/diagnosis/' + name
            references.add(relative); expected[relative] = (digest, size)
    for table in ('disease_records', 'agri_turns'):
        if table not in tables:
            continue
        for (url,) in connection.execute('SELECT image_url FROM ' + table + ' WHERE image_url IS NOT NULL'):
            if not url:
                continue
            for prefix, directory in (('/api/diagnosis/media/', 'private_uploads'), ('/static/uploads/diagnosis/', 'uploads')):
                if url.startswith(prefix):
                    name = url[len(prefix):]
                    if not MEDIA_NAME.fullmatch(name):
                        raise ValueError('历史媒体引用名称不合法')
                    references.add(directory + '/diagnosis/' + name)
    return references, expected


def create_bundle(snapshot_path, data_root, bundle_path, *, description='', app_version='unknown'):
    snapshot_path, data_root, bundle_path = Path(snapshot_path), Path(data_root).resolve(), Path(bundle_path)
    with open_readonly(snapshot_path) as database:
        validate_database(database)
        references, expected = _media_inventory(database)
        manifest = dict(format_version=BUNDLE_VERSION, created_at=datetime.now(timezone.utc).isoformat(),
            app_version=app_version, description=description, schema_revision=schema_revision(database),
            schema_fingerprint=schema_fingerprint(database), table_counts=table_counts(database),
            scope='database_and_local_diagnosis_media', files=[])
    files = [('app.db', snapshot_path)]
    for relative in sorted(references):
        path = (data_root / relative).resolve()
        if not path.is_relative_to(data_root) or not path.is_file():
            raise ValueError('存在缺失或越界的媒体，备份包未完成：' + relative)
        if relative in expected and (file_digest(path), path.stat().st_size) != expected[relative]:
            raise ValueError('媒体内容与登记摘要不一致：' + relative)
        files.append((relative, path))
    total = 0
    with zipfile.ZipFile(bundle_path, 'x', compression=zipfile.ZIP_STORED) as archive:
        for relative, path in files:
            size = path.stat().st_size
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError('备份包超过支持的32GiB范围')
            before = file_digest(path)
            archive.write(path, relative)
            # A changed image must never leave a supposedly complete package.
            if file_digest(path) != before:
                raise ValueError('备份期间文件发生变化，请重试')
            manifest['files'].append(dict(path=relative, size_bytes=size, sha256=before))
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        if len(encoded) > 4 * 1024**2:
            raise ValueError('备份清单超过支持范围，请分批归档媒体')
        archive.writestr('manifest.json', encoded)
    return manifest


def _allowed_name(name):
    path = PurePosixPath(name)
    if str(path) != name or path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    return name == 'app.db' or (len(path.parts) == 3 and path.parts[0] in {'private_uploads', 'uploads'}
        and path.parts[1] == 'diagnosis' and MEDIA_NAME.fullmatch(path.parts[2]) is not None)


def prepare_restore(bundle_path, destination):
    """Leave existing paths untouched; READY.json appears only after all checks pass."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('恢复目标必须是不存在的新目录，禁止覆盖已有路径')
    with zipfile.ZipFile(bundle_path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise ValueError('备份包缺少清单或包含重复文件')
        if archive.getinfo('manifest.json').file_size > 4 * 1024**2:
            raise ValueError('备份清单超过支持范围')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('format_version') != BUNDLE_VERSION:
            raise ValueError('不支持的备份包格式版本')
        files = manifest.get('files')
        if not isinstance(files, list) or not files or len(files) > 100000:
            raise ValueError('备份文件清单不合法')
        records = {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError('备份文件记录不合法')
            name, size, digest = item.get('path'), item.get('size_bytes'), item.get('sha256')
            if not isinstance(name, str) or not _allowed_name(name) or name in records:
                raise ValueError('备份包包含不允许的路径')
            if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('文件摘要或大小不合法')
            records[name] = item
        if 'app.db' not in records or set(names) != set(records) | {'manifest.json'}:
            raise ValueError('备份包文件与清单不一致')
        if sum(item['size_bytes'] for item in files) > MAX_TOTAL_BYTES:
            raise ValueError('恢复数据超过支持的32GiB范围')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                raise ValueError('不接受符号链接或加密的备份包')
            if entry.filename != 'manifest.json' and entry.file_size != records[entry.filename]['size_bytes']:
                raise ValueError('压缩包文件大小与清单不一致')
        destination.mkdir(parents=False, exist_ok=False)
        for name, item in records.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as source, target.open('xb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(block)
                    if size > item['size_bytes']:
                        raise ValueError('恢复文件超过清单大小')
                    digest.update(block); output.write(block)
            if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                raise ValueError('恢复文件校验失败；此目录不得切换为运行数据')
    with open_readonly(destination / 'app.db') as database:
        validate_database(database)
        if (schema_revision(database) != manifest.get('schema_revision') or
                schema_fingerprint(database) != manifest.get('schema_fingerprint') or
                table_counts(database) != manifest.get('table_counts')):
            raise ValueError('恢复数据库的版本、结构或记录数不匹配')
    with (destination / 'READY.json').open('x', encoding='utf-8') as output:
        json.dump(dict(verified=True, scope=manifest['scope'], schema_revision=manifest['schema_revision'],
            database_sha256=records['app.db']['sha256'], schema_fingerprint=manifest['schema_fingerprint'],
            table_counts=manifest['table_counts'], files=manifest['files'],
            needs_schema_upgrade=manifest['schema_revision'] < CURRENT_SCHEMA_REVISION,
            restored_at=datetime.now(timezone.utc).isoformat(), media_files=len(records)-1,
            action='独立副本已核验；切换前须停服、保留原数据并配置原加密密钥，禁止运行中覆盖'), output, ensure_ascii=False, indent=2)
    return manifest
