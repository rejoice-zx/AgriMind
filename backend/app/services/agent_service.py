# -*- coding: utf-8 -*-
"""
AgriMind 农业智能决策 Agent 与 6 大核心工具链服务 (AgriAgentService)

核心功能:
1. 农业专用 6 大工具注册与标准化 Function Calling 接口:
   - Tool 1: get_field_info(field_id) -> 地块属性、土壤特性、活跃种植作物及生长期
   - Tool 2: get_weather(location, field_id, days) -> 气象时序快照、降雨概率与植保作业天气风险
   - Tool 3: get_farming_history(field_id, crop_planting_id, record_type, limit) -> 农事记录与用药安全间隔期(PHI)追溯
   - Tool 4: search_agri_knowledge(query, crop, growth_stage, topic, disease, top_k) -> Hybrid RAG 国家标准与权威知识混合检索
   - Tool 5: diagnose_crop_image(image_url, crop, symptoms_hint) -> 农业病害多模态图像初诊代理工具
   - Tool 6: calculate_gdd(t_max, t_min, t_base, daily_temps, crop) -> 有效积温(GDD)与物候生长阶段预测
2. 农业自主规划与风险裁决引擎 (AgriAgentService):
   - 基于 ReAct 范式的自主意图拆解与多工具链协同调度
   - 植保气象防漂移/防冲刷硬风险拦截（雨前打药阻断、高温蒸发阻断、大风飘移阻断）
   - 安全间隔期与重复用药冲突检测
   - 知识库置信度评估与低置信度向农技专家工单的自动流转机制
"""

from __future__ import annotations

import os
import re
import json
import math
import logging
from app.core.operation_observation import observed_model, observe_operation, set_operation_outcome
import time
from datetime import date
from typing import Any, Dict, List, Optional
import httpx
from fastapi import HTTPException, status

from app.config import settings
from app.core.pesticide_standards import (
    find_all_banned_pesticides,
)

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord, is_spraying_record, is_fertilizing_record
from app.models.field import Field
from app.models.user import User
from app.models.weather import WeatherSnapshot
from app.schemas.agent import (
    AgentDecisionRequest,
    AgentDecisionResponse,
    ToolDefinition,
    ToolExecuteResponse,
    ToolTrace,
)
from app.services.rag_service import RAGService
from app.services.farm_service import FarmService
from app.services.settings_service import SettingsService
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# ============================================================================
# 农业 6 大专业工具链实现
# ============================================================================

