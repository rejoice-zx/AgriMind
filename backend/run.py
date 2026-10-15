"""Local launcher: opt-in first key initialization, stable reuse, no key output."""
import argparse
import os
from pathlib import Path
import secrets
import sqlite3
import sys


def prepare_key(configured, key_path, has_v3, initialize=False):
    if configured:
        if len(configured.strip()) < 32: raise RuntimeError('独立配置加密密钥至少需要32字符')
        return configured
    key_path = Path(key_path)
    if key_path.exists():
        if key_path.is_symlink(): raise RuntimeError('拒绝使用符号链接密钥文件')
        key = key_path.read_text(encoding='ascii').strip()
        if len(key) < 32: raise RuntimeError('本地密钥文件无效，请恢复原文件，不能自动覆盖')
        return key
    if has_v3:
        raise RuntimeError('数据库已有独立密钥密文，请恢复原密钥；禁止生成替代密钥')
    if not initialize:
        raise RuntimeError('首次启动请增加 --initialize-encryption；密钥只创建一次并保存在当前用户配置目录')
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import getpass
        import subprocess
        subprocess.run(['icacls', str(key_path.parent), '/inheritance:r', '/grant:r', getpass.getuser() + ':(OI)(CI)(F)'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        key_path.parent.chmod(0o700)
    key = secrets.token_urlsafe(48)
    # Exclusive creation: never overwrite another launcher's key.
    with key_path.open('x', encoding='ascii') as stream:
        if os.name != 'nt': os.fchmod(stream.fileno(), 0o600)
        stream.write(key)
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize-encryption', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'backend'))
    os.environ.setdefault('APP_ENV', 'development')
    from app.config import settings
    from sqlalchemy.engine import make_url
    url = make_url(settings.DATABASE_URL)
    if url.get_backend_name() != 'sqlite': raise RuntimeError('此本地启动器只支持当前SQLite部署')
    db_path = Path(url.database).resolve() if url.database and url.database != ':memory:' else None
    has_v3 = False
    if db_path and db_path.exists():
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_settings'").fetchone()
            if exists: has_v3 = bool(db.execute("SELECT 1 FROM system_settings WHERE value LIKE '%v3:%' LIMIT 1").fetchone())
    store = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local/share')) / 'AgriMind' / 'settings-encryption.key'
    settings.SETTINGS_ENCRYPTION_KEY = prepare_key(settings.SETTINGS_ENCRYPTION_KEY, store, has_v3, args.initialize_encryption)
    print('本地配置加密已就绪；请保管当前用户配置目录中的密钥文件，不要提交或随意更换。')
    import uvicorn
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    try: main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Configuration validation exceptions can embed input values; never dump them.
        print('本地启动失败，请检查配置格式、依赖和用户目录权限；未输出配置内容。', file=sys.stderr)
        raise SystemExit(1)
