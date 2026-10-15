"""Explicit, bounded OpenAI-compatible probes; no raw upstream errors or credentials returned."""
import asyncio
import base64
import json
import random
import re
import struct
import zlib
from urllib.parse import urlsplit
import httpx
from app.utils.security import validate_safe_external_url
from app.services.model_capabilities import model_list_path, model_details, siliconflow_instant_options


class ProbeFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class ModelProbeService:
    MAX_BYTES = 1024 * 1024
    MAX_MODELS = 500
    TOTAL_SECONDS = 80
    LIST_SECONDS = 5
    CALL_SECONDS = 35

    @staticmethod
    def create_client(timeout):
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)

    @staticmethod
    async def validate(base_url):
        value = base_url.strip().rstrip('/')
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProbeFailure('invalid_url', '请填写不含账号、查询参数或片段的HTTPS API根地址')
        if parsed.path.endswith(('/models', '/chat/completions')):
            raise ProbeFailure('invalid_url', '请填写API根地址，不要填写models或chat/completions端点')
        safe, _ = await asyncio.to_thread(validate_safe_external_url, value)
        if not safe: raise ProbeFailure('unsafe_url', 'API地址未通过外部地址安全检查')
        return value

    @classmethod
    async def request(cls, client, method, base_url, key, path, payload=None, trace=None):
        if trace is not None: trace.clear()
        async with client.stream(method, base_url + path, headers={'Authorization': 'Bearer ' + key, 'Accept-Encoding': 'identity'}, json=payload) as response:
            if trace is not None:
                value = response.headers.get('x-siliconcloud-trace-id', '')
                if re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) and key not in value:
                    trace['upstream_trace_id'] = value
            if response.status_code != 200:
                code = response.status_code
                messages = {400: '平台拒绝请求参数，请核对完整模型ID、兼容接口及图像输入格式',
                            401: '平台拒绝鉴权', 403: '平台拒绝此操作，请核对账号状态、权限与额度', 404: '平台未提供此端点或模型，请核对API根地址和完整模型ID',
                            429: '平台限流，本次未通过'}
                # Never echo provider bodies; some gateways echo credentials and submitted messages.
                raise ProbeFailure('http_' + str(code), messages.get(code, '平台请求失败（HTTP ' + str(code) + '）'))
            chunks = bytearray()
            async for chunk in response.aiter_raw():
                if len(chunks) + len(chunk) > cls.MAX_BYTES:
                    raise ProbeFailure('response_too_large', '平台响应超过1MiB上限')
                chunks.extend(chunk)
        try:
            data = json.loads(chunks)
        except (ValueError, RecursionError):
            raise ProbeFailure('invalid_response', '平台响应不是有效JSON') from None
        if not isinstance(data, dict): raise ProbeFailure('invalid_response', '平台响应结构不符合兼容接口')
        return data

    @classmethod
    def model_ids(cls, data):
        rows = data.get('data')
        if not isinstance(rows, list): raise ProbeFailure('invalid_response', '模型列表缺少data数组')
        ids = []; seen = set()
        for row in rows[:cls.MAX_MODELS]:
            value = row.get('id') if isinstance(row, dict) else None
            if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
                raise ProbeFailure('invalid_response', '模型列表包含无效模型ID')
            if value not in seen: ids.append(value); seen.add(value)
        return sorted(ids), len(rows) > cls.MAX_MODELS or bool(data.get('has_more'))

    @staticmethod
    def content(data):
        try:
            choice = data['choices'][0]
            content = choice['message']['content']
        except (KeyError, IndexError, TypeError):
            raise ProbeFailure('invalid_response', '平台未返回有效文本消息') from None
        if choice.get('finish_reason') == 'length':
            raise ProbeFailure('output_truncated', '模型输出达到长度上限，样例尚未完整回答；不能据此判断模型不支持图片')
        if not isinstance(content, str) or not content.strip():
            if choice['message'].get('reasoning_content') or choice['message'].get('reasoning'):
                raise ProbeFailure('reasoning_without_answer', '模型仅返回思考内容，未返回最终回答；请使用非思考模式或调整输出预算')
            raise ProbeFailure('invalid_response', '平台返回空文本或不兼容消息格式')
        return content.strip()

    @staticmethod
    def sample_colors(content):
        # Ignore benign explanations/Markdown while requiring exactly four color
        # names in order. Do not accept a response echoing all allowed colors.
        return re.findall(r'\b(?:RED|GREEN|BLUE|YELLOW|MAGENTA|CYAN)\b', content.upper())

    @staticmethod
    def image_sample():
        # Four quadrants in an unpredictable order; expected answer is never included in the prompt.
        palette = {'RED': (255, 0, 0), 'GREEN': (0, 255, 0), 'BLUE': (0, 0, 255),
                   'YELLOW': (255, 255, 0), 'MAGENTA': (255, 0, 255), 'CYAN': (0, 255, 255)}
        names = random.SystemRandom().sample(list(palette), 4)
        size = 128
        raw = bytearray()
        for y in range(size):
            raw.append(0)
            for x in range(size): raw.extend(palette[names[(2 if y >= 64 else 0) + (1 if x >= 64 else 0)]])
        def chunk(kind, value):
            return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value) & 0xffffffff)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', size, size, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
        return 'data:image/png;base64,' + base64.b64encode(png).decode(), names

    @staticmethod
    def failure(error):
        if isinstance(error, ProbeFailure): return error.code, error.message
        if isinstance(error, (TimeoutError, httpx.TimeoutException)): return 'timeout', '连接测试超时，本次未通过'
        if isinstance(error, httpx.HTTPError): return 'network_error', '网络或TLS连接失败，本次未通过'
        return 'probe_error', '连接测试未完成，请按请求ID核查'

    @classmethod
    async def discover(cls, config):
        result = {'success': False, 'message': '', 'models': [], 'model_details': [], 'truncated': False, 'source': 'live_api', 'code': ''}
        try:
            async with asyncio.timeout(min(cls.TOTAL_SECONDS, 24)):
                base = await cls.validate(config['base_url'])
                if not config['api_key']: raise ProbeFailure('missing_key', '此地址没有可用密钥；切换地址后请明确输入该平台密钥')
                async with cls.create_client(10) as client:
                    data = await cls.request(client, 'GET', base, config['api_key'], model_list_path(base))
                result['models'], result['truncated'] = cls.model_ids(data)
                result['model_details'] = model_details(data, result['models'])
                result.update(success=True, message='已读取平台本次返回的模型ID；列表不证明模型用途或图像能力', code='ok')
        except Exception as error:
            result['code'], result['message'] = cls.failure(error)
        return result

    @classmethod
    async def test(cls, config, kind):
        result = {'success': False, 'message': '', 'checks': [], 'authentication': 'unknown', 'vision': 'not_tested'}
        stage = 'configuration'
        trace = {}
        def checked(name, state, code, message):
            result['checks'].append({'name': name, 'state': state, 'code': code, 'message': message, **trace})
        try:
            async with asyncio.timeout(cls.TOTAL_SECONDS):
                base = await cls.validate(config['base_url'])
                if not config['api_key']: raise ProbeFailure('missing_key', '此地址没有可用密钥；切换地址后请明确输入该平台密钥')
                if not config['model_name'].strip(): raise ProbeFailure('missing_model', '请先选择或手填模型名称')
                async with cls.create_client(cls.CALL_SECONDS) as client:
                    stage = 'model_list'
                    try:
                        async with asyncio.timeout(cls.LIST_SECONDS):
                            data = await cls.request(client, 'GET', base, config['api_key'], model_list_path(base), trace=trace)
                        ids, _ = cls.model_ids(data)
                        result['authentication'] = 'verified'
                        if ids and config['model_name'] not in ids:
                            checked(stage, 'unverified', 'model_not_listed', '鉴权已通过，但所选ID不在本次列表中；请核对完整平台前缀，仍继续测试手填模型')
                        else:
                            checked(stage, 'passed', 'ok', '鉴权后的模型列表响应结构有效')
                    except ProbeFailure as error:
                        if error.code == 'http_401': raise
                        checked(stage, 'unverified', error.code, error.message + '；仍可手填模型并测试调用')
                    except (TimeoutError, httpx.HTTPError) as error:
                        code, message = cls.failure(error)
                        checked(stage, 'unverified', code, message + '；模型列表不可用，继续测试所选模型')
                    stage = 'text_call'
                    payload = {'model': config['model_name'], 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                               'max_tokens': 256, 'stream': False, **siliconflow_instant_options(base, config['model_name'])}
                    async with asyncio.timeout(cls.CALL_SECONDS):
                        cls.content(await cls.request(client, 'POST', base, config['api_key'], '/chat/completions', payload, trace))
                    result['authentication'] = 'verified'
                    checked(stage, 'passed', 'ok', '所选模型已返回有效文本消息')
                    if kind == 'vision':
                        stage = 'image_call'
                        image, expected = cls.image_sample()
                        prompt = ('Read the attached image. Report the colors of its four quadrants in this order: '
                                  'top-left, top-right, bottom-left, bottom-right. Use only four words separated by commas. '
                                  'Allowed words: RED, GREEN, BLUE, YELLOW, MAGENTA, CYAN.')
                        payload['messages'] = [{'role': 'user', 'content': [{'type': 'text', 'text': prompt},
                            {'type': 'image_url', 'image_url': {'url': image}}]}]
                        payload['max_tokens'] = 512
                        async with asyncio.timeout(cls.CALL_SECONDS):
                            content = cls.content(await cls.request(client, 'POST', base, config['api_key'], '/chat/completions', payload, trace))
                        if cls.sample_colors(content) != expected:
                            result['vision'] = 'accepted_unverified'
                            raise ProbeFailure('sample_mismatch', '图像请求已响应，但合成样例校验未通过；不认定图像能力已验证')
                        result['vision'] = 'sample_verified'
                        checked(stage, 'passed', 'ok', '单次合成图像输入及颜色顺序校验通过；不代表病害识别效果')
                    result['success'] = True
                    result['message'] = '所选模型文本调用通过' if kind != 'vision' else '单次合成图像测试通过；病害效果仍需真实样本验收'
        except Exception as error:
            code, message = cls.failure(error)
            if code == 'http_401': result['authentication'] = 'failed'
            if stage == 'image_call' and result['vision'] != 'accepted_unverified':
                result['vision'] = 'accepted_unverified' if code in ('output_truncated', 'reasoning_without_answer') else 'failed'
            checked(stage, 'failed', code, message)
            result['message'] = message
        return result
