"""Real settings persistence and controlled streamed HTTP; no commercial requests or deployment keys."""
import asyncio
import base64
import json
import struct
import zlib
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.database import get_db
from app.api.dependencies import get_current_user
from app.api.settings import router
from app.models import User
from app.services.model_probe_service import ModelProbeService as Probes
from app.services.settings_service import SettingsService, AgriAIConfig, LLMConfig
from app.services.vision_service import VisionService
from app.middleware.audit_context import AuditContextMiddleware


def response(status=200, data=None, *, raw=None, headers=None):
    body = raw if raw is not None else json.dumps(data).encode()
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


@pytest.fixture
def transport(monkeypatch, controlled_dns):
    controlled_dns({'provider.example': ['93.184.216.34'], 'other.example': ['93.184.216.35']})
    calls = []
    def default(request):
        if request.method == 'GET': return response(data={'data': [{'id': 'synthetic-model'}]})
        return response(data={'choices': [{'message': {'content': 'OK'}}]})
    handler = [default]
    async def serve(request):
        calls.append(request)
        answer = handler[0](request)
        return await answer if asyncio.iscoroutine(answer) else answer
    monkeypatch.setattr(Probes, 'create_client', staticmethod(lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(serve), timeout=timeout, follow_redirects=False, trust_env=False)))
    return SimpleNamespace(calls=calls, handler=handler)


def config(**values):
    return {'api_key': 'synthetic-platform-key', 'base_url': 'https://provider.example/v1', 'model_name': 'synthetic-model', **values}


def test_synthetic_png_has_actual_random_quadrants_and_no_answer_in_prompt():
    data, expected = Probes.image_sample()
    png = base64.b64decode(data.split(',', 1)[1])
    assert png[:8] == b'\x89PNG\r\n\x1a\n' and len(set(expected)) == 4
    palette = {'RED': (255, 0, 0), 'GREEN': (0, 255, 0), 'BLUE': (0, 0, 255),
               'YELLOW': (255, 255, 0), 'MAGENTA': (255, 0, 255), 'CYAN': (0, 255, 255)}
    offset = 8; packed = b''
    while offset < len(png):
        size = struct.unpack('!I', png[offset:offset + 4])[0]
        kind = png[offset + 4:offset + 8]; value = png[offset + 8:offset + 8 + size]
        assert zlib.crc32(kind + value) & 0xffffffff == struct.unpack('!I', png[offset + 8 + size:offset + 12 + size])[0]
        if kind == b'IDAT': packed += value
        offset += 12 + size
    raw = zlib.decompress(packed)
    for index, (x, y) in enumerate(((10, 10), (90, 10), (10, 90), (90, 90))):
        assert tuple(raw[y * 385 + 1 + x * 3:y * 385 + 1 + x * 3 + 3]) == palette[expected[index]]


@pytest.mark.asyncio
async def test_live_list_is_deduplicated_bounded_and_never_static_fallback(transport, monkeypatch):
    transport.handler[0] = lambda request: response(data={'data': [{'id': 'z'}, {'id': 'a'}, {'id': 'z'}], 'has_more': True})
    result = await Probes.discover(config())
    assert result['success'] and result['models'] == ['a', 'z'] and result['truncated']
    assert transport.calls[0].url.path == '/v1/models'
    assert transport.calls[0].headers['authorization'] == 'Bearer synthetic-platform-key'
    monkeypatch.setattr(Probes, 'MAX_MODELS', 1)
    assert (await Probes.discover(config()))['models'] == ['z']
    transport.handler[0] = lambda request: response(404, {'error': 'synthetic-platform-key'})
    result = await Probes.discover(config())
    assert not result['success'] and result['models'] == [] and 'synthetic-platform-key' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://provider.example/v1', 'https://localhost/v1', 'https://127.0.0.1/v1',
    'https://user:pass@provider.example/v1', 'https://provider.example/v1?api_key=fixture',
    'https://provider.example/v1#fragment', 'https://provider.example/v1/chat/completions', 'https://provider.example/v1/models'])
