"""Deployment static configuration (P2-DEPLOY, with Docker removed).

Covers what is still verifiable after the container artifacts were deleted in
c0afe93: the production interactive-docs gate and the CI workflow's shape.
Real container / volume / reverse-proxy behaviour stays EXCLUDED_NOT_RUN.
"""
import pathlib

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from app.main import app, interactive_docs_urls

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "verify.yml"


def test_production_switches_off_the_interactive_docs():
    assert interactive_docs_urls(True) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_non_production_keeps_the_interactive_docs():
    assert interactive_docs_urls(False) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


@pytest.mark.asyncio
async def test_this_non_production_app_still_serves_openapi():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json().get("info", {}).get("title")


def test_ci_workflow_is_present_valid_and_container_free():
    assert WORKFLOW.is_file(), f"missing {WORKFLOW}"
    text = WORKFLOW.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)

    assert set(spec["jobs"]) == {"backend", "frontend"}

    lowered = text.lower()
    for banned in ("docker", "dockerfile", "docker-compose", "nginx"):
        assert banned not in lowered, f"CI must not reference {banned}"

    backend_runs = " ".join(step.get("run", "") for step in spec["jobs"]["backend"]["steps"])
    assert "python tools/run_agri_checks.py tests" in backend_runs
    assert "--junitxml=" in backend_runs
    frontend_runs = " ".join(step.get("run", "") for step in spec["jobs"]["frontend"]["steps"])
    assert "typecheck" in frontend_runs
    assert "build" in frontend_runs
    assert "npm test" in frontend_runs
