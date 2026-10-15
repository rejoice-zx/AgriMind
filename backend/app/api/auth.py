"""Authentication API Endpoints"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import (
    FailureWindowLimiter,
    IssueWindowLimiter,
    client_key,
)
from app.services.auth_service import AuthService
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    TokenResponse,
    RegisterRequest,
    CustomerRegisterRequest,
    UserInfo,
    GuestTokenResponse
)
from app.api.dependencies import get_current_user
from app.models.user import User
from app.services.legacy_identity_service import LegacyIdentityService
import uuid


router = APIRouter(prefix="/api/auth", tags=["Authentication"])

# Dedicated auth budgets (P2-AUTH-BOUNDS / DS-13); module-level so they persist
# across requests and are shared by every worker thread.
_login_failures = FailureWindowLimiter()
_guest_issues = IssueWindowLimiter()

_TOO_MANY = HTTPException(
    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    detail="尝试过于频繁，请稍后重试",
)


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    http_request: Request,
    x_device_id: str | None = Header(None, alias="X-Device-ID"),
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate user and return JWT tokens.
    
    - **username**: User's username
    - **password**: User's password
    
    Returns access token, refresh token, and user info.
    """
    key = client_key(http_request)
    # A known account's success must not erase failures for other accounts/IPs.
    account_key = ("account", request.username)
    ip_key = ("ip", key)
    attempt_keys = (ip_key, account_key)
    if not _login_failures.begin_attempt(attempt_keys):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY.detail,
        )

    user = None
    try:
        user = await AuthService.authenticate_user(
            db, request.username, request.password
        )
    finally:
        _login_failures.finish_attempt(attempt_keys, user is not None, account_key)
    
    if not user:
        candidate = await AuthService.get_user_by_username(db, request.username)
        if candidate and candidate.registration_status in ('pending', 'rejected') and AuthService.verify_password(request.password, candidate.password_hash):
            detail = '账号申请已提交管理员审核，审核通过后才能登录' if candidate.registration_status == 'pending' else '注册申请未通过管理员审核，请联系管理员'
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Only this account's failures are cleared. The aggregate IP budget expires
    # normally, even when an attacker interleaves their own successful logins.
    
    # Update last login timestamp
    await AuthService.update_last_login(db, user)
    
    # Create tokens
    access_token, refresh_token, expires_in = AuthService.create_tokens(
        user.id, user.username, user.auth_subject
    )
    
    # Migrate guest conversations ONLY with a verified signed guest token.
    # Self-reported visitor_id / device / ids cannot authorize a claim (DS-02).
    to_customer_id = f"user_{user.id}"
    verified_visitor_id = await AuthService.resolve_guest_visitor_id(db, request.guest_token)
    await LegacyIdentityService.migrate_guest_conversations(
        db=db,
        to_customer_id=to_customer_id,
        verified_visitor_id=verified_visitor_id,
        device_id=request.guest_device_id or x_device_id,
        conversation_ids=request.guest_conversation_ids,
    )
    
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=UserInfo.model_validate(user)
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Refresh access token using refresh token.
    
    - **refresh_token**: Valid refresh token
    
    Returns new access token and refresh token.
    """
    # Verify refresh token
    payload = AuthService.verify_token(request.refresh_token, token_type="refresh")
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的刷新令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user from token
    user = await AuthService.resolve_registered_payload(db, payload)
    
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create new tokens
    access_token, refresh_token, expires_in = AuthService.create_tokens(
        user.id, user.username, user.auth_subject
    )
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Submit user registration request (requires admin approval).
    
    - **username**: Desired username (3-50 characters)
    - **password**: Password (minimum 6 characters)
    - **display_name**: Display name
    - **email**: Optional email address
    
    Returns success message. Account will be inactive until approved.
    """
    # Check if username already exists
    existing_user = await AuthService.get_user_by_username(db, request.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在"
        )
    
    # Expert applications are inactive until approval; farmers are approved automatically.
    await AuthService.create_user(
        db=db,
        username=request.username,
        password=request.password,
        display_name=request.display_name,
        email=request.email,
        role=request.role,
        is_active=request.role == 'customer',
        phone=request.phone,
        registration_status='pending' if request.role == 'expert' else 'approved'
    )
    
    return {'status': 'pending' if request.role == 'expert' else 'approved',
            'message': '已提交管理员审核，审核通过后才能登录账号' if request.role == 'expert' else '农户账号注册成功，请登录'}


@router.post("/register-customer", status_code=status.HTTP_201_CREATED)
async def register_customer(
    request: CustomerRegisterRequest,
    x_device_id: str | None = Header(None, alias="X-Device-ID"),
    db: AsyncSession = Depends(get_db)
):
    """
    Register a farmer immediately or submit an expert account for admin approval.
    Only successful farmer registration migrates guest conversations and logs in.
    """
    # Check if username already exists
    existing_user = await AuthService.get_user_by_username(db, request.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在"
        )
    
    # Create customer user with is_active=True (no approval needed)
    user = await AuthService.create_user(
        db=db,
        username=request.username,
        password=request.password,
        display_name=request.display_name,
        email=None,
        role=request.role,
        is_active=request.role == 'customer',
        phone=request.phone,
        registration_status='pending' if request.role == 'expert' else 'approved'
    )

    if request.role == 'expert':
        return {'status': 'pending', 'message': '已提交管理员审核，审核通过后才能登录账号'}
    
    # Migrate guest conversations ONLY with a verified signed guest token.
    # Self-reported visitor_id / device / ids cannot authorize a claim (DS-02).
    to_customer_id = f"user_{user.id}"
    verified_visitor_id = await AuthService.resolve_guest_visitor_id(db, request.guest_token)
    await LegacyIdentityService.migrate_guest_conversations(
        db=db,
        to_customer_id=to_customer_id,
        verified_visitor_id=verified_visitor_id,
        device_id=request.guest_device_id or x_device_id,
        conversation_ids=request.guest_conversation_ids,
    )
    
    # Auto-login: create tokens
    access_token, refresh_token, expires_in = AuthService.create_tokens(
        user.id, user.username, user.auth_subject
    )
    
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=UserInfo.model_validate(user)
    )


@router.get("/me", response_model=UserInfo)
async def get_current_user_info(
    current_user: User = Depends(get_current_user)
):
    """
    Get current authenticated user's information.
    
    Requires valid access token in Authorization header.
    """
    return UserInfo.model_validate(current_user)


@router.post("/guest", response_model=GuestTokenResponse)
async def get_guest_token(http_request: Request):
    """
    Issue a guest visitor token for anonymous customers.
    
    Returns a short-lived JWT with a unique visitor_id.
    No credentials required.
    """
    from datetime import timedelta

    # Issuance is budgeted per client so the endpoint cannot be farmed for
    # unlimited guest identities (P2-AUTH-BOUNDS / DS-13).
    if not _guest_issues.allow(client_key(http_request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_TOO_MANY.detail,
        )

    visitor_id = f"visitor_{uuid.uuid4().hex[:12]}"
    
    token = AuthService.create_access_token(
        data={"sub": visitor_id, "role": "guest"},
        expires_delta=timedelta(days=7)
    )
    
    return GuestTokenResponse(
        access_token=token,
        expires_in=7 * 24 * 3600,
        visitor_id=visitor_id
    )