async def test_unsafe_or_ambiguous_roots_never_send_credentials(transport, url):
    result = await Probes.discover(config(base_url=url))
    assert not result['success'] and not transport.calls
    assert 'synthetic-platform-key' not in json.dumps(result)


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_upstream_errors_are_not_echoed(transport):
    transport.handler[0] = lambda request: response(302, {'error': 'synthetic-platform-key'}, headers={'Location': 'https://other.example/v1/models'})
    result = await Probes.discover(config())
    assert len(transport.calls) == 1 and not result['success'] and result['code'] == 'http_302'
    assert 'synthetic-platform-key' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [{'data': 'wrong'}, {'data': [{'id': None}]}, {'data': [{'id': 'x\nforge'}]}, ['wrong']])
async def test_malformed_list_does_not_become_valid_models(transport, data):
    transport.handler[0] = lambda request: response(data=data)
    result = await Probes.discover(config())
    assert not result['success'] and result['models'] == [] and result['code'] == 'invalid_response'


@pytest.mark.asyncio
async def test_body_limit_and_overall_deadline_fail_without_retry(transport, monkeypatch):
    monkeypatch.setattr(Probes, 'MAX_BYTES', 10)
    transport.handler[0] = lambda request: response(raw=b'x' * 11)
    assert (await Probes.discover(config()))['code'] == 'response_too_large'
    monkeypatch.setattr(Probes, 'TOTAL_SECONDS', 0.2)
    async def slow(request):
        await asyncio.sleep(0.5)
        return response(data={'data': []})
    transport.handler[0] = slow
    before = len(transport.calls)
    assert (await Probes.discover(config()))['code'] == 'timeout'
    assert len(transport.calls) == before + 1


@pytest.mark.asyncio
async def test_list_unavailable_still_tests_handwritten_model_and_text_only_is_not_vision(transport):
    def handler(request):
        if request.method == 'GET': return response(404, {})
        assert json.loads(request.content)['model'] == 'manually-entered-model'
        return response(data={'choices': [{'message': {'content': 'text response'}}]})
    transport.handler[0] = handler
    result = await Probes.test(config(model_name='manually-entered-model'), 'decision')
    assert result['success'] and result['vision'] == 'not_tested' and len(transport.calls) == 2
    assert [row['state'] for row in result['checks']] == ['unverified', 'passed']


@pytest.mark.asyncio
async def test_successful_ping_cannot_pass_visual_test_but_real_image_sample_can(transport, monkeypatch):
    expected = ['RED', 'CYAN', 'GREEN', 'BLUE']
    monkeypatch.setattr('app.services.model_probe_service.random.SystemRandom', lambda: SimpleNamespace(sample=lambda values, count: expected))
    image, actual_order = Probes.image_sample()
    assert actual_order == expected
    monkeypatch.setattr(Probes, 'image_sample', staticmethod(lambda: (image, expected)))
    result = await Probes.test(config(), 'vision')
    assert not result['success'] and result['vision'] == 'accepted_unverified'
    assert result['checks'][-1]['code'] == 'sample_mismatch'
    payload = json.loads(transport.calls[-1].content)
    assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    assert ', '.join(expected) not in payload['messages'][0]['content'][0]['text']
    def success(request):
        if request.method == 'GET': return response(data={'data': []})
        payload = json.loads(request.content)
        answer = ', '.join(expected) if isinstance(payload['messages'][0]['content'], list) else 'OK'
        return response(data={'choices': [{'message': {'content': answer}}]})
    transport.handler[0] = success
    result = await Probes.test(config(), 'vision')
    assert result['success'] and result['vision'] == 'sample_verified'
    assert len(result['checks']) == 3 and '病害' in result['message']
    passed, message = await VisionService.test_connection('synthetic-platform-key', 'https://provider.example/v1', 'synthetic-model')
    assert passed and '病害' in message


