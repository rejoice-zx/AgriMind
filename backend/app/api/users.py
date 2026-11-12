# -*- coding: utf-8 -*-
"""
用户管理API端点 - User Management API Endpoints

功能:
- 用户CRUD操作
- 注册审批
- 密码重置
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from typing import Optional, List, Literal
from datetime import datetime, timezone

from app.database import get_db
from app.models.user import User
from app.models.conversation import Conversation
from app.models.farm import Farm
from app.services.auth_service import AuthService
from app.api.dependencies import AdminRequired
from app.core.roles import is_admin
import uuid
import logging
from pydantic import BaseModel, Field, EmailStr
from app.schemas.contact import PhoneNumber, PasswordText
from app.utils.time import utcnow


router = APIRouter(prefix="/api/users", tags=["Users"])
logger = logging.getLogger(__name__)


# ==================== Schemas ====================

class UserResponse(BaseModel):
    """用户响应"""
    id: int
    username: str
    role: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    registration_status: Optional[str] = None
    registration_reviewed_at: Optional[datetime] = None
    is_active: bool
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    """用户列表响应"""
    items: List[UserResponse]
    total: int
    page: int
    page_size: int


class CreateUserRequest(BaseModel):
    """创建用户请求"""
    username: str = Field(..., min_length=3, max_length=50)
    password: PasswordText

    display_name: str = Field(..., min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    phone: PhoneNumber
    role: str = Field(default="customer", pattern="^(admin|expert|cs|customer)$")


class UpdateUserRequest(BaseModel):
    """更新用户请求"""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    role: Optional[str] = Field(None, pattern="^(admin|expert|cs|customer)$")
    is_active: Optional[bool] = None
    phone: Optional[PhoneNumber] = None


class ResetPasswordRequest(BaseModel):
    """重置密码请求"""
    password: PasswordText



def _guard_other_admin(target: User, current_user: User, action: str) -> None:
    """Only the super administrator may reset, disable or demote another administrator (BUG-020).

    Mirrors the existing delete rule, so an ordinary administrator cannot take over the
    super administrator account by resetting its password or demoting it.
    """
    if target.id != current_user.id and is_admin(target) and current_user.username != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"只有超级管理员才能对其他管理员执行此操作（{action.rstrip('：')}）")


# ==================== API Endpoints ====================

@router.get("", response_model=UserListResponse)
async def get_users(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    role: Optional[str] = Query(None, description="角色筛选"),
    is_active: Optional[bool] = Query(None, description="状态筛选"),
    username: Optional[str] = Query(None, description="用户名搜索"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    stmt = select(User)
    count_stmt = select(func.count(User.id))
    
    if role:
        stmt = stmt.where(User.role == role)
        count_stmt = count_stmt.where(User.role == role)
    
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
        count_stmt = count_stmt.where(User.is_active == is_active)
    
    if username:
        stmt = stmt.where(User.username.ilike(f"%{username}%"))
        count_stmt = count_stmt.where(User.username.ilike(f"%{username}%"))
    
    # 按角色分组：admin > expert > cs > customer，同角色按 ID 升序
    from sqlalchemy import case
    role_order = case(
        (User.role == "admin", 0),
        (User.role == "expert", 1),
        (User.role == "cs", 2),
        else_=3
    )
    stmt = stmt.order_by(role_order, User.id.asc())
    
    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)
    
    # 执行查询
    result = await db.execute(stmt)
    users = list(result.scalars().all())
    
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0
    
    return UserListResponse(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/pending", response_model=List[UserResponse])
async def get_pending_registrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    获取待审批的注册申请
    
    Returns: 待审批用户列表
    """
    stmt = select(User).where(User.registration_status == 'pending').order_by(User.created_at.desc(), User.id.desc())
    result = await db.execute(stmt)
    users = result.scalars().all()
    
    return [UserResponse.model_validate(u) for u in users]


