# -*- coding: utf-8 -*-
"""
AgriMind 真实多模态大模型病害视觉诊断服务 (VisionService)

特性:
1. 真实接入多模态视觉大模型 (默认对接 SiliconFlow Qwen/Qwen3-VL-8B-Instruct)
2. 真正读取农户现场上传的病叶照片，执行深度视觉表型提取 (颜色、轮廓、霉层、轮纹、坏死斑)
3. 图像文件头、大小与分辨率检查：触发应用复核条件，不证明植物内容真实性
4. 多条件复核门限：保留模型原始评分，风险条件只影响是否建议专家复核
5. 绝无硬编码 API Key，严格通过环境变量或系统配置安全读取
6. 完善的离线/异常保守降级机制，绝不伪造虚假高置信度
"""

import os
import asyncio
import re
import json
import base64
import struct
import logging
from app.core.operation_observation import observed_model, observe_operation, set_operation_outcome
from typing import Dict, Any, Optional, Tuple
import httpx

from app.config import settings
from app.core.weather_policy import finite_number
from app.services.model_capabilities import siliconflow_instant_options

logger = logging.getLogger(__name__)

# 默认 SiliconFlow 视觉配置（从环境变量或系统配置动态覆盖，严禁硬编码真实密钥）
DEFAULT_VISION_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_VISION_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
DEFAULT_VISION_API_KEY = ""


def parse_image_dimensions(img_bytes: bytes) -> Tuple[str, int, int]:
    """通过纯 Python 标准库解析常见图片二进制文件头获取长宽尺寸与格式"""
    if len(img_bytes) < 24:
        return "unknown", 0, 0

    # 1. PNG 格式头校验
    if img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            w, h = struct.unpack(">II", img_bytes[16:24])
            return "png", int(w), int(h)
        except Exception:
            return "png", 0, 0

    # 2. JPEG 格式头校验
    if img_bytes.startswith(b"\xff\xd8"):
        try:
            idx = 2
            length_bytes = len(img_bytes)
            while idx < length_bytes - 8:
                if img_bytes[idx] != 0xFF:
                    break
                marker = img_bytes[idx + 1]
                idx += 2
                # SOF0, SOF1, SOF2 markers
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", img_bytes[idx + 3:idx + 7])
                    return "jpeg", int(w), int(h)
                segment_len = struct.unpack(">H", img_bytes[idx:idx + 2])[0]
                idx += segment_len
            return "jpeg", 0, 0
        except Exception:
            return "jpeg", 0, 0

    # 3. WebP 格式
    if img_bytes.startswith(b"RIFF") and b"WEBP" in img_bytes[8:16]:
        try:
            if img_bytes[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", img_bytes[26:30])
                return "webp", int(w & 0x3FFF), int(h & 0x3FFF)
            elif img_bytes[12:16] == b"VP8L":
                b0, b1, b2, b3 = img_bytes[21:25]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0xF) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return "webp", int(w), int(h)
            return "webp", 0, 0
        except Exception:
            return "webp", 0, 0

    return "unknown", 0, 0


def preflight_image_quality(img_bytes: bytes, file_path: str) -> Dict[str, Any]:
    """前置图像质检：文件大小、真实分辨率、非实物合成图风险核验"""
    size_bytes = len(img_bytes)
    fmt, width, height = parse_image_dimensions(img_bytes)

    # 规则1: 当前应用的文件大小复核阈值；不能据此证明非植物或人工合成
    is_too_small_bytes = size_bytes < 3000

    # 规则2: 当前应用的分辨率复核阈值；不代表已验证的病理识别能力
    is_low_resolution = (width > 0 and width < 250) or (height > 0 and height < 250)

    # 规则3: 疑似人工合成/纯色几何简笔画
    suspicious_synthetic = is_too_small_bytes or is_low_resolution

    warning_reasons = []
    if is_too_small_bytes:
        warning_reasons.append(f"文件过小({size_bytes} 字节)")
    if is_low_resolution:
        warning_reasons.append(f"分辨率严重偏低({width}x{height}，低于应用复核阈值 250x250)")

    return {
        "format": fmt,
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "is_low_resolution": is_low_resolution,
        "suspicious_synthetic": suspicious_synthetic,
        "warning_reasons": warning_reasons,
    }