@pytest.mark.asyncio
async def test_color_explanations_and_json_pass_but_reordered_or_echoed_colors_do_not(transport, monkeypatch):
    expected = ['RED', 'CYAN', 'GREEN', 'BLUE']
    image, _ = Probes.image_sample()
    monkeypatch.setattr(Probes, 'image_sample', staticmethod(lambda: (image, expected)))
    for answer, passed in [('The colors are **RED**, CYAN, GREEN, BLUE.', True),
                           ('{"colors":["RED","CYAN","GREEN","BLUE"]}', True),
                           ('RED, GREEN, CYAN, BLUE', False),
                           ('RED GREEN BLUE YELLOW MAGENTA CYAN', False), ('I cannot see images.', False)]:
        def handler(request):
            if request.method == 'GET': return response(data={'data': []})
            payload = json.loads(request.content)
            content = answer if isinstance(payload['messages'][0]['content'], list) else 'OK'
            return response(data={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
        transport.handler[0] = handler
        result = await Probes.test(config(), 'vision')
        assert result['success'] is passed
        assert result['vision'] == ('sample_verified' if passed else 'accepted_unverified')


@pytest.mark.asyncio
async def test_siliconflow_kimi_probe_uses_short_instant_calls_and_chat_model_list(transport, monkeypatch, controlled_dns):
    controlled_dns({'api.siliconflow.cn': ['93.184.216.34']})
    expected = ['RED', 'CYAN', 'GREEN', 'BLUE']
    image, _ = Probes.image_sample()
    monkeypatch.setattr(Probes, 'image_sample', staticmethod(lambda: (image, expected)))
    def handler(request):
        if request.method == 'GET':
            assert request.url.params['sub_type'] == 'chat'
            return response(data={'data': [{'id': 'Pro/moonshotai/Kimi-K2.6'}]})
        payload = json.loads(request.content)
        assert payload['enable_thinking'] is False
        is_image = isinstance(payload['messages'][0]['content'], list)
        assert payload['max_tokens'] == (512 if is_image else 256)
        return response(data={'choices': [{'message': {'content': ', '.join(expected) if is_image else 'OK'}}]},
                        headers={'x-siliconcloud-trace-id': 'sf-synthetic-trace'})
    transport.handler[0] = handler
    result = await Probes.test(config(base_url='https://api.siliconflow.cn/v1', model_name='Pro/moonshotai/Kimi-K2.6'), 'vision')
    assert result['success'] and result['vision'] == 'sample_verified'
    assert result['checks'][-1]['upstream_trace_id'] == 'sf-synthetic-trace'
    assert len(transport.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('finish,content,reasoning,expected_code', [
    ('length', 'RED', '', 'output_truncated'), ('stop', '', 'thinking only', 'reasoning_without_answer')
])
async def test_truncation_and_reasoning_only_are_explained_without_claiming_vision_support(transport, finish, content, reasoning, expected_code):
    def handler(request):
        if request.method == 'GET': return response(data={'data': []})
        payload = json.loads(request.content)
        if not isinstance(payload['messages'][0]['content'], list):
            return response(data={'choices': [{'message': {'content': 'OK'}}]})
        return response(data={'choices': [{'message': {'content': content, 'reasoning_content': reasoning}, 'finish_reason': finish}]})
    transport.handler[0] = handler
    result = await Probes.test(config(), 'vision')
    assert not result['success'] and result['vision'] == 'accepted_unverified'
    assert result['checks'][-1]['code'] == expected_code


@pytest.mark.asyncio
async def test_slow_or_offline_model_list_does_not_block_selected_model_call(transport, monkeypatch):
    monkeypatch.setattr(Probes, 'LIST_SECONDS', .01)
    async def handler(request):
        if request.method == 'GET': await asyncio.sleep(.05)
        return response(data={'choices': [{'message': {'content': 'OK'}}]})
    transport.handler[0] = handler
    result = await Probes.test(config(), 'decision')
    assert result['success']
    assert result['checks'][0]['state'] == 'unverified' and result['checks'][0]['code'] == 'timeout'
    assert result['checks'][1]['name'] == 'text_call' and len(transport.calls) == 2


@pytest.mark.asyncio
async def test_capability_metadata_and_name_hints_are_separate_and_no_model_inference_is_triggered(transport):
    rows = [
        {'id': 'declared', 'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']}},
        {'id': 'text-only', 'input_modalities': ['text']},
        {'id': 'Pro/moonshotai/Kimi-K2.6'}, {'id': 'moonshotai/Kimi-K2-Instruct'},
        {'id': 'Qwen/Qwen3-VL-8B-Instruct', 'supports_vision': False},
        {'id': 'ambiguous', 'supports_vision': True, 'input_modalities': ['text']},
        {'id': 'generator', 'input_modalities': ['text', 'image'], 'output_modalities': ['image']},
        {'id': 'unknown-gateway-model'},
    ]
    transport.handler[0] = lambda request: response(data={'data': rows})
    result = await Probes.discover(config())
    assert result['success'] and len(transport.calls) == 1 and transport.calls[0].method == 'GET'
    details = {item['id']: item for item in result['model_details']}
    assert details['declared']['image_input'] == 'supported' and details['declared']['evidence'] == 'metadata'
    assert details['Pro/moonshotai/Kimi-K2.6']['image_input'] == 'unknown'
    assert details['Pro/moonshotai/Kimi-K2.6']['vision_candidate']
    for model in ['text-only', 'Qwen/Qwen3-VL-8B-Instruct', 'generator']:
        assert details[model]['image_input'] == 'unsupported' and not details[model]['vision_candidate']
    for model in ['moonshotai/Kimi-K2-Instruct', 'unknown-gateway-model', 'ambiguous']:
        assert details[model]['image_input'] == 'unknown' and not details[model]['vision_candidate']


@pytest.mark.asyncio
async def test_failure_trace_is_safe_and_not_reused_from_previous_stage(transport):
    def handler(request):
        if request.method == 'GET':
            return response(data={'data': []}, headers={'x-siliconcloud-trace-id': 'list-only-trace'})
        raise httpx.ReadTimeout('synthetic timeout', request=request)
    transport.handler[0] = handler
    result = await Probes.test(config(), 'vision')
    assert result['checks'][0]['upstream_trace_id'] == 'list-only-trace'
    assert 'upstream_trace_id' not in result['checks'][1]
    assert result['checks'][1]['name'] == 'text_call' and result['vision'] == 'not_tested'
    transport.handler[0] = lambda request: response(403, {}, headers={'x-siliconcloud-trace-id': 'synthetic-platform-key'})
    result = await Probes.test(config(), 'decision')
    assert 'synthetic-platform-key' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 500])
