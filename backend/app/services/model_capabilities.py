"""Interpret provider-declared input capabilities; names are candidates, never proof."""
import re
from urllib.parse import urlsplit


def siliconflow_root(base_url):
    parsed = urlsplit(base_url)
    return (parsed.hostname in ('api.siliconflow.cn', 'api.siliconflow.com')
            and parsed.port in (None, 443) and parsed.path.rstrip('/') == '/v1')


def model_list_path(base_url):
    # SiliconFlow type=image means image generation, not visual understanding.
    return '/models?sub_type=chat' if siliconflow_root(base_url) else '/models'


def siliconflow_instant_options(base_url, model):
    # Use the same instant mode for probes and interactive leaf diagnosis.
    if siliconflow_root(base_url) and re.search(r'(?:^|/)kimi-k2\.(?:5|6)$', model.lower()):
        return {'enable_thinking': False}
    return {}


_VISION_NAME = re.compile(
    r'qwen\d+(?:\.\d+)*-(?:vl|omni)(?:-|$)|internvl(?:\d|\b)|deepseek-?vl(?:\d|\b)|'
    r'glm-4(?:\.\d+)?v(?:-|$)|kimi-k2\.(?:5|6)(?:-|$)|kimi-k2\.7-code(?:-|$)|'
    r'(?:^|[/_-])(?:vision|vlm)(?:[-_/.]|$)', re.IGNORECASE)


def image_capability(row):
    declarations = []
    architecture = row.get('architecture') if isinstance(row.get('architecture'), dict) else {}
    capabilities = row.get('capabilities')
    for source in (row, architecture):
        values = source.get('input_modalities')
        if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
            declarations.append('image' in [value.lower() for value in values])
        for key in ('supports_vision', 'supports_image_input'):
            if isinstance(source.get(key), bool): declarations.append(source[key])
    if isinstance(capabilities, dict):
        for key in ('vision', 'image_input'):
            if isinstance(capabilities.get(key), bool): declarations.append(capabilities[key])
    elif isinstance(capabilities, list) and any(value in ('vision', 'image_input') for value in capabilities):
        declarations.append(True)
    modality = architecture.get('modality')
    if isinstance(modality, str) and '->' in modality:
        inputs, outputs = modality.lower().split('->', 1)
        if 'text' not in outputs.split('+'):
            return 'unsupported', 'metadata', '平台声明不支持文本输出，不能用于视觉聊天诊断'
        declarations.append('image' in inputs.split('+'))
    outputs = architecture.get('output_modalities', row.get('output_modalities'))
    if isinstance(outputs, list) and outputs and all(isinstance(value, str) for value in outputs) and 'text' not in [value.lower() for value in outputs]:
        return 'unsupported', 'metadata', '平台声明不支持文本输出，不能用于视觉聊天诊断'
    if declarations:
        if all(declarations): return 'supported', 'metadata', '平台声明支持图像输入，仍需测试所选模型'
        if not any(declarations): return 'unsupported', 'metadata', '平台声明未支持视觉聊天输入或文本输出'
        return 'unknown', 'metadata', '平台能力信息不一致，请核实后测试'
    if _VISION_NAME.search(row['id']):
        return 'unknown', 'model_name', '名称符合视觉模型系列，仅作为候选，图像能力待测试'
    return 'unknown', 'unspecified', '平台未提供可确认的图像能力信息'


def model_details(data, ids):
    grouped = {}
    for row in data['data'][:500]:
        if isinstance(row, dict) and row.get('id') in ids:
            grouped.setdefault(row['id'], []).append(image_capability(row))
    result = []
    for model in ids:
        declarations = grouped.get(model, [])
        info = declarations[0] if declarations else ('unknown', 'unspecified', '平台未提供图像能力信息')
        if any(value != info for value in declarations):
            info = ('unknown', 'metadata', '同一模型的能力声明不一致，请核实后测试')
        state, evidence, message = info
        result.append({'id': model, 'image_input': state, 'evidence': evidence,
                       'vision_candidate': state == 'supported' or evidence == 'model_name', 'message': message})
    return result