@router.get('/registrations', response_model=UserListResponse)
async def list_registrations(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    registration_status: Optional[Literal['pending', 'approved', 'rejected']] = Query(None),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(AdminRequired)
):
    """All submitted registrations, including automatically approved farmer accounts."""
    condition = User.registration_status.is_not(None)
    if registration_status:
        condition = condition & (User.registration_status == registration_status)
    total = await db.scalar(select(func.count(User.id)).where(condition))
    users = (await db.scalars(select(User).where(condition)
        .order_by(User.created_at.desc(), User.id.desc()).offset((page-1)*page_size).limit(page_size))).all()
    return UserListResponse(items=[UserResponse.model_validate(user) for user in users],
                            total=total or 0, page=page, page_size=page_size)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    获取用户详情
    
    - **user_id**: 用户ID
    
    Returns: 用户详细信息
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    return UserResponse.model_validate(user)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    创建新用户（管理员直接创建，无需审批）
    
    - **username**: 用户名（3-50字符）
    - **password**: 密码（至少6字符）
    - **display_name**: 显示名称
    - **email**: 邮箱（可选）
    - **role**: 管理员、农技专家、农户或历史客服
    
    Returns: 新创建的用户信息
    """
    # 检查用户名是否已存在
    existing_user = await AuthService.get_user_by_username(db, request.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在"
        )
    
    # 创建用户（直接激活）
    user = await AuthService.create_user(
        db=db,
        username=request.username,
        password=request.password,
        display_name=request.display_name,
        email=request.email,
        role=request.role,
        is_active=True,
        phone=request.phone
    )
    
    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    request: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    更新用户信息
    
    - **user_id**: 用户ID
    - **display_name**: 新显示名称（可选）
    - **email**: 新邮箱（可选）
    - **role**: 新角色（可选）
    - **is_active**: 新状态（可选）
    
    Returns: 更新后的用户信息
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )

    if user.registration_status in ('pending', 'rejected') and (request.is_active is True or
            (request.role is not None and request.role != user.role)):
        raise HTTPException(409, '注册申请须在审核列表处理，不能通过修改角色或启用账号绕过审核')
    if 'phone' in request.model_fields_set and request.phone is None:
        raise HTTPException(422, '联系电话不能为空')
    
    if (request.role is not None and request.role != user.role) or request.is_active is False:
        _guard_other_admin(user, current_user, "修改角色或停用")

    # 不允许修改自己的角色和状态
    if user.id == current_user.id:
        if request.role is not None and request.role != user.role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能修改自己的角色"
            )
        if request.is_active is not None and request.is_active != user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能修改自己的状态"
            )
    
    from app.services.agri_history_service import AgriHistoryService
    if request.is_active is False or (request.role is not None and request.role not in {"expert", "admin"}):
        await AgriHistoryService.release_expert(db, user.id)
        from app.services.disease_review_service import DiseaseReviewService
        await DiseaseReviewService.release_expert(db, user.id)

    # 更新字段
    if request.display_name is not None:
        user.display_name = request.display_name
    if request.email is not None:
        user.email = request.email
    if request.phone is not None:
        user.phone = request.phone
    if request.role is not None:
        user.role = request.role
    if request.is_active is not None:
        if user.is_active and not request.is_active:
            # Re-enabling the account must not revive pre-disable access/refresh.
            user.auth_subject = str(uuid.uuid4())
        user.is_active = request.is_active
    
    await db.commit()
    await db.refresh(user)
    
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    删除用户
    
    - **user_id**: 用户ID
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 不允许删除自己
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己"
        )
    
    # 只有超级管理员(username=admin)才能删除其他管理员
    if is_admin(user) and current_user.username != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有超级管理员才能删除其他管理员"
        )

    # D2 策略：农场档案保留不丢——拥有农场的用户不允许直接删除（须先转移/处置其农场）。
    owned_farms = await db.scalar(
        select(func.count()).select_from(Farm).where(Farm.owner_id == user.id)
    )
    if owned_farms:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该用户名下仍有农场，请先转移或处置其农场后再删除"
        )

    # D2 策略：保留并匿名化其会话与客服引用，不做级联删除。
    # 1) 会话归属匿名化（user_{id} -> deleted_user_{id}），消息保留
    await db.execute(
        update(Conversation)
        .where(Conversation.customer_id == f"user_{user.id}")
        .values(customer_id=f"deleted_user_{user.id}")
    )
    # In-progress cases must become claimable again, not human_handling/NULL.
    await db.execute(
        update(Conversation)
        .where(Conversation.human_agent_id == user.id,
               Conversation.status == "human_handling")
        .values(human_agent_id=None, status="pending_human", updated_at=datetime.now(timezone.utc))
    )
    # 2) 解除其作为客服的其余引用（该 FK 无 ondelete，须显式处理）
    await db.execute(
        update(Conversation)
        .where(Conversation.human_agent_id == user.id)
        .values(human_agent_id=None)
    )

    from app.services.agri_history_service import AgriHistoryService
    await AgriHistoryService.release_expert(db, user.id)
    from app.services.disease_review_service import DiseaseReviewService
    await DiseaseReviewService.release_expert(db, user.id)
    await db.delete(user)
    await db.commit()
    # Retired customer-service WebSockets have no current subscribers.


@router.post("/{user_id}/approve", response_model=UserResponse)
async def approve_registration(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    审批通过注册申请
    
    - **user_id**: 用户ID
    
    Returns: 更新后的用户信息
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    if user.registration_status == 'approved' and user.is_active:
        return UserResponse.model_validate(user)
    if user.registration_status != 'pending':
        raise HTTPException(409, '此用户没有待审核注册申请')
    changed = await db.scalar(update(User).where(User.id == user_id, User.registration_status == 'pending')
        .values(is_active=True, registration_status='approved', registration_reviewed_at=utcnow(),
                registration_reviewed_by_subject=current_user.auth_subject).returning(User.id))
    if changed is None:
        await db.rollback()
        raise HTTPException(409, '申请状态已变化，请刷新审核列表')
    await db.commit()
    await db.refresh(user)
    
    return UserResponse.model_validate(user)


@router.post("/{user_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_registration(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    拒绝注册申请（保留申请记录与审核信息）
    
    - **user_id**: 用户ID
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    if user.registration_status == 'rejected':
        return
    if user.registration_status != 'pending':
        raise HTTPException(409, '此用户没有待审核注册申请')
    changed = await db.scalar(update(User).where(User.id == user_id, User.registration_status == 'pending')
        .values(is_active=False, registration_status='rejected', registration_reviewed_at=utcnow(),
                registration_reviewed_by_subject=current_user.auth_subject).returning(User.id))
    if changed is None:
        await db.rollback()
        raise HTTPException(409, '申请状态已变化，请刷新审核列表')
    await db.commit()


@router.post("/{user_id}/reset-password", response_model=UserResponse)
async def reset_password(
    user_id: int,
    request: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(AdminRequired)
):
    """
    重置用户密码
    
    - **user_id**: 用户ID
    - **password**: 新密码（至少6字符）
    
    Returns: 用户信息
    """
    user = await AuthService.get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    _guard_other_admin(user, current_user, "重置密码：")
    user.password_hash = await asyncio.to_thread(AuthService.hash_password, request.password)
    user.auth_subject = str(uuid.uuid4())
    await db.commit()
    await db.refresh(user)
    
    return UserResponse.model_validate(user)