async def test_status_failures_are_separate_from_valid_text_or_image_content(transport, status):
    transport.handler[0] = lambda request: response(status, {'error': 'synthetic-platform-key must not leak'})
    result = await Probes.test(config(), 'vision')
    assert not result['success'] and result['vision'] == 'not_tested'
    assert result['authentication'] == ('failed' if status == 401 else 'unknown')
    assert result['checks'][-1]['code'] == 'http_' + str(status)
    assert 'synthetic-platform-key' not in json.dumps(result)


@pytest.mark.asyncio
async def test_200_without_valid_message_and_missing_key_do_not_pass(transport):
    transport.handler[0] = lambda request: response(data={'choices': []}) if request.method == 'POST' else response(data={'data': []})
    result = await Probes.test(config(), 'decision')
    assert not result['success'] and result['checks'][-1]['code'] == 'invalid_response'
    transport.calls.clear()
    assert not (await Probes.test(config(api_key=''), 'decision'))['success']
    assert not transport.calls


@pytest.mark.asyncio
async def test_persisted_endpoint_changes_do_not_carry_old_keys_and_no_probe_saves_drafts(test_session, transport):
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(llm_api_key='synthetic-decision-key',
        llm_base_url='https://provider.example/v1', llm_model='old-text', vision_api_key='synthetic-vision-key',
        vision_base_url='https://provider.example/v1', vision_model='old-vision'))
    admin = User(username='probe-admin', role='admin', password_hash='unused', is_active=True)
    test_session.add(admin); await test_session.commit()
    app = FastAPI(); app.include_router(router); app.add_middleware(AuditContextMiddleware)
    async def db(): yield test_session
    async def current(): return admin
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = current
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        for kind in ('decision', 'vision'):
            result = await client.post('/api/settings/model-discovery', json={'kind': kind, 'base_url': 'https://other.example/v1'})
            assert result.status_code == 200 and not result.json()['success'] and not transport.calls
        first = await client.post('/api/settings/model-discovery', json={'kind': 'decision'})
        assert first.status_code == 200 and first.headers['cache-control'] == 'private, no-store'
        assert first.json()['request_id'] == first.headers['x-request-id']
        assert transport.calls[-1].headers['authorization'] == 'Bearer synthetic-decision-key'
        draft = await client.post('/api/settings/model-test', json={'kind': 'decision', 'api_key': 'synthetic-new-key',
            'base_url': 'https://other.example/v1', 'model': 'draft-model'})
        assert draft.status_code == 200 and draft.json()['success']
        saved = await SettingsService.get_agri_ai_config(test_session)
        assert saved.llm_model == 'old-text' and saved.llm_api_key == 'synthetic-decision-key'
        changed = await client.put('/api/settings', json={'agri_ai': {'llm_base_url': 'https://other.example/v1',
            'vision_base_url': 'https://other.example/v1', 'llm_model': 'chosen-new-text', 'vision_model': 'chosen-new-vision'}})
        assert changed.status_code == 200 and not changed.json()['agri_ai']['has_llm_key'] and not changed.json()['agri_ai']['has_vision_key']
        saved = await SettingsService.get_agri_ai_config(test_session)
        assert saved.llm_api_key == '' and saved.vision_api_key == ''
        assert saved.llm_model == 'chosen-new-text' and saved.vision_model == 'chosen-new-vision'
        admin.role = 'expert'
        for path in ('model-discovery', 'model-test'):
            assert (await client.post('/api/settings/' + path, json={'kind': 'decision'})).status_code == 403


