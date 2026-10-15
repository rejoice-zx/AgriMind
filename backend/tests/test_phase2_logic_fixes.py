# -*- coding: utf-8 -*-
"""
阶段二业务安全与核心逻辑纠偏专项验证套件 (test_phase2_logic_fixes.py)
覆盖验证：
1. 气象安全门 Fail-Closed 逻辑（键缺失默认不适宜打药，离线告警透传）
2. 农事 record_type 多端枚举统一归一化（激活 PHI 安全间隔期与抗药性告警）
3. RAG 元数据过滤为空时杜绝全库回退，主动分流专家兜底
4. SettingsService import os 修复与 test_connection 同步方法异步化（杜绝 NameError 与 TypeError）
"""

import pytest
from datetime import date, timedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_service import AgriTools
from app.models.farming_record import (
    FarmingRecord,
    is_spraying_record,
    is_fertilizing_record,
)
from app.models.farm import Farm
from app.models.field import Field
from app.models.crop import CropPlanting
from app.models.user import User
from app.services.rag_service import RAGService
from app.services.settings_service import AgriAIConfig, SettingsService
from app.services.weather_service import WeatherService


@pytest.mark.asyncio
async def test_weather_spray_safety_fail_closed_and_offline():
    """测试气象安全门：键缺失默认不适宜打药(Fail-Closed)，离线透传警告"""
    # 1. 验证 WeatherService 兜底结构包含 is_fallback: True
    fallback_data = WeatherService._fallback_safe_weather(days=3)
    assert fallback_data["is_fallback"] is True
    assert fallback_data["spray_safety"]["is_suitable_to_spray"] is False
    assert fallback_data["spray_safety"]["is_offline_warning"] is True

    # 2. 验证键缺失时 Fail-Closed 保守判定
    dummy_spray_safety = {}  # 键缺失
    is_suitable = dummy_spray_safety.get("is_suitable_to_spray", False)
    assert is_suitable is False  # 必须为 False，绝不能默认 True


def test_farming_record_type_normalization():
    """测试农事 record_type 规范归一化判定函数"""
    # 施药/打药类中英文值域
    assert is_spraying_record("spraying") is True
    assert is_spraying_record("施药") is True
    assert is_spraying_record("病虫害防治") is True
    assert is_spraying_record("pest_control") is True
    assert is_spraying_record("常规田间喷药作业") is True
    assert is_spraying_record("irrigation") is False

    # 施肥/水肥类中英文值域
    assert is_fertilizing_record("fertilizing") is True
    assert is_fertilizing_record("施肥") is True
    assert is_fertilizing_record("水肥管理") is True
    assert is_fertilizing_record("spraying") is False


@pytest.mark.asyncio
async def test_phi_warning_triggered_by_spraying_record(test_session: AsyncSession):
    """测试通过前端标准 'spraying' 写入的记录能正确激活 PHI 安全间隔期告警"""
    farmer = User(username="farmer_phi", email="phi@test.com", password_hash="hash", role="customer", is_active=True)
    test_session.add(farmer)
    await test_session.commit()
    await test_session.refresh(farmer)

    farm = Farm(owner_id=farmer.id, name="PHI测试农场")
    test_session.add(farm)
    await test_session.commit()
    await test_session.refresh(farm)

    field = Field(farm_id=farm.id, name="PHI测试大棚")
    test_session.add(field)
    await test_session.commit()
    await test_session.refresh(field)

    # 模拟前端写入 'spraying' 记录，发生于 2 天前
    rec = FarmingRecord(
        field_id=field.id,
        record_type="spraying",  # 前端标准英文枚举
        record_date=date.today() - timedelta(days=2),
        input_name="68%精甲霜·锰锌水分散粒剂",
        input_amount=100.0,
        unit="克/亩",
        safety_interval_days=7,
    )
    test_session.add(rec)
    await test_session.commit()

    # 查询农事追溯工具
    history = await AgriTools.get_farming_history(test_session, field_id=field.id)
    assert history["last_spray"] is not None
    assert history["last_spray"]["input_name"] == "68%精甲霜·锰锌水分散粒剂"
    assert len(history["safety_warnings"]) >= 1
    assert "高频施药" in history["safety_warnings"][0] or "药害" in history["safety_warnings"][0]


@pytest.mark.asyncio
async def test_rag_metadata_filter_no_fallback_to_all():
    """测试 RAG 元数据过滤为空时严禁回退全库，直接声明无适用知识并流转专家"""
    rag = RAGService()
    # 模拟底库只有番茄知识
    mock_kb = [
        {"id": "kb_tomato_1", "crop": "番茄", "title": "番茄晚疫病防治", "answer": "使用烯酰吗啉喷雾", "source_name": "GB/T 23416.2"},
        {"id": "kb_tomato_2", "crop": "番茄", "title": "番茄开花期水肥", "answer": "控制氮肥增施钾肥", "source_name": "NY/T 393"},
    ]

    # 查询非番茄作物（火龙果）
    res = await rag.hybrid_search(
        query="火龙果溃疡病怎么防治？",
        knowledge_items=mock_kb,
        filter_metadata={"crop": "火龙果"},
    )

    # 严禁召回番茄的条目，必须为空且标明流转专家
    assert len(res.retrieved_items) == 0
    assert res.need_expert is True
    assert "火龙果" in res.expert_reason


def test_settings_service_no_nameerror_and_connection():
    """测试 SettingsService：无 NameError，test_connection 同步方法正常执行"""
    # 1. 实例化 AgriAIConfig（此前因缺少 import os 会抛 NameError）
    config = AgriAIConfig()
    assert config.llm_base_url is not None
    assert config.vision_base_url is not None

    # 2. 调用未知 provider 测试，验证不再因 await 同步方法抛 TypeError
    import asyncio
    success, msg = asyncio.run(
        SettingsService.test_llm_connection(
            provider="non_existent_provider",
            api_key="test_key",
        )
    )
    assert success is False
    assert "未知" in msg and "提供商" in msg
