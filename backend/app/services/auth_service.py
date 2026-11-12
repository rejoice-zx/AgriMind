"""Authentication Service - JWT Token and Password Management"""
from datetime import datetime, timedelta, timezone
import re
import hmac
from typing import Optional, Tuple
from jose import JWTError, jwt
import asyncio
import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from app.config import settings
from app.models.user import User

# Fixed bcrypt hash of a random throwaway string; only used to equalize timing for unknown usernames.
_TIMING_DUMMY_HASH = "$2b$12$gXh.aDWdFW0Mxfsmsu73KeRSiA73hpPU2h9kyQ2lgrY3a.pkhr3C6"


class AuthService:
    """Authentication service for JWT tokens and password management"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """
        Hash a password using bcrypt.
        
        Args:
            password: Plain text password
            
        Returns:
            Hashed password string
        """
        # Encode password to bytes, hash it, and decode back to string
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """
        Verify a password against its hash.
        
        Args:
            plain_password: Plain text password to verify
            hashed_password: Stored password hash
            
        Returns:
            True if password matches, False otherwise
        """
        try:
            password_bytes = plain_password.encode('utf-8')
            hashed_bytes = hashed_password.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hashed_bytes)
        except Exception:
            return False
    
    @staticmethod
    def create_access_token(
        data: dict,
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Create a JWT access token.
        
        Args:
            data: Payload data to encode in the token
            expires_delta: Optional custom expiration time
            
        Returns:
            Encoded JWT token string
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
            )
        to_encode.update({
            "exp": expire,
            "type": "access"
        })
        return jwt.encode(
            to_encode,
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM
        )

    @staticmethod
    def create_refresh_token(
        data: dict,
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Create a JWT refresh token.
        
        Args:
            data: Payload data to encode in the token
            expires_delta: Optional custom expiration time
            
        Returns:
            Encoded JWT refresh token string
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
            )
        to_encode.update({
            "exp": expire,
            "type": "refresh"
        })
        return jwt.encode(
            to_encode,
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM
        )
    
    @staticmethod
    def create_tokens(user_id: int, username: str, auth_subject: str) -> Tuple[str, str, int]:
        """
        Create both access and refresh tokens for a user.
        
        Args:
            user_id: User's database ID
            username: User's username
            
        Returns:
            Tuple of (access_token, refresh_token, expires_in_seconds)
        """
        if not isinstance(auth_subject, str) or not auth_subject:
            raise ValueError("A persisted account subject is required to issue credentials")
        token_data = {"sub": str(user_id), "username": username, "account_subject": auth_subject}
        access_token = AuthService.create_access_token(token_data)
        refresh_token = AuthService.create_refresh_token(token_data)
        expires_in = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        return access_token, refresh_token, expires_in
    
    @staticmethod
    def verify_token(token: str, token_type: str = "access") -> Optional[dict]:
        """
        Verify and decode a JWT token.
        
        Args:
            token: JWT token string to verify
            token_type: Expected token type ("access" or "refresh")
            
        Returns:
            Decoded token payload if valid, None otherwise
        """
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM]
            )
            # Verify token type
            if payload.get("type") != token_type:
                return None
            return payload
        except JWTError:
            return None
    
    @staticmethod
    async def authenticate_user(
        db: AsyncSession,
        username: str,
        password: str
    ) -> Optional[User]:
        """
        Authenticate a user by username and password.
        
        Args:
            db: Database session
            username: User's username
            password: User's plain text password
            
        Returns:
            User object if authentication successful, None otherwise
        """
        result = await db.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()
        
        if not user:
            # Spend comparable time for unknown usernames so timing does not reveal accounts (BUG-018).
            await asyncio.to_thread(AuthService.verify_password, password, _TIMING_DUMMY_HASH)
            return None
        if not await asyncio.to_thread(AuthService.verify_password, password, user.password_hash):
            return None
        if not user.is_active or user.registration_status in ('pending', 'rejected'):
            return None
        
        return user
    
    @staticmethod
    async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
        """
        Get a user by their ID.
        
        Args:
            db: Database session
            user_id: User's database ID
            
        Returns:
            User object if found, None otherwise
        """
        result = await db.execute(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def resolve_access_identity(db: AsyncSession, token: str) -> Optional[Tuple[str, Optional[User]]]:
        """Resolve a signed access identity against current account state.

        Guest identities remain separate from registered user identities. Legacy
        namespaced guest subjects are accepted without requiring today's UUID shape.
        Registered roles and activity come from a fresh database query, never JWT claims.
        """
        payload = AuthService.verify_token(token, token_type="access")
        if not payload:
            return None
        subject = payload.get("sub")
        if payload.get("role") == "guest":
            if not isinstance(subject, str) or len(subject) > 64:
                return None
            if not re.fullmatch(r"(?:visitor|guest)_[A-Za-z0-9_-]+", subject):
                return None
            return subject, None
        user = await AuthService.resolve_registered_payload(db, payload)
        if user is None:
            return None
        return f"user_{user.id}", user

    @staticmethod
    async def resolve_registered_payload(db: AsyncSession, payload: dict) -> Optional[User]:
        """Bind a credential to an active account lifetime, not a reusable row ID."""
        if payload.get("role") == "guest":
            return None
        subject = payload.get("sub")
        account_subject = payload.get("account_subject")
        if (not isinstance(subject, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", subject)
                or not isinstance(account_subject, str)
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", account_subject)):
            return None
        user_id = int(subject)
        if user_id > 2**63 - 1:
            return None
        user = await AuthService.get_user_by_id(db, user_id)
        if not user or not user.is_active or user.registration_status in ('pending', 'rejected') or not user.auth_subject:
            return None
        if not hmac.compare_digest(account_subject, user.auth_subject):
            return None
        return user

    @staticmethod
    async def resolve_guest_visitor_id(db: AsyncSession, token: Optional[str]) -> Optional[str]:
        """Verified guest subject for a signed guest access token, else None.

        Only a valid access token whose role is guest yields a subject; registered or
        malformed tokens return None, so they can never authorize a guest-conversation
        claim. Legacy `guest_`/`visitor_` subjects are accepted by resolve_access_identity.
        """
        if not token:
            return None
        identity = await AuthService.resolve_access_identity(db, token)
        if identity is None or identity[1] is not None:
            return None
        return identity[0]

    @staticmethod
    async def get_user_by_username(
        db: AsyncSession,
        username: str
    ) -> Optional[User]:
        """
        Get a user by their username.
        
        Args:
            db: Database session
            username: User's username
            
        Returns:
            User object if found, None otherwise
        """
        result = await db.execute(
            select(User).where(User.username == username)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def create_user(
        db: AsyncSession,
        username: str,
        password: str,
        display_name: str,
        email: Optional[str] = None,
        role: str = "customer",
        is_active: bool = False,
        phone: Optional[str] = None,
        registration_status: Optional[str] = None
    ) -> User:
        """
        Create a new user.
        
        Args:
            db: Database session
            username: User's username
            password: User's plain text password (will be hashed)
            display_name: User's display name
            email: Optional email address
            role: User role (default: "cs")
            is_active: Whether user is active (default: False for pending approval)
            
        Returns:
            Created User object
        """
        password_hash = await asyncio.to_thread(AuthService.hash_password, password)
        user = User(
            username=username,
            password_hash=password_hash,
            display_name=display_name,
            email=email,
            role=role,
            is_active=is_active,
            phone=phone,
            registration_status=registration_status
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, '用户名已存在') from None
        await db.refresh(user)
        return user
    
    @staticmethod
    async def update_last_login(db: AsyncSession, user: User) -> None:
        """
        Update user's last login timestamp.
        
        Args:
            db: Database session
            user: User object to update
        """
        user.last_login = datetime.now(timezone.utc)
        await db.commit()


# Convenience functions for direct import
hash_password = AuthService.hash_password
verify_password = AuthService.verify_password
create_access_token = AuthService.create_access_token
create_refresh_token = AuthService.create_refresh_token
create_tokens = AuthService.create_tokens
verify_token = AuthService.verify_token