@pytest.mark.asyncio
async def test_same_url_vision_can_reuse_decision_key_but_different_url_cannot(test_session):
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(llm_api_key='synthetic-text-key',
        llm_base_url='https://provider.example/v1/', vision_api_key='', vision_base_url='https://provider.example/v1'))
    assert (await SettingsService.resolve_agri_model(test_session, 'vision'))['api_key'] == 'synthetic-text-key'
    assert (await SettingsService.resolve_agri_model(test_session, 'vision', base_url='https://other.example/v1'))['api_key'] == ''
    assert SettingsService.endpoint_key('https://provider.example/v1', 'synthetic', ' https://provider.example/v1/ ', None) == 'synthetic'
    assert SettingsService.endpoint_key('https://provider.example/v1', 'synthetic', 'https://other.example/v1', None) == ''


@pytest.mark.asyncio
async def test_shared_connection_and_save_do_not_rebind_stored_key_to_another_provider(test_session, transport):
    await SettingsService.set_llm_config(test_session, LLMConfig(provider='siliconflow', api_key='synthetic-shared-key',
        api_url='https://provider.example/v1', model='shared-model'))
    admin = User(username='shared-probe-admin', role='admin', password_hash='unused', is_active=True)
    test_session.add(admin); await test_session.commit()
    app = FastAPI(); app.include_router(router)
    async def db(): yield test_session
    async def current(): return admin
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = current
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        changed = await client.post('/api/settings/test-connection', json={'provider': 'openai', 'api_url': 'https://other.example/v1', 'model': 'manual-model'})
        assert changed.status_code == 200 and not changed.json()['success'] and not transport.calls
        same = await client.post('/api/settings/test-connection', json={'provider': 'siliconflow', 'api_url': 'https://provider.example/v1', 'model': 'shared-model'})
        assert same.status_code == 200 and same.json()['success']
        assert transport.calls[-1].headers['authorization'] == 'Bearer synthetic-shared-key'
        saved = await client.put('/api/settings', json={'llm': {'provider': 'openai', 'api_url': 'https://other.example/v1', 'model': 'manual-model'}})
        assert saved.status_code == 200 and not saved.json()['llm']['has_api_key']
    assert (await SettingsService.get_llm_config(test_session)).api_key == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['llm', 'vision'])
@pytest.mark.parametrize('submitted', [None, ''])
async def test_changed_address_requires_explicit_model_without_partial_save(test_session, transport, kind, submitted):
    await SettingsService.set_agri_ai_config(test_session, AgriAIConfig(llm_api_key='synthetic-old-key',
        llm_base_url='https://provider.example/v1', vision_base_url='https://provider.example/v1'))
    before = await SettingsService.get_setting(test_session, SettingsService.KEY_AGRI_AI_CONFIG)
    admin = User(username='model-guard-admin', role='admin', password_hash='unused', is_active=True)
    test_session.add(admin); await test_session.commit()
    app = FastAPI(); app.include_router(router)
    async def db(): yield test_session
    async def current(): return admin
    app.dependency_overrides[get_db] = db; app.dependency_overrides[get_current_user] = current
    body = {kind + '_base_url': 'https://other.example/v1'}
    if submitted is not None: body[kind + '_model'] = submitted
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        result = await client.put('/api/settings', json={'agri_ai': body, 'rag': {'top_k': 9}})
        assert result.status_code == 422
    assert await SettingsService.get_setting(test_session, SettingsService.KEY_AGRI_AI_CONFIG) == before
    assert await SettingsService.get_setting(test_session, SettingsService.KEY_RAG_CONFIG) is None
    changed = await SettingsService.resolve_agri_model(test_session, 'decision', api_key='synthetic-new-key', base_url='https://other.example/v1')
    assert changed['model_name'] == ''
