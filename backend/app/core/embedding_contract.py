"""Non-secret index identity, distinct from the private credential scope used by caches."""
import hashlib
import hmac
import json
from urllib.parse import urlsplit


def fingerprint(contract):
    return hashlib.sha256(json.dumps(contract, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def make_contract(config, rag):
    parsed = urlsplit(config.api_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Embedding root cannot contain credentials or query parameters')
    from app.config import settings
    scope_key = settings.SETTINGS_ENCRYPTION_KEY or settings.JWT_SECRET_KEY
    credential_scope = hmac.new(scope_key.encode(), b'embedding-scope-v1:' + config.api_key.encode(), hashlib.sha256).hexdigest()
    return {'version': 1, 'provider': config.provider, 'api_url': config.api_url.strip().rstrip('/'),
            'credential_scope': credential_scope,
            'model': config.model, 'dimension': config.dimension,
            'request_dimensions': getattr(config, 'request_dimensions', False),
            'chunk_size': rag.chunk_size, 'chunk_overlap': rag.chunk_overlap, 'chunk_max_per_item': 10,
            'text_layout': 'question-newline-answer-v1'}


def validate_contract(contract):
    if contract is None: return None
    if not isinstance(contract, dict) or set(contract) != {'version', 'provider', 'api_url', 'model', 'dimension',
            'request_dimensions', 'chunk_size', 'chunk_overlap', 'chunk_max_per_item', 'text_layout', 'credential_scope'}:
        raise ValueError('Invalid embedding contract')
    if contract['version'] != 1 or contract['text_layout'] != 'question-newline-answer-v1':
        raise ValueError('Unsupported embedding contract')
    for key in ('provider', 'api_url', 'model'):
        if not isinstance(contract[key], str) or not contract[key] or len(contract[key]) > 2048:
            raise ValueError('Invalid embedding identity')
    parsed = urlsplit(contract['api_url'])
    if parsed.username or parsed.password or parsed.query or parsed.fragment: raise ValueError('Unsafe contract URL')
    if not isinstance(contract['credential_scope'], str) or len(contract['credential_scope']) != 64:
        raise ValueError('Invalid credential scope')
    for key in ('dimension', 'chunk_size', 'chunk_overlap', 'chunk_max_per_item'):
        if type(contract[key]) is not int or contract[key] < 0: raise ValueError('Invalid contract geometry')
    if not 0 < contract['dimension'] <= 65536 or not 0 < contract['chunk_size'] or not contract['chunk_overlap'] < contract['chunk_size']:
        raise ValueError('Invalid contract geometry')
    if type(contract['request_dimensions']) is not bool: raise ValueError('Invalid dimension request policy')
    return dict(contract)
