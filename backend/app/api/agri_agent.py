# -*- coding: utf-8 -*-
"""
AgriMind 农业智能 Agent 与工具链 API 路由
"""

from typing import List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.dependencies import get_current_user
from app.models.user import User
from app.schemas.agent import (
    AgentDecisionRequest,
    AgentDecisionResponse,
    ToolDefinition,
    ToolExecuteRequest,
    ToolExecuteResponse,
)
from app.services.agent_service import AgriAgentService, AgriToolRegistry
from app.services.agri_history_service import AgriHistoryService
from app.core.roles import is_agri_staff
from app.core.analysis_lifecycle import run_owned_analysis
from app.schemas.agri_history import AgentRequestStatus

router = APIRouter(prefix="/api/agent", tags=["农业智能决策Agent"])


async def require_expert_or_admin(current_user: User = Depends(get_current_user)) -> User:
    """权限校验：单步工具调试仅允许管理员和农技专家"""
    if not is_agri_staff(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要农技专家或系统管理员权限进行工具单步调试",
        )
    return current_user


@router.post("/decide", response_model=AgentDecisionResponse, summary="执行农业多工具自主协同决策")
async def execute_agent_decision(
    request: AgentDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    农业智能决策 Agent 主入口:
    - 自动识别用户意图（植保打药、水肥管理、病害诊断、物候预测等）
    - 自主调度 6 大农业核心工具（田块画像、天气预警、农事追溯、Hybrid RAG国家规程、视觉多模态初诊、有效积温GDD）
    - 综合气象降雨、高温漂移、安全间隔期与抗药性进行多重风险仲裁
    - 保存完整问答；需要专家复核时创建通用农业咨询记录
    """
    return await run_owned_analysis(db, request, current_user, 'agent', _run_decision)


async def _run_decision(db, request, current_user):
    if request.image_url:
        from app.services.diagnosis_media_service import DiagnosisMediaService
        from app.services.farm_service import FarmService
        if request.field_id is not None:
            await FarmService.ensure_field_access(db, request.field_id, current_user)
        image_field_id = request.field_id
        if request.crop_planting_id is not None:
            crop = await FarmService.ensure_crop_access(db, request.crop_planting_id, current_user)
            if image_field_id is None: image_field_id = crop.field_id
        await DiagnosisMediaService.access(db, request.image_url, current_user, for_link=True, field_id=image_field_id)
    return await AgriHistoryService.decide(db, request, current_user)


@router.get('/requests/{request_id}', response_model=AgentRequestStatus)
async def get_agent_request(request_id: UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await AgriHistoryService.request_status(db, request_id, current_user)


@router.post('/requests/{request_id}/cancel', response_model=AgentRequestStatus)
async def cancel_agent_request(request_id: UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await AgriHistoryService.cancel_request(db, request_id, current_user)


@router.get("/tools", response_model=List[ToolDefinition], summary="获取当前注册的农业核心工具列表")
async def list_agent_tools(
    current_user: User = Depends(get_current_user),
):
    """
    获取系统注册的 6 大农业核心工具列表及 Function Calling 标准 Schema
    """
    return AgriToolRegistry.get_tool_definitions()


@router.post("/tools/execute", response_model=ToolExecuteResponse, summary="单步调试/直接执行某个农业工具")
async def execute_single_tool(
    request: ToolExecuteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_expert_or_admin),
):
    """
    提供给前端或管理员直接单步调试/调用某个指定工具（需要专家或管理权限）
    """
    if request.tool_name == "diagnose_crop_image":
        from app.services.diagnosis_media_service import DiagnosisMediaService
        await DiagnosisMediaService.access(db, request.arguments.get("image_url"), current_user)
    return await AgriToolRegistry.execute_tool(
        tool_name=request.tool_name,
        arguments=request.arguments,
        db=db,
    )
