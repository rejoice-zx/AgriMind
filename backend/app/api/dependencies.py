"""API Dependencies - Authentication and Authorization"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional, Tuple

from app.database import get_db
from app.services.auth_service import AuthService
from app.models.user import User
from app.core.roles import ADMIN_VALUES, CHAT_VALUES


# HTTP Bearer token security scheme
security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Dependency to get the current authenticated user from JWT token.
    
    Args:
        credentials: Bearer token from Authorization header
        db: Database session
        
    Returns:
        Authenticated User object
        
    Raises:
        HTTPException: 401 if token is invalid or user not found
    """
    token = credentials.credentials
    
    # Verify access token
    payload = AuthService.verify_token(token, token_type="access")
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Guest tokens cannot access staff-only endpoints
    if payload.get("role") == "guest":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="访客令牌无法访问此接口",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check the account lifetime as well as current account state. A reused
    # SQLite row ID must never turn an old credential into another user's JWT.
    user = await AuthService.resolve_registered_payload(db, payload)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return user

async def get_optional_access_identity(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db)
) -> Optional[Tuple[str, Optional[User]]]:
    """
    Missing credentials preserve anonymous behavior; supplied invalid or revoked
    credentials are rejected before callers can fall back to anonymous identity.
    """
    if not credentials:
        if "authorization" in request.headers:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少或无效的访问令牌", headers={"WWW-Authenticate": "Bearer"})
        return None

    identity = await AuthService.resolve_access_identity(db, credentials.credentials)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效、过期或已撤销的访问令牌", headers={"WWW-Authenticate": "Bearer"})
    return identity


async def get_optional_user(
    identity: Optional[Tuple[str, Optional[User]]] = Depends(get_optional_access_identity),
) -> Optional[User]:
    """Return the current DB user, or None for absent auth or a validated guest."""
    return identity[1] if identity is not None else None


async def get_customer_id_from_token(
    identity: Optional[Tuple[str, Optional[User]]] = Depends(get_optional_access_identity),
) -> Optional[str]:
    """Return a validated guest or currently active registered customer identity."""
    return identity[0] if identity is not None else None


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Dependency to ensure user is active.
    
    Args:
        current_user: Current authenticated user
        
    Returns:
        Active User object
        
    Raises:
        HTTPException: 401 if user is not active
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户已被禁用"
        )
    return current_user



# Role-based permission mapping
# Maps roles to allowed API endpoint prefixes/patterns
# 角色名常量的单一来源是 app.core.roles（本表只保留旧静态清单中仍挂载的前缀，农业资源权限由各业务API检查）
ROLE_PERMISSIONS = {
    "admin": [
        "/api/auth",
        "/api/knowledge",
        "/api/statistics",
        "/api/performance",
        "/api/logs",
        "/api/settings",
        "/api/users",
    ],
    "cs": [
        "/api/auth",
    ],
}


def require_roles(allowed_roles: List[str]):
    """
    Dependency factory to require specific roles for an endpoint.
    
    Args:
        allowed_roles: List of role names that can access the endpoint
        
    Returns:
        Dependency function that validates user role
        
    Example:
        @router.get("/admin-only")
        async def admin_endpoint(user: User = Depends(require_roles(["admin"]))):
            ...
    """
    async def role_checker(
        current_user: User = Depends(get_current_user)
    ) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足"
            )
        return current_user
    
    return role_checker


def require_admin():
    """
    Dependency to require admin role.
    
    Returns:
        Dependency function that validates admin role
    """
    return require_roles(sorted(ADMIN_VALUES))


class RBACChecker:
    """
    RBAC permission checker class for more complex permission scenarios.
    Can be used as a dependency or called directly.
    """
    
    def __init__(self, allowed_roles: List[str]):
        """
        Initialize RBAC checker with allowed roles.
        
        Args:
            allowed_roles: List of role names that can access the resource
        """
        self.allowed_roles = allowed_roles
    
    async def __call__(
        self,
        current_user: User = Depends(get_current_user)
    ) -> User:
        """
        Check if current user has required role.
        
        Args:
            current_user: Current authenticated user
            
        Returns:
            User if authorized
            
        Raises:
            HTTPException: 403 if user lacks required role
        """
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足"
            )
        return current_user
    
    @staticmethod
    def check_permission(user_role: str, endpoint: str) -> bool:
        """
        Check if a role has permission to access an endpoint.
        
        Args:
            user_role: User's role
            endpoint: API endpoint path
            
        Returns:
            True if role has permission, False otherwise
        """
        allowed_endpoints = ROLE_PERMISSIONS.get(user_role, [])
        return any(endpoint.startswith(prefix) for prefix in allowed_endpoints)
    
    @staticmethod
    def get_allowed_endpoints(user_role: str) -> List[str]:
        """
        Get list of allowed endpoint prefixes for a role.
        
        Args:
            user_role: User's role
            
        Returns:
            List of allowed endpoint prefixes
        """
        return ROLE_PERMISSIONS.get(user_role, [])


# Convenience instances for common role checks (role sets come from app.core.roles)
AdminRequired = RBACChecker(sorted(ADMIN_VALUES))
CSOrAdminRequired = RBACChecker(sorted(CHAT_VALUES))
