"""Export reviewable source without local state or existing Git history; never overwrite outputs."""
import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]
TOPS = {'backend', 'frontend', 'config', 'image', '.github'}
ROOT_FILES = {'README.md', '.gitignore', 'pytest.ini', 'LICENSE', 'LICENSE.md'}
SKIP_DIRS = {'node_modules', '__pycache__', '.pytest_cache', '.venv', 'venv', 'logs', 'scratch', 'dist', '.git', '.rivet', '.agent', '.cache', '.pytest_tmp'}
TEXT_SUFFIXES = {'.py', '.ts', '.vue', '.js', '.mjs', '.json', '.md', '.yml', '.yaml', '.toml', '.ini', '.txt', '.html', '.css', '.scss', '.ps1', '.lock', '.example'}
KEY_PATTERN = re.compile(rb'(?:sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def allowed(path):
    relative = path.relative_to(ROOT)
    parts = relative.parts
    lower = path.name.lower()
    # Design tokens are frontend source; still scan their content for credentials.
    is_stylesheet = path.suffix.lower() in {'.css', '.scss'}
    if lower.startswith(('.env', 'credentials.')) or ('token' in lower and not is_stylesheet) or 'secret' in lower or ('private' in lower and 'key' in lower): return False
    if any(part in SKIP_DIRS or part.startswith(('agrimind-stage-a-', 'agri-build-', 'agri-retirement-preview-', 'verification-')) for part in parts): return False
    if parts[:2] == ('backend', 'data'): return False
    if lower.endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.pyc', '.pyo', '.log', '.tsbuildinfo')): return False
    return (len(parts) == 1 and lower in {name.lower() for name in ROOT_FILES}) or (len(parts) > 1 and parts[0] in TOPS)


def source_files():
    # Do not descend into local runtime/agent data, dependencies or prior test snapshots.
    import os
    files = []
    for name in sorted(ROOT_FILES):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            path.resolve().relative_to(ROOT)
            files.append(path)
    for top in sorted(TOPS):
        if not (ROOT / top).is_dir(): continue
        for directory, folders, names in os.walk(ROOT / top, followlinks=False):
            base = Path(directory)
            folders[:] = sorted(name for name in folders if allowed(base / name) and not (base / name).is_symlink()
                and not (hasattr(base / name, 'is_junction') and (base / name).is_junction()))
            for name in sorted(names):
                path = base / name
                if path.is_symlink() or not allowed(path): continue
                path.resolve().relative_to(ROOT)
                files.append(path)
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())


def check_source():
    """Scan selected current text files, including untracked source, without Git or ZIP output."""
    files = source_files()
    text_files = [path for path in files if path.suffix.lower() in TEXT_SUFFIXES]
    matched = sum(bool(KEY_PATTERN.search(path.read_bytes())) for path in text_files)
    return {'status': 'pass' if text_files and matched == 0 else 'fail',
        'matched_files': matched, 'checked_text_files': len(text_files), 'source_files': len(files)}


def export(output_parent):
    files = source_files()
    records = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix.lower() in TEXT_SUFFIXES and KEY_PATTERN.search(path.read_bytes()):
            raise ValueError('源码疑似包含凭证材料，停止导出；未输出匹配内容')
        records.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest, 'bytes': path.stat().st_size})
    destination = Path(output_parent).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    archive = destination / f'AgriMind-source-{stamp}-{uuid.uuid4().hex[:8]}.zip'
    manifest = {'created_at': datetime.now(timezone(timedelta(hours=8))).isoformat(), 'file_count': len(records),
        'scope': 'Application source, README, configuration template and screenshots; no agent guidance, development reviews/design documents, Git history, deployment files, local agent state, runtime data or generated test/build snapshots.', 'files': records}
    with ZipFile(archive, 'x', compression=ZIP_DEFLATED) as output:
        for path, record in zip(files, records):
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != record['sha256']: raise ValueError('导出期间源码发生变化，请重新导出')
            output.writestr('AgriMind/' + record['path'], content)
        output.writestr('AgriMind/source-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with ZipFile(archive) as check:
        if check.testzip() is not None or len(check.namelist()) != len(records) + 1: raise ValueError('源码包完整性校验失败')
    return {'archive': str(archive), 'source_files': len(records), 'bytes': archive.stat().st_size,
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    parser.add_argument('--check', action='store_true', help='Only check selected source for credential patterns; no ZIP is created')
    args = parser.parse_args()
    if args.check:
        try:
            result = check_source()
        except (OSError, ValueError):
            print(json.dumps({'status': 'error'}, ensure_ascii=False))
            raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result['status'] == 'pass' else 1)
    print(json.dumps(export(args.output_dir), ensure_ascii=False))