class VisionService:
    """真实多模态视觉诊断客户端"""

    CALL_SECONDS = 90

    @staticmethod
    def failed_result(image_path, crop_name, code, message):
        # A transport/configuration/format failure is not a disease diagnosis.
        logger.warning("视觉诊断未完成 code=%s", code)
        return {
            "image_url": image_path, "crop": crop_name,
            "diagnosis_status": "failed", "error_code": code,
            "suspected_disease": "本次图像诊断未完成", "confidence": None,
            "features_detected": [], "severity": "unknown", "need_expert": True,
            "description": message, "source_model": "Fallback",
        }

    @classmethod
    @observed_model("agri_vision_model")
    async def diagnose_leaf_image(
        cls,
        image_path: str,
        crop_name: str = "未提供",
        symptoms_hint: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        调用多模态大模型对病叶图片进行真实视觉诊断
        含前置图像质检、抗幻觉严格 Prompt 与后置多维安全联合闸门
        """
        key = api_key if api_key is not None else getattr(settings, "VISION_API_KEY", "") or getattr(settings, "LLM_API_KEY", "") or DEFAULT_VISION_API_KEY
        url = (base_url or getattr(settings, "VISION_BASE_URL", "") or os.getenv("VISION_BASE_URL") or DEFAULT_VISION_BASE_URL).rstrip("/")
        model = model_name or getattr(settings, "VISION_MODEL", "") or os.getenv("VISION_MODEL") or DEFAULT_VISION_MODEL

        # 检查图片是否存在并执行严格沙箱白名单校验（杜绝任意文件读取与目录穿越）
        allowed_dirs = [
            os.path.realpath(os.path.join(settings.DATA_DIR, "uploads")),
            os.path.realpath(os.path.join(settings.DATA_DIR, "private_uploads")),
            os.path.realpath(os.path.join(settings.BASE_DIR, "doc", "eval_images")),
        ]

        local_path = image_path
        if local_path.startswith("/api/diagnosis/media/"):
            # HTTP entry points have already authorized this opaque media id; internal eval paths stay separate.
            from app.services.diagnosis_media_service import DiagnosisMediaService
            name, _ = DiagnosisMediaService.parse(local_path)
            local_path = str(DiagnosisMediaService.path(name))
        elif local_path.startswith("/static/uploads/"):
            clean_rel = local_path.replace("/static/uploads/", "").lstrip("/\\")
            local_path = os.path.join(settings.DATA_DIR, "uploads", clean_rel)
        elif not os.path.isabs(local_path):
            candidate1 = os.path.join(settings.DATA_DIR, "uploads", local_path)
            candidate2 = os.path.join(settings.BASE_DIR, "doc", "eval_images", local_path)
            if os.path.exists(candidate1):
                local_path = candidate1
            elif os.path.exists(candidate2):
                local_path = candidate2
            else:
                local_path = candidate1

        real_path = os.path.realpath(local_path)
        is_safe_sandbox = False
        for d in allowed_dirs:
            if os.path.exists(d):
                try:
                    if os.path.commonpath([real_path, d]) == d:
                        is_safe_sandbox = True
                        break
                except ValueError:
                    pass

        valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
        ext = os.path.splitext(real_path)[1].lower()

        if not is_safe_sandbox or ext not in valid_exts or not os.path.exists(real_path) or not os.path.isfile(real_path):
            logger.warning(f"拒绝访问非法或越界病叶图片路径: {image_path} (解析路径: {real_path})")
            return {
                "image_url": image_path,
                "crop": crop_name,
                "suspected_disease": "未能获取合法图片文件",
                "confidence": None,
                "features_detected": [],
                "severity": "unknown",
                "need_expert": True,
                "description": "病叶照片路径不合法或超出允许目录，已拒绝处理，本次不能生成有效初诊，请重新上传现场照片。",
                "source_model": "None",
            }
        local_path = real_path

        # 读取二进制并执行前置质检
        try:
            with open(local_path, "rb") as f:
                img_bytes = f.read()

            preflight = preflight_image_quality(img_bytes, local_path)
            ext = os.path.splitext(local_path)[1].lower().replace(".", "")
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
            b64_data = f"data:{mime};base64," + base64.b64encode(img_bytes).decode("utf-8")
        except Exception as e:
            logger.error(f"读取图片编码失败: {e}")
            return {
                "image_url": image_path,
                "crop": crop_name,
                "suspected_disease": "图片解析失败",
                "confidence": None,
                "features_detected": [],
                "severity": "unknown",
                "need_expert": True,
                "description": f"图片编码异常: {str(e)}，请重新上传现场照片。",
                "source_model": "None",
            }

        # 若未配置有效 API Key，直接执行安全降级，绝不虚假发包
        if not key:
            logger.warning("未配置有效 VISION_API_KEY 或 LLM_API_KEY，视觉诊断执行安全降级")
            return cls.failed_result(image_path, crop_name, "missing_key",
                "系统尚未配置可用的视觉模型密钥，请联系管理员在系统设置中配置并保存后重新诊断。")

        # Ask for observations, never prescribe a score or claim professional credentials.
        prompt = f"""你是作物图像初步分析助手。作物信息：{crop_name}；补充描述：{symptoms_hint or '未提供'}。
只报告图片中可观察到的特征。作物未提供时不要默认番茄，不凭描述臆造病斑或病原体。
先判断是否包含植物主体，非植物、示意图或无法辨识时说明原因，并设置 need_expert=true。
真实离体叶片可以放在纯色背景上，不能仅因背景纯色判为非植物。
结论应标明疑似或无法判断，资料不足时要求补充清晰现场取样；不要给药剂剂量或编造标准。
confidence 如提供，只能是有限的0至1模型自评分，不是经校准正确率；没有有效评分时为null。
不得为某种病害、健康叶片、非植物或格式异常套用预设分数。保留实际观察特征，不编造特征凑数量。
severity 仅在有依据时填 low/medium/high/severe，否则 unknown。需要人工复核时 need_expert=true。
严格返回JSON对象，示例结构（示例值不作为默认诊断）：
{{"suspected_disease":"无法判断", "confidence":null, "features_detected":[], "severity":"unknown", "need_expert":true, "description":"说明观察依据和缺失资料"}}"""

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": b64_data}}
                    ]
                }
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
            "stream": False,
            **siliconflow_instant_options(url, model),
        }

        try:
            async with asyncio.timeout(cls.CALL_SECONDS), httpx.AsyncClient(
                timeout=httpx.Timeout(cls.CALL_SECONDS, connect=10.0),
                trust_env=False, follow_redirects=False,
            ) as client:
                with observe_operation("agri_vision_model_http") as measurement:
                    resp = await client.post(f"{url}/chat/completions", headers=headers, json=payload)
                    measurement.outcome = "success" if resp.status_code == 200 else "error"
                if resp.status_code == 200:
                    try:
                        choice = resp.json()["choices"][0]
                        message = choice["message"]
                        raw_content = message.get("content")
                        if choice.get("finish_reason") == "length":
                            return cls.failed_result(image_path, crop_name, "output_truncated",
                                "模型回答达到输出长度上限，本次未取得完整诊断；请重新诊断或联系管理员调整模型。")
                        if not isinstance(raw_content, str) or not raw_content.strip():
                            if message.get("reasoning_content") or message.get("reasoning"):
                                return cls.failed_result(image_path, crop_name, "reasoning_without_answer",
                                    "模型仅返回思考内容，未生成最终诊断；请联系管理员核对模型的非思考模式。")
                            raise ValueError("empty final response")
                        raw_content = raw_content.strip()
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        return cls.failed_result(image_path, crop_name, "invalid_response",
                            "平台返回了空回答或不兼容的响应，本次未取得有效诊断；请联系管理员核对模型接口。")
                    logger.info("多模态模型已返回视觉分析响应")
                    # 清洗 markdown 标签
                    cleaned = re.sub(r"^```json\s*", "", raw_content)
                    cleaned = re.sub(r"^```\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                    try:
                        parsed = json.loads(cleaned)
                        if not isinstance(parsed, dict):
                            raise ValueError("视觉结果必须为对象")
                        conf = finite_number(parsed.get("confidence"), 0, 1)
                        raw_name = parsed.get("suspected_disease")
                        suspected = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "待鉴别"
                        raw_features = parsed.get("features_detected")
                        features = [f.strip() for f in raw_features if isinstance(f, str) and f.strip()] if isinstance(raw_features, list) else []
                        raw_severity = parsed.get("severity")
                        severity = raw_severity if isinstance(raw_severity, str) and raw_severity in {"low", "medium", "high", "severe"} else "unknown"
                        desc = parsed.get("description") if isinstance(parsed.get("description"), str) else "未提供观察依据"

                        # ==================== 后置多维联合安全闸门 ====================
                        need_expert = parsed.get("need_expert") is not False or conf is None
                        safety_notes = []

                        # 闸门1: 前置质检不合格（低分辨率或极小文件）
                        if preflight["suspicious_synthetic"]:
                            need_expert = True
                            safety_notes.append(f"【图像复核】文件大小或分辨率触发应用复核条件({', '.join(preflight['warning_reasons'])})，建议人工复核")

                        # 闸门2: 模型自身识别为非实物或无法辨识
                        refusal_keywords = ["非真实", "人工绘制", "示意图", "简笔画", "几何", "无法辨识", "非植物", "合成", "模型无法"]
                        if any(kw in suspected for kw in refusal_keywords) or any(any(kw in f for kw in refusal_keywords) for f in features):
                            need_expert = True
                            safety_notes.append("【实物审查】识别为非真实叶片实物或病斑不可辨识")

                        # 强制复核与仅评分触发的建议分开，前者不能由自动建单开关绕过。
                        mandatory_review = need_expert
                        # 闸门3: 当前应用的模型评分复核门限，不是已校准的正确率
                        from app.core.confidence_policy import VISION_EXPERT_GATE
                        if conf is None or conf < VISION_EXPERT_GATE:
                            need_expert = True

                        # 闸门4: 当前应用的特征数量复核条件，不证明病害鉴别能力
                        if len(features) < 2:
                            need_expert = True
                            mandatory_review = True
                            safety_notes.append("【特征不足】有效观察特征少于2项，按应用策略建议人工复核")

                        if safety_notes:
                            desc = f"{'；'.join(safety_notes)}。\n{desc}"

                        set_operation_outcome("success")
                        return {
                            "image_url": image_path,
                            "crop": crop_name,
                            "suspected_disease": suspected,
                            "confidence": conf,
                            "features_detected": features,
                            "severity": severity,
                            "need_expert": need_expert,
                            "mandatory_review": mandatory_review,
                            "description": desc,
                            "source_model": model,
                            "image_quality": {
                                "format": preflight["format"],
                                "dimensions": f"{preflight['width']}x{preflight['height']}",
                                "size_bytes": preflight["size_bytes"],
                                "passed_preflight": not preflight["suspicious_synthetic"]
                            }
                        }
                    except Exception as json_err:
                        logger.error("解析视觉模型 JSON 失败：%s", type(json_err).__name__)
                        return cls.failed_result(image_path, crop_name, "invalid_json",
                            "视觉模型输出格式无法解析，本次未取得有效诊断；可重新诊断或联系管理员核对模型。")
                else:
                    logger.error("视觉大模型返回错误状态：%s", resp.status_code)
                    descriptions = {
                        400: "平台拒绝诊断参数，请联系管理员核对完整模型ID、图像输入支持及接口配置。",
                        401: "视觉模型鉴权失败，请联系管理员检查并保存有效密钥。",
                        403: "平台拒绝访问视觉模型，请联系管理员核对模型权限、账号状态及额度。",
                        404: "平台未找到所选模型或接口，请联系管理员核对API根地址与完整模型ID。",
                        429: "视觉模型平台限流或额度受限，请稍后重新诊断，或联系管理员检查额度。",
                    }
                    return cls.failed_result(image_path, crop_name, f"http_{resp.status_code}",
                        descriptions.get(resp.status_code, f"视觉模型平台请求失败（HTTP {resp.status_code}），请稍后重新诊断。"))
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("视觉诊断失败 code=timeout")
            return cls.failed_result(image_path, crop_name, "timeout",
                "等待视觉模型响应超时，本次未取得诊断结果；可点击重新诊断，若持续超时请联系管理员检查模型服务与网络。")
        except httpx.HTTPError:
            logger.warning("视觉诊断失败 code=network_error")
            return cls.failed_result(image_path, crop_name, "network_error",
                "连接视觉模型平台失败，请稍后重新诊断；若持续失败，请联系管理员检查网络或TLS连接。")
        except Exception as api_err:
            logger.error("调用视觉大模型失败：%s", type(api_err).__name__)
            return cls.failed_result(image_path, crop_name, "vision_error",
                "视觉诊断未能完成，请联系管理员按本次请求ID核查。")

    @classmethod
    async def test_connection(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> Tuple[bool, str]:
        """测试多模态大模型连通性"""
        key = api_key if api_key is not None else getattr(settings, "VISION_API_KEY", "") or getattr(settings, "LLM_API_KEY", "") or DEFAULT_VISION_API_KEY
        url = (base_url or getattr(settings, "VISION_BASE_URL", "") or os.getenv("VISION_BASE_URL") or DEFAULT_VISION_BASE_URL).rstrip("/")
        model = model_name or getattr(settings, "VISION_MODEL", "") or os.getenv("VISION_MODEL") or DEFAULT_VISION_MODEL

        if not key:
            return False, "未配置视觉大模型 API Key"

        from app.services.model_probe_service import ModelProbeService
        result = await ModelProbeService.test({"api_key": key, "base_url": url, "model_name": model}, "vision")
        return result["success"], result["message"]
