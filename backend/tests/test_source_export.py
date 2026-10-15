"""The GitHub source export must omit runtime data and keep useful source/docs."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def test_source_export_omits_runtime_and_git_history_and_keeps_source(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'backend/tools/export_github_source.py'
    spec = importlib.util.spec_from_file_location('source_export_under_test', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = tmp_path / 'project'; root.mkdir()
    for name in ('README.md', 'AGENTS.md', 'backend/app/main.py', 'doc/README.md',
                 'config/environment.example', 'image/screen.jpg', 'frontend/tools/build_agri_frontend.mjs',
                 'frontend/src/styles/tokens.css',
                 'suggestions/end/report.md',
                 '.rivet/local.txt', 'data/local.txt', '.git/history.txt',
                 'frontend/node_modules/local.txt', 'suggestions/codex/agrimind-stage-a-old/local.txt'):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic source', encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', root)
    assert not module.allowed(root / '.env')
    assert not module.allowed(root / 'backend/app/example_token.py')
    result = module.export(tmp_path / 'exports')
    with ZipFile(result['archive']) as archive:
        assert set(archive.namelist()) == {'AgriMind/README.md',
            'AgriMind/backend/app/main.py', 'AgriMind/config/environment.example', 'AgriMind/image/screen.jpg',
            'AgriMind/frontend/tools/build_agri_frontend.mjs',
            'AgriMind/frontend/src/styles/tokens.css', 'AgriMind/source-manifest.json'}
        manifest = json.loads(archive.read('AgriMind/source-manifest.json'))
        assert manifest['file_count'] == result['source_files'] == 6
        assert all(record['sha256'] and record['bytes'] > 0 for record in manifest['files'])


def test_source_scan_checks_untracked_text_and_rejects_an_empty_selection(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'backend/tools/export_github_source.py'
    spec = importlib.util.spec_from_file_location('source_scan_under_test', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = tmp_path / 'project'; root.mkdir()
    monkeypatch.setattr(module, 'ROOT', root)
    assert module.check_source()['status'] == 'fail'
    (root / 'README.md').write_text('synthetic source', encoding='utf-8')
    assert module.check_source()['status'] == 'pass'
    target = root / 'backend/app/main.py'; target.parent.mkdir(parents=True)
    target.write_text('synthetic_input = ' + repr('sk-' + 'x' * 40), encoding='utf-8')
    result = module.check_source()
    assert result['status'] == 'fail' and result['matched_files'] == 1
    target.write_text('synthetic source', encoding='utf-8')
    stylesheet = root / 'frontend/src/styles/tokens.css'
    stylesheet.parent.mkdir(parents=True)
    stylesheet.write_text('/* ' + 'sk-' + 'x' * 40 + ' */', encoding='utf-8')
    result = module.check_source()
    assert result['status'] == 'fail' and result['matched_files'] == 1
    assert not (root / '.git').exists()
    assert not (root / 'dist').exists()


def test_exporter_finds_a_renamed_project_with_spaces_without_git(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'backend/tools/export_github_source.py'
    root = tmp_path / 'AgriMind 改名检查'
    tool = root / 'backend/tools/export_github_source.py'
    tool.parent.mkdir(parents=True)
    shutil.copyfile(source, tool)
    (root / 'README.md').write_text('synthetic source', encoding='utf-8')
    # Exercise a legacy Windows code page even when CI runs on Linux.
    legacy_encoding_env = {**os.environ, 'PYTHONIOENCODING': 'gbk'}
    checked = subprocess.run([sys.executable, str(tool), '--check'], cwd=tmp_path,
        capture_output=True, text=True, encoding='utf-8', env=legacy_encoding_env)
    assert checked.returncode == 0 and json.loads(checked.stdout)['checked_text_files'] == 2
    output = subprocess.run([sys.executable, str(tool), '--output-dir', str(root / 'dist')], cwd=tmp_path,
        capture_output=True, text=True, encoding='utf-8', env=legacy_encoding_env)
    assert output.returncode == 0
    result = json.loads(output.stdout)
    with ZipFile(result['archive']) as archive:
        assert set(archive.namelist()) == {'AgriMind/README.md',
            'AgriMind/backend/tools/export_github_source.py', 'AgriMind/source-manifest.json'}
