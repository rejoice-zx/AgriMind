"""The engineering runner must preserve failure gates after benchmark retirement."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "backend" / "tools" / "verify_all.py"
    spec = importlib.util.spec_from_file_location("engineering_verification_runner", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "BASE_DIR", tmp_path)
    return module


def controlled_commands(runner, monkeypatch, failure=None):
    calls = []

    def run(command, cwd=None, shell=False, extra_env=None):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "--short"]:
            return 0, "fixture-head", 0.01
        if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "fixture-branch", 0.01
        if len(command) > 1 and Path(command[1]).name == "export_github_source.py":
            assert '--check' in command
            if failure == "scan_error":
                return 2, "", 0.01
            if failure == "scan_hit":
                return 1, json.dumps({'status': 'fail', 'matched_files': 1, 'checked_text_files': 10}), 0.01
            return 0, json.dumps({'status': 'pass', 'matched_files': 0,
                'checked_text_files': 0 if failure == 'scan_empty' else 10}), 0.01
        if command[1:2] == ["-c"]:
            assert 'app.config' not in command[-1]
            return (1 if failure == "dependencies" else 0), "fixture dependency imports", 0.01
        if len(command) > 1 and Path(command[1]).name == "run_agri_checks.py":
            assert extra_env and Path(extra_env['AGRI_CHECK_SCRATCH']).is_dir()
            if failure == "pytest":
                return 1, "1 failed, 807 passed", 0.01
            if failure == "pytest_no_summary":
                return 0, "No execution summary", 0.01
            return 0, "808 passed in 0.01s", 0.01
        if "--test" in command:
            return (1 if failure == "frontend_tests" else 0), "# tests 181\n# pass 181\n# fail 0", 0.01
        if len(command) > 1 and Path(command[1]).name == "vue-tsc.js":
            return (1 if failure == "typecheck" else 0), "fixture typecheck", 0.01
        if len(command) > 1 and Path(command[1]).name == "build_agri_frontend.mjs":
            assert extra_env and Path(extra_env['AGRI_BUILD_SCRATCH']).is_dir()
            return (1 if failure == "build" else 0), "fixture build", 0.01
        raise AssertionError(f"Unexpected or retired verification command: {command}")

    monkeypatch.setattr(runner, "run_command", run)
    return calls


def test_successive_runs_keep_separate_snapshots_without_documentation(runner, monkeypatch):
    calls = controlled_commands(runner, monkeypatch)
    assert runner.main() is True
    snapshots = list(runner.BASE_DIR.glob("scratch/verification-*/baseline_snapshot.md"))
    assert len(snapshots) == 1
    original = snapshots[0].read_bytes()
    assert runner.main() is True
    assert len(list(runner.BASE_DIR.glob("scratch/verification-*/baseline_snapshot.md"))) == 2
    assert snapshots[0].read_bytes() == original
    assert not (runner.BASE_DIR / "doc").exists()
    assert not (runner.BASE_DIR / "docs").exists()
    temp_arguments = [arg for cmd in calls for arg in cmd if arg.startswith("--junitxml=")]
    assert len(temp_arguments) == 2 and len(set(temp_arguments)) == 2


@pytest.mark.parametrize("failure", [
    "scan_error", "scan_hit", "scan_empty", "dependencies", "pytest", "pytest_no_summary", "frontend_tests", "typecheck", "build",
])
def test_remaining_checks_cannot_report_success_when_a_gate_fails(runner, monkeypatch, failure):
    controlled_commands(runner, monkeypatch, failure)
    assert runner.main() is False


def test_git_metadata_is_optional_for_source_archive_verification(runner, monkeypatch):
    controlled_commands(runner, monkeypatch)
    original = runner.run_command

    def without_git(command, *args, **kwargs):
        if command[0] == 'git':
            raise FileNotFoundError('synthetic missing executable')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(runner, 'run_command', without_git)
    assert runner.main() is True
