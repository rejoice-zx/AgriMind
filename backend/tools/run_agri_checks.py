"""Run offline checks in fresh storage without reading deployment configuration."""
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
scratch = Path(os.environ.get("AGRI_CHECK_SCRATCH") or ROOT / "scratch")
scratch.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="agrimind-stage-a-", dir=scratch))
os.environ.update({"APP_ENV": "development", "ENV": "development", "DEBUG": "False",
    "DATABASE_URL": "sqlite+aiosqlite:///" + (WORK / "checks.db").as_posix(),
    "DATA_DIR": str(WORK / "data"), "LOGS_DIR": str(WORK / "logs"), "BASE_DIR": str(WORK),
    "JWT_SECRET_KEY": uuid.uuid4().hex + uuid.uuid4().hex,
    "SETTINGS_ENCRYPTION_KEY": uuid.uuid4().hex + uuid.uuid4().hex,
    "LLM_API_KEY": "", "VISION_API_KEY": "", "WEATHER_API_KEY": "", "EMBEDDING_API_KEY": "",
    "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "FAISS_OPT_LEVEL": "generic",
    "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "ALLOW_LOCAL_DATABASE_SNAPSHOT_TEST": "false"})
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "backend"))
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import pydantic_settings
original_init = pydantic_settings.BaseSettings.__init__
def isolated_init(self, *args, **kwargs):
    kwargs["_env_file"] = None
    original_init(self, *args, **kwargs)
pydantic_settings.BaseSettings.__init__ = isolated_init

# Preserve Windows asyncio socketpair creation; reject real traffic at the HTTP transport.
import httpx
import requests
def deny_http(*args, **kwargs):
    raise RuntimeError("Agricultural checks disable real HTTP requests")
async def deny_async_http(*args, **kwargs):
    raise RuntimeError("Agricultural checks disable real HTTP requests")
httpx.HTTPTransport.handle_request = deny_http
httpx.AsyncHTTPTransport.handle_async_request = deny_async_http
requests.Session.send = deny_http

# Import native FAISS before pytest's capture hooks on Windows.
import faiss

if __name__ == "__main__":
    import pytest
    os.chdir(ROOT / "backend")
    targets = sys.argv[1:] or ["tests/test_agri_optimization_a.py"]
    # This test hard-codes a real file; DATABASE_URL cannot isolate it.
    targets += ["--ignore=" + str(ROOT / "backend/tests/test_local_database_upgrade_snapshot.py")]
    print("Offline checks: fresh DB/data/logs; deployment configuration disabled", flush=True)
    print("CHECK_OUTPUT=" + str(WORK), flush=True)
    raise SystemExit(pytest.main([*targets, "-q", "--tb=short", "-p", "no:cacheprovider", "--basetemp", str(WORK / "pytest")]))