class AgriTools:
    """农业核心工具集实现类"""

    @staticmethod
    async def get_field_info(
        db: AsyncSession,
        field_id: int,
        crop_planting_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Tool 1: 获取田块属性、土壤特性、种植作物与当前生长期
        """
        stmt = (
            select(Field)
            .options(
                selectinload(Field.farm),
                selectinload(Field.crop_plantings),
            )
            .where(Field.id == field_id)
        )
        result = await db.execute(stmt)
        field = result.scalar_one_or_none()

        if not field:
            return {
                "found": False,
                "field_id": field_id,
                "message": f"未找到ID为 {field_id} 的田块信息",
            }

        # 查找当前处于 active 状态的种植批次
        active_crop: Optional[CropPlanting] = None
        candidates = [cp for cp in field.crop_plantings if cp.status == 'active']
        if crop_planting_id is not None:
            active_crop = next((cp for cp in candidates if cp.id == crop_planting_id), None)
            if active_crop is None: return {'found': False, 'field_id': field_id, 'message': '指定活跃作物批次不可用'}
        else:
            if len(candidates) > 1:
                return {'found': False, 'field_id': field_id, 'message': '此田块有多个活跃批次，请明确选择作物批次'}
            active_crop = max(candidates, key=lambda cp: (cp.planting_date or date.min, cp.id)) if candidates else None

        today = date.today()
        planting_info = None
        if active_crop:
            days_planted = (today - active_crop.planting_date).days if active_crop.planting_date else 0
            planting_info = {
                "crop_planting_id": active_crop.id,
                "crop": active_crop.crop,
                "variety": active_crop.variety,
                "planting_date": active_crop.planting_date.isoformat() if active_crop.planting_date else None,
                "expected_harvest_date": active_crop.expected_harvest_date.isoformat() if active_crop.expected_harvest_date else None,
                "growth_stage": active_crop.growth_stage,
                "days_planted": days_planted,
                "area": active_crop.area,
                "status": active_crop.status,
            }

        from app.services.field_environment_service import FieldEnvironmentService
        return {
            "environment": await FieldEnvironmentService.latest(db, field),
            "found": True,
            "field_id": field.id,
            "field_name": field.name,
            "farm_id": field.farm_id,
            "farm_name": field.farm.name if field.farm else None,
            "region": f"{field.farm.province or ''}{field.farm.city or ''}{field.farm.district or ''}" if field.farm else None,
            "area_mu": field.area,

            "soil_type": field.soil_type,
            "irrigation_type": field.irrigation_type,
            "latitude": field.latitude,
            "longitude": field.longitude,
            "current_crop": planting_info,
        }

    @staticmethod
    async def get_weather(db: AsyncSession, field_id: Optional[int] = None,
                          location: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
        from app.services.weather_service import WeatherService
        field = (await db.execute(select(Field).where(Field.id == field_id))).scalars().first() if field_id is not None else None
        if field is None:
            result = WeatherService._fallback_safe_weather(days, "location_missing", "未选择具备有效经纬度的田块，不能推断现场天气")
        else:
            result = await WeatherService.get_field_weather(db, field, days)
        cur = result.get("current", {})
        safety = result.get("spray_safety", {})
        if field is not None and not result.get("is_fallback", False) and all(cur.get(k) is not None for k in ("temperature", "humidity", "rainfall", "wind_speed")):
            from datetime import datetime, timezone
            observed = cur.get("timestamp")
            # Preserve provider observation time, not the time this tool was called.
            if observed:
                observed_time = datetime.fromisoformat(observed).astimezone(timezone.utc)
                existing = (await db.execute(select(WeatherSnapshot.id).where(
                    WeatherSnapshot.field_id == field.id, WeatherSnapshot.timestamp == observed_time,
                    WeatherSnapshot.source == cur.get("source")))).first()
                if not existing:
                    db.add(WeatherSnapshot(field_id=field.id, timestamp=observed_time,
                        temperature=cur["temperature"], humidity=cur["humidity"], rainfall=cur["rainfall"],
                        wind_speed=cur["wind_speed"], weather=cur.get("weather"), source=cur.get("source")))
                    await db.flush()
        suitable = bool(safety.get("is_suitable_to_spray", False)) and not result.get("is_fallback", False)
        return {"field_id": field_id, "location": field.name if field else "地点未绑定",
                "current_weather": cur, "forecasts": result.get("forecasts", []),
                "environment": result.get('environment'),
                "forecast_days": len(result.get("forecasts", [])),
                "spray_safety": {"is_suitable": suitable, "has_rain_soon": safety.get("has_rain_soon", False),
                    "is_offline_warning": safety.get("is_offline_warning", True),
                    "risks": safety.get("spray_risks", []), "advice": result.get("summary", "天气未知")}}


    @staticmethod
    async def get_farming_history(
        db: AsyncSession,
        field_id: int,
        crop_planting_id: Optional[int] = None,
        record_type: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        Tool 3: 追溯农事操作历史，排查最近施肥、施药安全间隔期(PHI)与重复用药抗药性
        """
        stmt = (
            select(FarmingRecord)
            .where(FarmingRecord.field_id == field_id, FarmingRecord.revoked_at.is_(None))
        )
        if crop_planting_id:
            stmt = stmt.where(FarmingRecord.crop_planting_id == crop_planting_id)
        if record_type:
            stmt = stmt.where(FarmingRecord.record_type == record_type)

        stmt = stmt.order_by(desc(FarmingRecord.record_date)).limit(limit)
        result = await db.execute(stmt)
        records = list(result.scalars().all())

        today = date.today()
        last_spray = None
        last_fertilize = None
        recent_inputs = []

        history_items = []
        for r in records:
            days_ago = (today - r.record_date).days
            item = {
                "id": r.id,
                "record_type": r.record_type,
                "record_date": r.record_date.isoformat(),
                "days_ago": days_ago,
                "input_name": r.input_name,
                "input_amount": r.input_amount,
                "safety_interval_days": r.safety_interval_days,
                "unit": r.unit,
                "operator": r.operator,
                "description": r.description,
            }
            history_items.append(item)

            if is_spraying_record(r.record_type) and last_spray is None:
                last_spray = item
            if is_fertilizing_record(r.record_type) and last_fertilize is None:
                last_fertilize = item
            if r.input_name:
                recent_inputs.append(r.input_name)

        # 安全间隔期与重复用药安全评估
        warnings = []
        if last_spray:
            days = last_spray["days_ago"]
            if days <= 4:
                warnings.append(f"距上次施药({last_spray['input_name']})仅过去 {days} 天，短时间内高频施药易引发作物药害及害虫抗药性，应谨慎复配。")

        return {
            "field_id": field_id,
            "total_records_found": len(records),
            "records": history_items,
            "last_spray": last_spray,
            "last_fertilize": last_fertilize,
            "recent_inputs": recent_inputs,
            "safety_warnings": warnings,
        }

    @staticmethod
    async def search_agri_knowledge(
        db: AsyncSession,
        query: str,
        crop: Optional[str] = None,
        growth_stage: Optional[str] = None,
        topic: Optional[str] = None,
        disease: Optional[str] = None,
        top_k: Optional[int] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Tool 4: 读取当前配置和有效候选知识；来源标注不等于标准核验
        """
        from app.services.agri_retrieval_service import AgriRetrievalService
        filters = {key: value for key, value in dict(crop=crop, growth_stage=growth_stage, topic=topic, disease=disease, region=region).items() if value}
        search_result, retrieval_trace = await AgriRetrievalService.search(db, query, filters, top_k=top_k)

        items = []
        for it in search_result.retrieved_items:
            items.append({
                "id": it["id"],
                "question": it["question"],
                "answer": it["answer"],
                "crop": it.get("crop"),
                "growth_stage": it.get("growth_stage"),
                "topic": it.get("topic"),
                "disease": it.get("disease"),
                "authority_level": it.get("authority_level", "L1"),
                "source_name": it.get("source_name"),
                "final_score": it.get("score", 0.0),
                "source_type": it.get("source_type"), "version": it.get("version"),
                "publish_date": it.get("publish_date"), "url": it.get("url"), "source_verified": False,
            })

        return {
            "query": query,
            "crop": crop,
            "disease": disease,
            "total_recalled": len(items),
            "confidence": search_result.confidence,
            "need_expert": search_result.need_expert,
            "expert_reason": search_result.expert_reason,
            "router_intent": search_result.intent,
            "context_text": search_result.context_text,
            "retrieval_trace": retrieval_trace,
            "extracted_entities": search_result.extracted_entities,
            "items": items,
        }


    @staticmethod
    async def diagnose_crop_image(
        image_url: str,
        crop: Optional[str] = None,
        symptoms_hint: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """
        Tool 5: 农业病害多模态视觉初诊代理工具
        通过分析病斑形态、色泽与分布规律输出疑似病害、可信度及专家转接建议
        """
        from app.services.vision_service import VisionService
        crop_name = crop or "未提供"
        if not image_url:
            return {
                "image_url": None,
                "crop": crop_name,
                "suspected_disease": "未提供病叶图像",
                "confidence": None,
                "features_detected": [],
                "severity": "未知",
                "need_expert": True,
                "description": "未检测到上传的图片文件，请上传现场照片；本次未创建专家工单。",
            }

        # 调用 SiliconFlow 真实多模态大模型 (Qwen3-VL) 执行深度视觉识别
        model_config = await SettingsService.resolve_agri_model(db, "vision") if db is not None else {}
        return await VisionService.diagnose_leaf_image(
            image_path=image_url,
            crop_name=crop_name,
            symptoms_hint=symptoms_hint,
            **model_config,
        )

    @staticmethod
    def calculate_gdd(
        t_max: Optional[float] = None,
        t_min: Optional[float] = None,
        t_base: float = 10.0,
        daily_temps: Optional[List[Dict[str, float]]] = None,
        crop: str = "番茄",
    ) -> Dict[str, Any]:
        """
        Tool 6: 有效积温 (GDD, Growing Degree Days) 与作物生长阶段预测模型
        公式: GDD = max(0, (T_max + T_min) / 2 - T_base)
        """
        from app.core.gdd import calculate_thermal_sum
        return calculate_thermal_sum(t_max, t_min, t_base, daily_temps, crop)



# ============================================================================
# 工具注册表 (AgriToolRegistry)
# ============================================================================

class AgriToolRegistry:
    """农业工具注册中心与标准化 Function Calling 模式转换器"""

    _TOOLS: Dict[str, Dict[str, Any]] = {
        "get_field_info": {
            "name": "get_field_info",
            "description": "获取指定田块的物理属性（面积、土壤、滴灌系统）与当前活跃种植作物的品种、定植天数及生长阶段（苗期/开花期/坐果期/采收期）。",
            "category": "field",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "integer",
                        "description": "田块ID号，例如 1",
                    },
                    "crop_planting_id": {"type": "integer", "description": "指定此田块的活跃作物批次，可选"}
                },
                "required": ["field_id"],
            },
        },
        "get_weather": {
            "name": "get_weather",
            "description": "获取地块所在地近7天气象预报及最新微气象快照，评估降雨概率、温度、风速对打药和水肥作业的气象安全风险。",
            "category": "environment",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "integer",
                        "description": "田块ID（可选，优先查询地块微气象传感器）",
                    },
                    "location": {
                        "type": "string",
                        "description": "地理位置名称，如'山东寿光'",
                    },
                    "days": {
                        "type": "integer",
                        "description": "预报天数（默认7天）",
                        "default": 7,
                    },
                },
            },
        },
        "get_farming_history": {
            "name": "get_farming_history",
            "description": "查询田块历史农事操作记录（施肥、施药、修剪等），检测安全间隔期(PHI)与重复施药风险。",
            "category": "farming",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "integer",
                        "description": "田块ID",
                    },
                    "record_type": {
                        "type": "string",
                        "description": "操作类型筛选：'施药' / '施肥' / '灌溉'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大记录条数",
                        "default": 10,
                    },
                },
                "required": ["field_id"],
            },
        },
        "search_agri_knowledge": {
            "name": "search_agri_knowledge",
            "description": "调用农业 Hybrid RAG 引擎，基于国家标准与专家规程检索病虫害防治、推荐配方、用药剂量与禁忌事项。",
            "category": "knowledge",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索问题或病害关键词，例如'番茄灰霉病防治配方'",
                    },
                    "crop": {
                        "type": "string",
                        "description": "作物名称（如'番茄'）",
                    },
                    "growth_stage": {
                        "type": "string",
                        "description": "生长期（苗期/开花期/坐果期/采收期）",
                    },
                    "topic": {
                        "type": "string",
                        "description": "主题（病害防治/水肥管理/环境调控）",
                    },
                    "disease": {
                        "type": "string",
                        "description": "病害全称（如'灰霉病'）",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "召回条数",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        "diagnose_crop_image": {
            "name": "diagnose_crop_image",
            "description": "通过病叶或植株照片进行农业多模态视觉初诊，识别疑似病害（灰霉病/早疫病/晚疫病等）及置信度。",
            "category": "multimodal",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "病害照片URL或文件名",
                    },
                    "crop": {
                        "type": "string",
                        "description": "作物名称",
                    },
                    "symptoms_hint": {
                        "type": "string",
                        "description": "视觉症状补充提示（如'同心轮纹'、'灰色霉层'）",
                    },
                },
                "required": ["image_url"],
            },
        },
        "calculate_gdd": {
            "name": "calculate_gdd",
            "description": "有效积温(GDD)计算与作物生长周期阶段预测，辅助判定物候进程与开花转色期。",
            "category": "science",
            "parameters": {
                "type": "object",
                "properties": {
                    "t_max": {"type": "number", "description": "日最高温(℃)"},
                    "t_min": {"type": "number", "description": "日最低温(℃)"},
                    "t_base": {"type": "number", "description": "发育基点温度(默认10℃)", "default": 10.0},
                    "daily_temps": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "气温序列列表",
                    },
                    "crop": {"type": "string", "description": "作物名称", "default": "番茄"},
                },
            },
        },
    }

    @classmethod
    def get_tool_definitions(cls) -> List[ToolDefinition]:
        """获取所有标准工具定义（供 LLM 或前端展示）"""
        return [
            ToolDefinition(
                name=t["name"],
                description=t["description"],
                parameters=t["parameters"],
                category=t["category"],
            )
            for t in cls._TOOLS.values()
        ]

    @classmethod
    async def execute_tool(
        cls,
        tool_name: str,
        arguments: Dict[str, Any],
        db: AsyncSession,
    ) -> ToolExecuteResponse:
        if tool_name not in cls._TOOLS:
            return await cls._dispatch_tool(tool_name, arguments, db)
        from app.services.performance_service import get_performance_service
        started, success = time.perf_counter(), False
        try:
            response = await cls._dispatch_tool(tool_name, arguments, db)
            success = response.status == "success"
            return response
        finally:
            try:
                get_performance_service().record("agri_tool_" + tool_name, time.perf_counter() - started, success)
            except Exception:
                logger.warning("农业工具性能记录失败；保留工具原结果")

    @classmethod
    async def _dispatch_tool(
        cls,
        tool_name: str,
        arguments: Dict[str, Any],
        db: AsyncSession,
    ) -> ToolExecuteResponse:
        """分发并执行工具调用"""
        if tool_name not in cls._TOOLS:
            return ToolExecuteResponse(
                tool_name=tool_name,
                status="error",
                result=None,
                error=f"未找到工具: {tool_name}",
            )

        try:
            if tool_name == "get_field_info":
                field_id = int(arguments.get("field_id", 1))
                if arguments.get('crop_planting_id') is not None:
                    res = await AgriTools.get_field_info(db, field_id, int(arguments['crop_planting_id']))
                else:
                    res = await AgriTools.get_field_info(db, field_id)
            elif tool_name == "get_weather":
                res = await AgriTools.get_weather(
                    db,
                    field_id=arguments.get("field_id"),
                    location=arguments.get("location"),
                    days=int(arguments.get("days", 7)),
                )
            elif tool_name == "get_farming_history":
                res = await AgriTools.get_farming_history(
                    db,
                    field_id=int(arguments.get("field_id", 1)),
                    crop_planting_id=arguments.get("crop_planting_id"),
                    record_type=arguments.get("record_type"),
                    limit=int(arguments.get("limit", 10)),
                )
            elif tool_name == "search_agri_knowledge":
                res = await AgriTools.search_agri_knowledge(
                    db,
                    query=arguments.get("query", ""),
                    crop=arguments.get("crop"),
                    growth_stage=arguments.get("growth_stage"),
                    topic=arguments.get("topic"),
                    disease=arguments.get("disease"),
                    top_k=arguments.get("top_k"),
                    region=arguments.get("region"),
                )
            elif tool_name == "diagnose_crop_image":
                res = await AgriTools.diagnose_crop_image(
                    image_url=arguments.get("image_url", ""),
                    crop=arguments.get("crop"),
                    symptoms_hint=arguments.get("symptoms_hint"),
                    db=db,
                )
            elif tool_name == "calculate_gdd":
                res = AgriTools.calculate_gdd(
                    t_max=arguments.get("t_max"),
                    t_min=arguments.get("t_min"),
                    t_base=float(arguments.get("t_base", 10.0)),
                    daily_temps=arguments.get("daily_temps"),
                    crop=arguments.get("crop", "番茄"),
                )
            else:
                return ToolExecuteResponse(
                    tool_name=tool_name,
                    status="error",
                    result=None,
                    error="未知工具实现",
                )

            return ToolExecuteResponse(
                tool_name=tool_name,
                status="success",
                result=res,
            )
        except Exception as e:
            logger.exception("执行工具 %s 失败", tool_name)
            return ToolExecuteResponse(
                tool_name=tool_name,
                status="error",
                result=None,
                error=str(e),
            )


# ============================================================================
# 农业自主规划与综合决策 Agent (AgriAgentService)
# ============================================================================

class AgriAgentService:
    """农业智能决策 Agent 核心服务"""

    @classmethod
    async def decide(
        cls,
        db: AsyncSession,
        request: AgentDecisionRequest,
        current_user: Optional[User] = None,
    ) -> AgentDecisionResponse:
        """
        Agent 核心规划与决策入口：
        1. 意图解析与规划制定
        2. 多工具自主并发/链式调用收集证据（严格记录各工具物理执行耗时）
        3. 冲突仲裁与农业风险裁决（降雨拦截、抗药性、安全间隔期、L1国标对齐、法定禁用农药双侧硬阻断）
        4. 结构化决策输出与低置信度专家工单分流（Fail-Safe 兜底防穿透）
        """
        start_time = time.perf_counter()
        query = request.query
        field_id = request.field_id
        # Resource authorization must finish before any tool, model or snapshot call.
        # Preserve resource-free service calls, but never treat an omitted caller as
        # authorization for a resource-bound request (including zero/negative IDs).
        if field_id is not None or request.crop_planting_id is not None:
            if current_user is None or not current_user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="访问农业资源需要有效的登录用户",
                )
            if field_id is not None:
                await FarmService.ensure_field_access(db, field_id, current_user)
            if request.crop_planting_id is not None:
                crop = await FarmService.ensure_crop_access(db, request.crop_planting_id, current_user)
                if field_id is not None and crop.field_id != field_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="作物种植批次不属于指定地块",
                    )
                if field_id is None:
                    field_id = crop.field_id
                    await FarmService.ensure_field_access(db, field_id, current_user)
        tool_traces: List[ToolTrace] = []
        evidences: List[Dict[str, Any]] = []
        risks: List[str] = []
        recommendations: List[str] = []

        # 输入侧安全审计：全量扫描法定禁限用高毒农药负面清单
        banned_in_query = find_all_banned_pesticides(query)
        if banned_in_query:
            banned_str = "、".join(banned_in_query)
            risks.append(f"【高危禁用农药拦截】咨询内容涉及法定禁用/高毒农药 [{banned_str}]，根据国家《农药管理条例》严禁在果蔬上使用！")

        # 1. 意图解析
        q_lower = query.lower()
        is_spray = any(w in query for w in ["打药", "喷药", "施药", "用药", "防虫", "防病", "灰霉病", "疫病", "白粉病"])
        is_fertilize = any(w in query for w in ["施肥", "水肥", "冲施", "追肥", "底肥", "氮肥", "钾肥"])
        is_diagnosis = (request.image_url is not None) or any(w in query for w in ["诊断", "得病", "发黄", "枯萎", "斑点", "生病"])
        is_gdd = any(w in query for w in ["积温", "几天成熟", "什么时候开花", "生长期", "物候"])

        decision_type = "GENERAL"
        if is_spray:
            decision_type = "SPRAY_DECISION"
        elif is_fertilize:
            decision_type = "FERTILIZATION"
        elif is_diagnosis:
            decision_type = "DIAGNOSIS"
        elif is_gdd:
            decision_type = "GDD_PREDICTION"

        field_info = None
        weather_info = None
        history_info = None
        rag_info = None
        diagnosis_info = None
        gdd_info = None
        # 视觉初诊工具（diagnose_crop_image）的 need_expert 是权威硬闸信号，
        # 不能被高置信度的模型结论静默覆盖（NEW-08 / P1-EXPERT-FLAG）。
        vision_need_expert = False

        detected_crop = next((crop for crop in ("番茄", "黄瓜", "辣椒", "茄子", "西瓜") if crop in query), "未提供")
        detected_stage = "未录入"
        detected_disease = None

        # 提取常见病害关键词
        for d in ["灰霉病", "早疫病", "晚疫病", "叶霉病", "白粉病", "青枯病", "病毒病"]:
            if d in query:
                detected_disease = d
                break

        # 2. 调度 Tool 1: get_field_info
        if field_id is not None:
            t0 = time.perf_counter()
            field_arguments = {"field_id": field_id}
            if request.crop_planting_id is not None: field_arguments['crop_planting_id'] = request.crop_planting_id
            f_exec = await AgriToolRegistry.execute_tool("get_field_info", field_arguments, db)
            dur_ms = round((time.perf_counter() - t0) * 1000, 2)
            tool_traces.append(ToolTrace(
                tool_name="get_field_info",
                arguments=field_arguments,
                result_summary=f"查询田块 #{field_id} 成功: {f_exec.result.get('field_name') if f_exec.result else ''}",
                status=f_exec.status,
                duration_ms=dur_ms,
            ))
            if f_exec.status == "success" and f_exec.result.get("found"):
                field_info = f_exec.result
                cur_crop = field_info.get("current_crop")
                if cur_crop:
                    detected_crop = cur_crop.get("crop") or detected_crop
                    detected_stage = cur_crop.get("growth_stage") or detected_stage
                evidences.append({
                    "source": "田块档案",
                    "content": f"地块【{field_info.get('field_name')}】({field_info.get('soil_type')})，当前种植作物: {detected_crop}，所处生长阶段: {detected_stage}",
                })
                if field_info.get('environment'):
                    evidences.append({'source': '田块环境（人工录入，非连续监测）',
                        'content': json.dumps(field_info['environment'], ensure_ascii=False)})

        # 3. 调度 Tool 5: diagnose_crop_image (若有图片)
        if request.image_url and request.enable_multimodal:
            t0 = time.perf_counter()
            diag_exec = await AgriToolRegistry.execute_tool(
                "diagnose_crop_image",
                {"image_url": request.image_url, "crop": detected_crop, "symptoms_hint": query},
                db,
            )
            dur_ms = round((time.perf_counter() - t0) * 1000, 2)
            tool_traces.append(ToolTrace(
                tool_name="diagnose_crop_image",
                arguments={"image_url": request.image_url, "crop": detected_crop},
                result_summary=f"初诊疑似: {diag_exec.result.get('suspected_disease') if diag_exec.result else '未识别'}",
                status=diag_exec.status,
                duration_ms=dur_ms,
            ))
            if diag_exec.status == "success":
                diagnosis_info = diag_exec.result
                if diagnosis_info.get("need_expert"):
                    vision_need_expert = True
                if diagnosis_info.get("suspected_disease") and diagnosis_info.get("suspected_disease") != "未知病害":
                    detected_disease = diagnosis_info["suspected_disease"].split(" ")[0].replace("番茄", "")
                evidences.append({
                    "source": "多模态视觉初诊",
                    "content": f"疑似病害: {diagnosis_info.get('suspected_disease')}, 初诊置信度: {diagnosis_info.get('confidence')}, 特征: {','.join(diagnosis_info.get('features_detected', []))}",
                })

        # 4. 调度 Tool 2: get_weather (打药/施肥/天气决策场景)
        if is_spray or is_fertilize or is_gdd or decision_type in ("SPRAY_DECISION", "GENERAL"):
            t0 = time.perf_counter()
            w_exec = await AgriToolRegistry.execute_tool(
                "get_weather",
                {"field_id": field_id, "location": request.location or (field_info.get("region") if field_info else None), "days": 7},
                db,
            )
            dur_ms = round((time.perf_counter() - t0) * 1000, 2)
            tool_traces.append(ToolTrace(
                tool_name="get_weather",
                arguments={"field_id": field_id, "days": 7},
                result_summary="获取7天天候与植保作业适宜度评估",
                status=w_exec.status,
                duration_ms=dur_ms,
            ))
            if w_exec.status == "success":
                weather_info = w_exec.result
                safety = weather_info.get("spray_safety", {})
                if not safety.get("is_suitable"):
                    risks.extend(safety.get("risks", []))
                evidences.append({
                    "source": "气象与微环境预警",
                    "content": f"当前天气: {weather_info.get('current_weather', {}).get('weather')}, 适宜度结论: {safety.get('advice')}",
                })

        # 5. 调度 Tool 3: get_farming_history (查近期操作与用药安全间隔期)
        if field_id is not None and (is_spray or is_fertilize):
            t0 = time.perf_counter()
            h_exec = await AgriToolRegistry.execute_tool(
                "get_farming_history",
                {"field_id": field_id, "limit": 5},
                db,
            )
            dur_ms = round((time.perf_counter() - t0) * 1000, 2)
            tool_traces.append(ToolTrace(
                tool_name="get_farming_history",
                arguments={"field_id": field_id, "limit": 5},
                result_summary="获取近期施肥打药历史",
                status=h_exec.status,
                duration_ms=dur_ms,
            ))
            if h_exec.status == "success":
                history_info = h_exec.result
                for warn in history_info.get("safety_warnings", []):
                    risks.append(warn)
                if history_info.get("last_spray"):
                    evidences.append({
                        "source": "历史农事档案",
                        "content": f"上次施药: {history_info['last_spray']['input_name']}，执行于 {history_info['last_spray']['days_ago']} 天前",
                    })

        # 6. 调度 Tool 4: search_agri_knowledge (检索 Hybrid RAG 国家规程与防治方案)
        rag_search_query = query
        if detected_disease:
            rag_search_query = f"{detected_crop}{detected_disease}防治方法与推荐农药规程"

        t0 = time.perf_counter()
        k_exec = await AgriToolRegistry.execute_tool(
            "search_agri_knowledge",
            {
                "query": rag_search_query,
                "crop": detected_crop if detected_crop != "未提供" else None,
                "growth_stage": detected_stage if detected_stage != "未录入" else None,
                "disease": detected_disease,
                "region": (field_info or {}).get('region'),
            },
            db,
        )
        dur_ms = round((time.perf_counter() - t0) * 1000, 2)
        tool_traces.append(ToolTrace(
            tool_name="search_agri_knowledge",
            arguments={"query": rag_search_query, "crop": detected_crop, "disease": detected_disease},
            result_summary=f"Hybrid RAG 召回 {len(k_exec.result.get('items', [])) if k_exec.result else 0} 条候选知识（来源标注待核验）",
            status=k_exec.status,
            duration_ms=dur_ms,
            execution_metadata=(k_exec.result or {}).get("retrieval_trace", {}),
        ))
        if k_exec.status == "success":
            rag_info = k_exec.result
            if rag_info.get("context_text"):
                evidences.append({"source": "知识库检索内容（来源和等级为数据库标注，待核验）", "content": rag_info["context_text"]})
            else:
                # Compatibility for tool adapters that still return only items; apply the saved character budget.
                from app.services.agri_retrieval_service import AgriRetrievalService
                rag_config, _ = await AgriRetrievalService.config(db)
                remaining = rag_config.max_context_length
                for it in rag_info.get("items", []):
                    content = f"【{it.get('question')}】{it.get('answer')}"[:remaining]
                    if not content: break
                    evidences.append({"source": "知识库候选（来源标注待核验）", "content": content})
                    remaining -= len(content)

        # 7. 调度 Tool 6: calculate_gdd (如果是物候推算或提供了气象序列)
        if is_gdd or (weather_info and "forecasts" in weather_info and decision_type == "GDD_PREDICTION"):
            # A short forecast is not historical accumulated GDD; do not invent a temperature series.
            daily_list = []
            t0 = time.perf_counter()
            g_exec = await AgriToolRegistry.execute_tool(
                "calculate_gdd",
                {"daily_temps": daily_list, "crop": detected_crop, "t_base": 10.0},
                db,
            )
            dur_ms = round((time.perf_counter() - t0) * 1000, 2)
            tool_traces.append(ToolTrace(
                tool_name="calculate_gdd",
                arguments={"crop": detected_crop, "t_base": 10.0},
                result_summary="缺少完整历史温度序列，不能计算当前累计积温",
                status=g_exec.status,
                duration_ms=dur_ms,
            ))
            risks.append("缺少从定植开始的历史温度序列，当前不能计算累计积温或预测采收天数")

        # ====================================================================
        # 3. 智能冲突仲裁与真实大模型自主研判 (SiliconFlow DeepSeek-V3 推理)
        # ====================================================================

        # 整理证据链条
        evidence_summary_lines = []
        for e in evidences:
            evidence_summary_lines.append(f"- 【{e.get('source')}】: {e.get('content')}")
        context_str = "\n".join(evidence_summary_lines)

        weather_risk_summary = []
        if weather_info and "spray_safety" in weather_info:
            weather_risk_summary.extend(weather_info["spray_safety"].get("risks", []))

        # 真实调用大模型执行植保决策推理
        model_config = await SettingsService.resolve_agri_model(db, "decision")
        llm_decision = await cls._synthesize_with_llm(
            query=query,
            detected_crop=detected_crop,
            detected_stage=detected_stage,
            detected_disease=detected_disease,
            context_str=context_str,
            weather_risk_summary=weather_risk_summary,
            diagnosis_info=diagnosis_info,
            rag_info=rag_info,
            **model_config,
        )

        # 模型必须返回 JSON 对象；非对象结构一律按保守降级处理（防御，避免下游 .get 崩溃）
        if not isinstance(llm_decision, dict):
            logger.error("农业决策大模型返回非对象结构(%s)，按保守降级处理", type(llm_decision).__name__)
            llm_decision = {}

        # --------------------------------------------------------------------
        # 输出侧安全审计与过滤：拦截模型生成的违规禁用农药推荐
        # --------------------------------------------------------------------
        raw_recs = llm_decision.get("recommendations", [])
        if not isinstance(raw_recs, list):
            raw_recs = []
        filtered_recommendations: List[str] = []
        banned_in_recs: List[str] = []
        for rec in raw_recs:
            rec_str = str(rec)
            found_banned = find_all_banned_pesticides(rec_str)
            if found_banned:
                banned_in_recs.extend(found_banned)
                logger.warning("大模型建议包含法定禁用农药 %s，已实施安全网剔除: %s", found_banned, rec_str)
            else:
                filtered_recommendations.append(rec_str)

        if banned_in_recs:
            unique_banned = list(dict.fromkeys(banned_in_recs))
            risks.append(f"【模型输出违规拦截】大模型推荐包含法定禁用/限用农药 [{ '、'.join(unique_banned) }]，安全防护网已强制剔除违规建议！")

        raw_conclusion = str(llm_decision.get("conclusion") or "已综合完成现代数字农业规程研判。")
        banned_in_conclusion = find_all_banned_pesticides(raw_conclusion)

        # 整理风险集合（去重保序）；模型 risks 仅接纳字符串元素
        llm_risks = llm_decision.get("risks")
        llm_risks = [r for r in llm_risks if isinstance(r, str)] if isinstance(llm_risks, list) else []
        all_risks = risks + llm_risks
        final_risks = list(dict.fromkeys(all_risks))

        # --------------------------------------------------------------------
        # Fail-Safe 容错与防御性覆写
        # --------------------------------------------------------------------
        # 严格布尔：仅接受真正的 JSON 布尔值；字符串 "false"/数字/其它一律保守判 False
        # （旧实现 bool("false") 会误判为 True，属于 NEW-07）。
        llm_can_proceed = llm_decision.get("can_proceed")
        can_proceed = llm_can_proceed if isinstance(llm_can_proceed, bool) else False

        # 2. 检查硬阻断条件
        has_banned_violation = bool(banned_in_query or banned_in_recs or banned_in_conclusion)
        # 缺少有效气象证据（工具失败/未返回 spray_safety）时按"不宜作业"处理，施药决策 fail-closed；
        # 非施药决策（is_spray/SPRAY_DECISION 之外）不会触发气象阻断。
        if weather_info and "spray_safety" in weather_info:
            weather_suitable = bool(weather_info["spray_safety"].get("is_suitable", False))
        else:
            weather_suitable = False
        has_weather_block = bool((is_spray or decision_type == "SPRAY_DECISION") and not weather_suitable)

        # 3. 代码层绝对硬阻断覆写（消除 LLM 幻觉放行风险）
        final_conclusion = raw_conclusion
        if has_banned_violation:
            can_proceed = False
            if "严禁" not in final_conclusion and "阻断" not in final_conclusion:
                final_conclusion = f"【法规硬阻断/严禁使用禁用农药】检测到国家明令禁止的高毒/限用农药成分，严禁施用！{final_conclusion}"

        if has_weather_block and not has_banned_violation:
            can_proceed = False
            unknown_weather = not weather_info or weather_info.get("spray_safety", {}).get("is_offline_warning", True)
            explanation = "缺少有效天气证据，无法确认施药条件" if unknown_weather else "；".join(weather_info.get("spray_safety", {}).get("risks", []))
            final_conclusion = f"【气象数据不足/暂缓施药】{explanation}" if unknown_weather else f"【气象风险/暂缓施药】{explanation}"
            filtered_recommendations = ["先核实本田块天气与现场情况；天气核实前暂缓施药。", "药剂、适用条件和安全间隔期需核验后再作决策。"]

        # 4. 置信度容错与保守兜底：仅接受有限且落在 [0,1] 的数值；缺失/NaN/Inf/越界/非数值
        #    一律保守兜底 0.60，防止跳过专家闸门。
        raw_conf = llm_decision.get("confidence")
        confidence = 0.60
        if (isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool)
                and math.isfinite(raw_conf) and 0.0 <= raw_conf <= 1.0):
            confidence = round(float(raw_conf), 2)

        # 5. 专家工单触发机制
        need_expert = bool(llm_decision.get("need_expert", False))
        raw_reason = llm_decision.get("expert_reason")
        expert_reason = raw_reason.strip()[:1000] or None if isinstance(raw_reason, str) else None
        if raw_reason is not None and not isinstance(raw_reason, str):
            # Malformed model output must never escape the response schema or
            # weaken the safety gate. Do not stringify arbitrary model objects.
            need_expert = True
            expert_reason = "模型专家复核理由格式异常，请由农技专家复核"

        if has_banned_violation:
            need_expert = True
            expert_reason = expert_reason or "检测到法定禁用/高毒农药违规咨询或输出，已强制阻断并流转专家工作台复核"
        elif confidence < 0.70:
            need_expert = True
            expert_reason = expert_reason or f"决策置信度 ({confidence}) 低于安全阈值 (0.70)，自动流转农技专家复核"

        # 视觉初诊"建议转交农技专家复核"是权威硬闸，独立于模型结论与置信度生效；
        # 只并入视觉工具标记 —— RAG 检索的 need_expert 属提示性，不机械 OR 全部工具。
        if vision_need_expert:
            need_expert = True
            expert_reason = expert_reason or "多模态视觉初诊建议转交农技专家复核，已强制流转专家工作台"

        # Missing applicable retrieval evidence must not become an approval for input application or diagnosis.
        # A low heuristic retrieval confidence alone is not an unconditional gate for every general question.
        knowledge_gap = decision_type in {"SPRAY_DECISION", "FERTILIZATION", "DIAGNOSIS"} and not (rag_info and rag_info.get("items"))
        if knowledge_gap:
            can_proceed = False
            need_expert = True
            gap_reason = "缺少满足当前作物、版本、日期及检索门限条件的知识依据，需人工复核"
            expert_reason = expert_reason or gap_reason
            final_risks = list(dict.fromkeys([*final_risks, gap_reason]))
            if not (has_banned_violation or has_weather_block):
                final_conclusion = "【知识依据不足/待复核】" + gap_reason
                filtered_recommendations = ["补充可核验的适用资料，并由农技专家复核。", "资料不足时不据此自动执行用药、施肥或确诊。"]

        # 6. 兜底推荐建议
        if not filtered_recommendations:
            if has_banned_violation:
                filtered_recommendations = [
                    "坚决严禁购买和施用国家明令禁限用高毒农药，依法依规开展绿色防控",
                    "建议咨询当地农技推广站，改用生物农药或农业农村部登记推荐的正规低毒低残留药剂"
                ]
            elif has_weather_block:
                filtered_recommendations = [
                    "延后至风停雨过、叶面干燥且光照适宜时作业",
                    "如遇设施大棚高湿病害暴发，可改用安全无硫烟熏剂封闭防控"
                ]
            else:
                filtered_recommendations = [
                    "严格遵守《农药管理条例》与食品安全国家标准",
                    "施药时做好个人防护，严格遵守安全用药间隔期"
                ]

        total_dur_ms = round((time.perf_counter() - start_time) * 1000, 2)

        if is_gdd and gdd_info is None and not (has_banned_violation or has_weather_block):
            can_proceed = False
            need_expert = True
            confidence = min(confidence, 0.30)
            final_conclusion = "【数据不足】缺少定植以来的完整历史温度序列，不能计算当前累计积温或预测采收天数。"
            filtered_recommendations = ["先补齐带日期的历史温度记录，再计算积温。", "物候与采收判断需要结合真实长势及农技专家意见。"]

        # Advisory flags must never coexist with an approval. Proposed sprays also
        # require a product-label contract which this application does not yet hold.
        if is_spray or decision_type == 'SPRAY_DECISION' or '采收' in query or '采摘' in query:
            need_expert = True
            expert_reason = expert_reason or '产品标签、适用条件与安全间隔期需要人工核验'
            if field_id is not None:
                from app.services.harvest_safety_service import harvest_safety
                crop_context = (field_info or {}).get('current_crop') or {}
                safety = await harvest_safety(db, field_id, request.crop_planting_id or crop_context.get('crop_planting_id'))
                evidences.append({'source': '完整批次施药履历核查', 'content': safety})
                final_risks = list(dict.fromkeys([*final_risks, *safety['reasons']]))
        if need_expert:
            can_proceed = False
            if not final_conclusion.startswith(('【法规', '【气象', '【知识', '【数据')):
                final_conclusion = '【待专家复核，不构成作业许可】' + (expert_reason or '现有依据不足，需要专家核查')

        return AgentDecisionResponse(
            query=query,
            conclusion=final_conclusion,
            decision_type=decision_type,
            confidence=confidence,
            can_proceed=can_proceed,
            risks=final_risks,
            recommendations=filtered_recommendations,
            evidences=evidences,
            tool_traces=tool_traces,
            need_expert=need_expert,
            expert_reason=expert_reason,
            total_duration_ms=total_dur_ms,
        )


    @classmethod
    @observed_model("agri_decision_model")
    async def _synthesize_with_llm(
        cls,
        query: str,
        detected_crop: str,
        detected_stage: str,
        detected_disease: Optional[str],
        context_str: str,
        weather_risk_summary: List[str],
        diagnosis_info: Optional[Dict[str, Any]],
        rag_info: Optional[Dict[str, Any]],
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """真实调用 SiliconFlow DeepSeek-V3 执行严谨的农业研判与风险仲裁"""
        api_key = api_key if api_key is not None else getattr(settings, "LLM_API_KEY", "") or os.getenv("LLM_API_KEY", "")
        base_url = (base_url or getattr(settings, "LLM_BASE_URL", "") or os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")).rstrip("/")
        model = model_name or getattr(settings, "LLM_MODEL", "") or os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3")

        # 若未配置 API Key，执行安全保底，严禁发起无效外部请求
        if not api_key:
            logger.warning("未配置 LLM_API_KEY，农业决策大模型启用保守离线降级机制")
            has_weather_risk = len(weather_risk_summary) > 0 and any(
                any(kw in r for kw in ["降雨", "雨量", "大风", "高温", "风速"])
                for r in weather_risk_summary
            )
            return {
                "conclusion": (
                    "【气象硬阻断/严禁打药】检测到作业气象风险，严禁打药作业以防药液冲刷飘移危害。"
                    if has_weather_risk
                    else "【待复核】决策模型不可用，当前不能给出作业批准。"
                ),
                "can_proceed": False,
                "confidence": 0.30,
                "risks": weather_risk_summary + (["大模型决策服务未配置 API 密钥，缺乏在线高阶研判"] if not has_weather_risk else []),
                "recommendations": ["严格按照技术规程操作", "施药作业前人工核查田间温湿度与风力实况", "严格遵守农药安全用药间隔期(PHI)"],
                "need_expert": True,
                "expert_reason": "大模型决策服务离线/未配置 API 密钥，已自动触发安全转交专家复核"
            }

        system_prompt = (
            "你是AgriMind农业资料分析助手。\n"
            "知识的来源名称、分级和日期是录入标签，未自动验证权威性；不得将所有候选称为已核验国家标准。\n"
            "你的任务是综合田块档案、室外天气参考、明确时间和位置的人工观测、近期农事用药历史、多模态病害视觉报告及已录入的候选知识，"
            "对农户的咨询执行严谨的农学研判、风险审查与冲突仲裁。\n"
            "【核心决策准则】\n"
            "1. 环境边界：室外天气只作参考，设施类型未知或温室/拱棚不得将室外风雨温湿度直接当作设施内条件，也不得直接套用露地阈值。人工观测只代表记录时间和位置，过期、设施变化或位置不匹配不能当作当前依据；无风险信息不等于适宜作业。天气规则是系统预防性策略，不是已核验国标；任何施药/采收均需标签及专家复核，can_proceed=False。\n"
            "2. 安全间隔期(PHI)与抗药性原则：若近期曾施药或临近采收期，必须提示农药安全间隔期(PHI)与抗药性轮换用药要求。\n"
            "3. 法定禁限用农药绝对阻断原则：严禁推荐任何国家法定禁用或高毒限用农药（包括克百威、呋喃丹、甲胺磷、甲基对硫磷、氧乐果、水胺硫磷、甲基异柳磷、涕灭威、神农丹、灭多威、六六六、滴滴涕、三氯杀螨醇、毒死蜱、三唑磷、氟虫腈、杀扑磷、甲拌磷、久效磷等）。若用户咨询使用此类农药，必须判定 can_proceed=false，并在 conclusion 明确以【法规硬阻断】开头警告法律禁止使用！\n"
            "4. 疑难病害转交原则：若病害视觉初诊置信度<0.70或病征模糊，将 need_expert 设为 true，并给出清晰转交原因。\n"
            "5. 严格输出标准 JSON 格式，绝不要包含 markdown 标记或任何闲聊："
            "{\n"
            '  "conclusion": "【作业研判/气象硬阻断/法规硬阻断】具体结论总结",\n'
            '  "can_proceed": true/false,\n'
            '  "confidence": 0.90,\n'
            '  "risks": ["具体风险项1", "具体风险项2"],\n'
            '  "recommendations": ["步骤指导1", "实操注意事项2"],\n'
            '  "need_expert": false,\n'
            '  "expert_reason": null\n'
            "}"
        )

        user_content = (
            f"农户咨询问题: {query}\n"
            f"作物与物候: 作物【{detected_crop}】，当前处于【{detected_stage}】物候期\n"
            f"疑似病害: {detected_disease or '未明确指定'}\n"
            f"微气象风险预警: {'; '.join(weather_risk_summary) if weather_risk_summary else '未提供有效风险信息，不能据此认定天气适宜'}\n"
            f"现场记录与知识库候选证据汇总:\n{context_str or '暂无现场数据'}\n"
            f"请根据上述记录给出结构化分析，注明缺失及待核验内容。"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.1,
            "max_tokens": 800
        }

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                with observe_operation("agri_decision_model_http") as measurement:
                    resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                    measurement.outcome = "success" if resp.status_code == 200 else "error"
                if resp.status_code == 200:
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r"^```json\s*", "", raw)
                    cleaned = re.sub(r"^```\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        set_operation_outcome("success")
                        return parsed
                    logger.error(f"农业决策大模型返回非对象 JSON: {type(parsed).__name__}")
                else:
                    logger.error("农业决策模型HTTP失败: %s", resp.status_code)
        except Exception as e:
            logger.error("农业决策模型调用异常: %s", type(e).__name__)

        # 客观降级逻辑（真实标注大模型离线/网络受阻，绝不伪造虚假高置信度 0.85）
        has_weather_risk = len(weather_risk_summary) > 0 and any(
            any(kw in r for kw in ["降雨", "雨量", "大风", "高温", "风速"])
            for r in weather_risk_summary
        )
        return {
            "conclusion": (
                "【气象硬阻断/严禁打药】检测到作业气象风险，严禁打药作业以防药液冲刷飘移危害。"
                if has_weather_risk
                else "【待复核】决策模型不可用，当前不能给出作业批准。"
            ),
            "can_proceed": False,
            "confidence": 0.30,
            "risks": weather_risk_summary + (["农业大模型在线推理服务暂不可用"] if not has_weather_risk else []),
            "recommendations": ["严格按照技术规程操作", "施药作业前人工核查田间温湿度与风力实况", "严格遵守农药安全用药间隔期(PHI)"],
            "need_expert": True,
            "expert_reason": "大模型研判服务在线调用异常，触发系统安全兜底"
        }
