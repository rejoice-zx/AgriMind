# -*- coding: utf-8 -*-
"""
AgriMind 农事日历与智能计划 API 路由 (agri_plan.py)
对应改造计划第三十三节与分步改进计划第 10 步：
实现 POST /api/agri/plan 与 POST /api/agri/plan/apply 端点。
"""

import logging
from app.core.audit_logging import audit, bind_audit
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.dependencies import get_current_user
from app.models.user import User
from app.schemas.agriculture import (
    AgriPlanRequest,
    AgriPlanResponse,
    AgriPlanApplyRequest,
    AgriPlanApplyResponse,
)
from app.services.agri_plan_service import AgriPlanService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agri", tags=["智能农事规划与日历"])


@router.post(
    "/plan",
    response_model=AgriPlanResponse,
    summary="读取已保存的农事检查建议，必要时重新核查",
)
async def generate_agricultural_plan(
    request: AgriPlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    共用种植批次的持久化计划；跨天补齐窗口，农情变化或过期时核查依据。
    force_refresh 用于手动重新核查天气。输出为检查建议，不是农艺处方。
    """
    return await AgriPlanService.generate_plan(
        db=db,
        request=request,
        user_id=current_user.id,
        user_role=current_user.role,
    )


@router.post(
    "/plan/apply",
    response_model=AgriPlanApplyResponse,
    summary="将确认的农事计划项一键落库沉淀为 FarmingRecord 物理执行记录",
)
async def apply_agricultural_plan(
    request: AgriPlanApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    将农事计划项转化为真实物理执行履历，实现"农田档案 -> 智能决策 -> 农事执行 -> 历史留痕"完整闭环。
    """
    bind_audit(field_id=request.field_id, actor_id=current_user.id)
    operator = current_user.username
    result = await AgriPlanService.apply_plan_items(
        db=db,
        field_id=request.field_id,
        items=request.items,
        operator=operator,
        user_id=current_user.id,
        user_role=current_user.role,
    )
    audit("plan_confirmation_returned", field_id=request.field_id, actor_id=current_user.id)
    return AgriPlanApplyResponse(
        success=True,
        created_count=result.created_count,
        record_ids=result.record_ids,
        message=f"已确认 {len(result.record_ids)} 项今日已完成事项，新增 {result.created_count} 条履历（重复确认不新增）",
    )
