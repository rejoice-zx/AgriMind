"""Only synthetic logs and isolated databases; no deployment log content or external requests."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.core.audit_logging import AuditFormatter, audit, bind_audit, request_context, redact
from app.middleware.audit_context import AuditContextMiddleware
from app.services.log_service import LogService
from app.api.logs import router
from app.api.dependencies import get_current_user
from app.models import User
from tests.test_agri_history import history_env, ask
from tests.test_disease_review_lifecycle import env, payload
from app.services.disease_review_service import DiseaseReviewService as Reviews


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'LOGS_DIR', str(tmp_path / 'synthetic_logs'))
    return LogService()


def write(service, name, lines):
    Path(service.log_dir, name).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def line(message='synthetic', *, when='2026-10-10T08:00:00+08:00', level='INFO', **context):
    suffix = ' | ctx=' + json.dumps(context) if context else ''
    return f'{when} | {level} | fixture | {message}' + suffix


def event_records(caplog, event):
    return [record for record in caplog.records if getattr(record, 'audit_fields', {}).get('event') == event]


def test_formatter_redacts_secrets_tracebacks_and_preserves_one_event_identity(logs):
    formatter = AuditFormatter()
    owner = request_context.set({'request_id': str(uuid4())})
    try:
        record = logging.LogRecord('fixture', logging.ERROR, '', 1,
            'password=synthetic-password Authorization: Bearer synthetic-bearer\nforged line', (), None)
        first = formatter.format(record); second = formatter.format(record)
    finally:
        request_context.reset(owner)
    assert first == second and '\n' not in first
    assert 'synthetic-password' not in first and 'synthetic-bearer' not in first
    entry = logs._parse_log_line(first)
    assert entry.context['request_id'] and entry.event_id and not entry.legacy_time
    assert datetime.fromisoformat(entry.timestamp).utcoffset().total_seconds() == 0
    try:
        raise RuntimeError('api_key=synthetic-exception-key')
    except RuntimeError:
        import sys
        record = logging.LogRecord('fixture', logging.ERROR, '', 1, 'controlled exception', (), sys.exc_info())
    assert 'synthetic-exception-key' not in formatter.format(record)


def test_redaction_supports_json_query_credentials_and_does_not_hide_correlation():
    text = '{"access_token": "synthetic-access", "password": "synthetic-pass"} ' + \
           'https://fixture-user:fixture-pass@example.invalid/?api_key=fixture-key&signature=fixture-sig request_id=abc'
    result = redact(text)
    for value in ('synthetic-access', 'synthetic-pass', 'fixture-user', 'fixture-pass', 'fixture-key', 'fixture-sig'):
        assert value not in result
    assert 'request_id=abc' in result
    escaped = json.dumps({'LLM_API_KEY': 'synthetic-escaped-"key-value', 'password': 'synthetic-escaped-"pass-value'})
    assert 'key-value' not in redact(escaped) and 'pass-value' not in redact(escaped)
    assert 'synthetic-basic' not in redact('Authorization: Basic synthetic-basic')


def test_mirrors_deduplicate_but_repeated_events_and_legacy_lines_remain(logs):
    first = line(event_id=uuid4().hex)
    second = line(event_id=uuid4().hex)
    legacy = '2026-10-10 08:00:00 | ERROR | fixture | repeated legacy event'
    write(logs, 'app.log', [first, second, legacy, legacy])
    write(logs, 'error.log', [first, second, legacy, legacy])
    page = logs.read_log_paginated()
    assert page.total == 4 and page.legacy_time_entries == 2
    assert len([entry for entry in page.entries if entry.event_id]) == 2


def test_different_secret_values_are_not_mistaken_for_duplicate_legacy_events(logs):
    write(logs, 'app.log', [line('api_key=synthetic-A'), line('api_key=synthetic-B')])
    page = logs.read_log_paginated()
    assert page.total == 2 and all('synthetic-' not in entry.message for entry in page.entries)
    assert logs.read_log_paginated(keyword='synthetic-A').total == 0


def test_offset_aware_time_filter_rotation_and_exact_context_match(logs):
    request = str(uuid4())
    write(logs, 'app.log', [line('current', when='2026-10-10T00:01:00Z', request_id=request, field_id=12)])
    write(logs, 'app.log.1', [line('boundary', when='2026-10-10T08:00:00+08:00', field_id=12)])
    write(logs, 'error.log', [line('other field', field_id=112)])
    write(logs, 'unrelated.log', [line('must not read')])
    page = logs.read_log_paginated(start_time='2026-10-10T00:00:00Z', end_time='2026-10-10T08:01:00+08:00', field_id=12)
    assert page.total == 2 and page.entries[0].message == 'current'
    assert logs.read_log_paginated(request_id=request).total == 1
    assert logs.read_log_paginated(field_id=2).total == 0
    assert {file.name for file in logs.get_log_files()} == {'app.log', 'app.log.1', 'error.log'}
    with pytest.raises(ValueError): logs.read_log_paginated(start_time='2026-10-10T08:00:00')
    with pytest.raises(ValueError): logs.read_log_paginated(start_time='2026-10-11T00:00:00Z', end_time='2026-10-10T00:00:00Z')


def test_large_tail_is_bounded_and_truncation_does_not_claim_full_total(logs, monkeypatch):
    monkeypatch.setattr(logs, 'FILE_TAIL_BYTES', 2048)
    monkeypatch.setattr(logs, 'MAX_SCAN_BYTES', 3072)
    for name in ('app.log', 'app.log.1', 'error.log'):
        write(logs, name, [line(str(index) + 'x' * 120) for index in range(500)])
    page = logs.read_log_paginated()
    assert page.truncated and page.scanned_bytes <= 3072 and page.total < 500
    assert page.total > 0
    monkeypatch.setattr(logs, 'MAX_ENTRIES', 1)
    page = logs.read_log_paginated()
    assert page.truncated and page.total <= 1


def test_incomplete_and_oversized_records_are_not_presented_as_complete(logs, monkeypatch):
    monkeypatch.setattr(logs, 'MAX_RECORD_BYTES', 200)
    Path(logs.log_dir, 'app.log').write_text(line('x' * 500) + '\n' + line('unfinished'), encoding='utf-8')
    page = logs.read_log_paginated()
    assert page.truncated and page.total == 0


def test_path_guard_and_bounded_redacted_download(logs, monkeypatch):
    for name in ('../outside.log', 'unrelated.log', '/app.log', 'app.log.6'):
        with pytest.raises(ValueError): logs.get_log_content(name)
    write(logs, 'app.log', [line('password=synthetic-file-pass')])
    assert 'synthetic-file-pass' not in logs.get_log_content()
    monkeypatch.setattr(logs, 'MAX_SCAN_BYTES', 5)
    with pytest.raises(ValueError): logs.get_log_content()
    monkeypatch.setattr(logs, 'MAX_SCAN_BYTES', 8192)
    write(logs, 'error.log', [line('temporarily unreadable')])
    original_open = Path.open
    def unavailable(path, *args, **kwargs):
        if path.name == 'error.log': raise PermissionError('synthetic unavailable file')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', unavailable)
    page = logs.read_log_paginated()
    assert page.unavailable_files == 1 and page.truncated and page.total == 1


@pytest.mark.asyncio
async def test_request_context_isolation_header_and_reset_even_on_exception():
    headers = []; requests = []; waiting = asyncio.Event()
    async def app(scope, receive, send):
        requests.append(request_context.get()['request_id'])
        bind_audit(field_id=scope['field'])
        if len(requests) == 2: waiting.set()
        await waiting.wait()
        assert request_context.get()['field_id'] == scope['field']
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'x-request-id', b'untrusted')]})
        await send({'type': 'http.response.body', 'body': b''})
    async def send(message):
        if message['type'] == 'http.response.start': headers.append(dict(message['headers']))
    async def receive(): return {'type': 'http.request', 'body': b''}
    middleware = AuditContextMiddleware(app)
    await asyncio.gather(*[middleware({'type': 'http', 'path': '/api/agent/decide', 'field': index}, receive, send) for index in (1, 2)])
    assert len(set(requests)) == 2 and {header[b'x-request-id'].decode() for header in headers} == set(requests)
    assert request_context.get() is None
    async def fail(*args): raise RuntimeError('controlled')
    with pytest.raises(RuntimeError): await AuditContextMiddleware(fail)({'type': 'http', 'path': '/api/agent/decide'}, receive, send)
    assert request_context.get() is None


@pytest.mark.asyncio
async def test_api_admin_scope_validation_download_and_query_contract(logs, test_session, monkeypatch):
    import app.api.logs as api
    monkeypatch.setattr(api, 'get_log_service', lambda: logs)
    user = User(username='log-auditor', password_hash='unused', role='admin', is_active=True)
    test_session.add(user); await test_session.commit()
    app = FastAPI(); app.include_router(router); app.add_middleware(AuditContextMiddleware)
    async def current(): return user
    app.dependency_overrides[get_current_user] = current
    request = str(uuid4())
    write(logs, 'app.log', [line('password=synthetic-api-pass', request_id=request, field_id=1)])
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        response = await client.get('/api/logs', params={'request_id': request})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        data = response.json()
        assert data['total'] == 1 and data['query_scope'] == 'recent_file_tails' and not data['truncated']
        assert data['items'][0]['context']['field_id'] == 1 and 'synthetic-api-pass' not in response.text
        assert response.headers['x-request-id'] != request
        for params in ({'request_id': 'bad'}, {'page': 1001}, {'field_id': 0}, {'keyword': 'x' * 201},
                       {'start_time': '2026-10-10T08:00:00'}, {'start_time': 'bad'}):
            assert (await client.get('/api/logs', params=params)).status_code == 422
        download = await client.get('/api/logs/download')
        assert download.status_code == 200 and 'synthetic-api-pass' not in download.text
        assert download.headers['cache-control'] == 'private, no-store'
        monkeypatch.setattr(logs, 'MAX_SCAN_BYTES', 5)
        assert (await client.get('/api/logs/download')).status_code == 413
        user.role = 'expert'
        for path in ('/api/logs', '/api/logs/files', '/api/logs/download'):
            assert (await client.get(path)).status_code == 403


def test_log_sink_failure_cannot_change_business_result(monkeypatch):
    def fail(*args, **kwargs): raise OSError('controlled log sink failure')
    monkeypatch.setattr(logging.getLogger('app.audit'), 'info', fail)
    audit('controlled_test', field_id=1)


def test_logging_setup_is_idempotent_and_formatter_offset_is_explicit():
    from app.main import setup_logging, get_log_formatter
    root = logging.getLogger()
    count = len(root.handlers)
    setup_logging(); setup_logging()
    assert len(root.handlers) == count
    record = logging.LogRecord('fixture', logging.INFO, '', 1, 'synthetic', (), None)
    assert datetime.fromisoformat(get_log_formatter().formatTime(record)).tzinfo is not None


@pytest.mark.asyncio
async def test_actual_archive_logs_links_and_replay_without_question_text(history_env, caplog):
    caplog.set_level(logging.INFO, logger='app.audit')
    env = history_env
    env.client._transport.app.add_middleware(AuditContextMiddleware)
    request = str(uuid4())
    question = 'synthetic-private-question-should-not-be-logged'
    body = {'query': question, 'field_id': env.field.id, 'request_id': request}
    first = await env.client.post('/api/agent/decide', json=body)
    assert first.status_code == 200
    replay = await env.client.post('/api/agent/decide', json=body)
    assert replay.status_code == 200 and len(env.calls) == 1
    rows = event_records(caplog, 'decision_completed')
    assert len(rows) == 1
    fields = rows[0].audit_fields
    assert fields['business_request_id'] == request and fields['field_id'] == env.field.id
    assert fields['request_id'] == first.headers['x-request-id']
    assert fields['conversation_id'] == first.json()['conversation_id'] and fields['consultation_id'] == first.json()['consultation_id']
    assert len(event_records(caplog, 'decision_replayed')) == 1
    assert all(question not in record.getMessage() for record in caplog.records if record.name == 'app.audit')


@pytest.mark.asyncio
async def test_failed_archive_has_no_completed_audit(history_env, caplog):
    caplog.set_level(logging.INFO, logger='app.audit')
    response = await ask(history_env, 'fail')
    assert response.status_code == 500
    assert not event_records(caplog, 'decision_completed') and len(event_records(caplog, 'decision_failed')) == 1


@pytest.mark.asyncio
async def test_review_audit_is_committed_once_and_failed_transaction_has_no_success(env, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='app.audit')
    async with env.sessions() as db:
        await Reviews.claim(db, env.record_id, env.users['a'])
        async def fail(*args): raise RuntimeError('controlled archive failure')
        monkeypatch.setattr(Reviews, 'add_knowledge', fail)
        with pytest.raises(RuntimeError): await Reviews.resolve(db, env.record_id, env.users['a'], payload(1))
        assert not event_records(caplog, 'disease_resolved')
        await Reviews.resolve(db, env.record_id, env.users['a'], payload(0))
        await Reviews.resolve(db, env.record_id, env.users['a'], payload(0))
    records = event_records(caplog, 'disease_resolved')
    assert len(records) == 1 and records[0].audit_fields['expert_case_id']
    assert records[0].audit_fields['record_id'] == env.record_id


@pytest.mark.asyncio
async def test_unhandled_error_keeps_server_request_id_and_never_returns_internal_details():
    from app.main import internal_error_response
    app = FastAPI()
    app.add_middleware(AuditContextMiddleware)
    app.add_exception_handler(Exception, internal_error_response)
    @app.get('/controlled-failure')
    async def failure(): raise RuntimeError('synthetic-internal-provider-details')
    async with AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url='http://test') as client:
        response = await client.get('/controlled-failure', headers={'X-Request-ID': 'untrusted-client-value'})
    assert response.status_code == 500
    assert response.headers['x-request-id'] == response.json()['request_id']
    assert response.headers['x-request-id'] != 'untrusted-client-value'
    assert 'synthetic-internal-provider-details' not in response.text
    assert request_context.get() is None


def test_missing_dates_and_traceback_continuations_are_reported_without_fabricated_times(logs):
    write(logs, 'app.log', [line('normal'), '08:00:00 | ERROR | date missing', 'Traceback continuation'])
    page = logs.read_log_paginated()
    assert page.total == 1 and page.unparsed_lines == 2
    assert logs._parse_log_line(line(when='9999-12-31T23:59:59-14:00')) is None
