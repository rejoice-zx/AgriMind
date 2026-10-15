# -*- coding: utf-8 -*-
"""
AgriMind AI 配置自检脚本 (check_ai_config.py)
检查系统各 AI 与外部服务配置项的来源与长度，绝不输出 Key 完整内容。
"""

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 添加 backend 路径
BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / "backend"))

try:
    from dotenv import load_dotenv
    for _p in (BASE_DIR / ".env", BASE_DIR / "backend" / ".env"):
        if _p.exists():
            load_dotenv(_p, override=False)
except Exception:
    pass

try:
    from app.config import settings
except Exception as e:
    settings = None

def mask_key(k: str) -> str:
    if not k:
        return "<空 / 未设置>"
    if len(k) <= 8:
        return f"长度={len(k)} (已配置)"
    return f"{k[:4]}...{k[-4:]} (长度={len(k)})"

def check_config():
    print("=" * 60)
    print("  AgriMind 智能中枢与外部服务配置自检")
    print("=" * 60)
    
    items = [
        ("LLM_API_KEY", "农业决策大模型 Key"),
        ("VISION_API_KEY", "病害视觉大模型 Key"),
        ("EMBEDDING_API_KEY", "稠密向量嵌入 Key"),
        ("WEATHER_API_KEY", "第三方气象服务 Key"),
        ("LLM_BASE_URL", "大模型 Base URL"),
        ("LLM_MODEL", "大模型 Model 名称"),
        ("VISION_BASE_URL", "视觉模型 Base URL"),
        ("VISION_MODEL", "视觉模型 Model 名称"),
        ("EMBEDDING_BASE_URL", "向量模型 Base URL"),
        ("EMBEDDING_MODEL", "向量模型 Model 名称"),
    ]
    
    for key, desc in items:
        # 1. 尝试从 os.environ 获取
        env_val = os.getenv(key, "")
        # 2. 尝试从 settings 获取
        settings_val = getattr(settings, key, "") if settings else ""
        
        val = env_val or settings_val
        source = ".env(自动加载)" if env_val else ("Settings默认" if settings_val else "未配置")
        status = "已生效" if val else "未设置(离线/降级模式)"

        # 检查运行期回落机制（如 EMBEDDING 回退继承 LLM 配置）
        if key == "EMBEDDING_API_KEY" and not val:
            fallback = os.getenv("LLM_API_KEY", "") or (getattr(settings, "LLM_API_KEY", "") if settings else "")
            if fallback:
                source = "回退继承LLM_API_KEY"
                val = fallback
                status = "已生效(运行期继承)"
        elif key == "EMBEDDING_BASE_URL" and not val:
            fallback = os.getenv("LLM_BASE_URL", "") or (getattr(settings, "LLM_BASE_URL", "") if settings else "")
            if fallback:
                val = f"{fallback} (继承LLM_BASE_URL)"

        if "KEY" in key:
            if val:
                print(f"{key:<18}: 来源={source}  长度={len(val)}  状态={status}")
            else:
                print(f"{key:<18}: 来源={source}  状态={status}")
        else:
            print(f"{key:<18}: {val or '<未设置>'}")

    print("=" * 60)

if __name__ == "__main__":
    check_config()
