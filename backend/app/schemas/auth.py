"""Authentication Schemas"""
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Literal
from app.schemas.contact import PhoneNumber, PasswordText
from datetime import datetime


class TokenResponse(BaseModel):
    """Token response schema"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    """Token refresh request schema"""
    refresh_token: str


class RegisterRequest(BaseModel):
    """Registration through the compatibility endpoint; privileged roles are unavailable."""
    username: str = Field(..., min_length=3, max_length=50)
    password: PasswordText
    display_name: str = Field(..., min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    phone: PhoneNumber
    role: Literal['customer', 'expert'] = 'expert'


class CustomerRegisterRequest(BaseModel):
    """Customer registration request schema"""
    username: str = Field(..., min_length=3, max_length=50)
    password: PasswordText
    display_name: str = Field(..., min_length=1, max_length=100)
    phone: PhoneNumber
    role: Literal['customer', 'expert'] = 'customer'
    visitor_id: Optional[str] = None  # legacy; NOT used for authorization
    guest_device_id: Optional[str] = Field(None, max_length=128)
    guest_token: Optional[str] = Field(None, max_length=4096)  # signed proof of guest ownership
    guest_conversation_ids: List[str] = Field(default_factory=list, max_length=200)


class LoginRequest(BaseModel):
    """Login request schema"""
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1)
    visitor_id: Optional[str] = None  # legacy; NOT used for authorization
    guest_device_id: Optional[str] = Field(None, max_length=128)
    guest_token: Optional[str] = Field(None, max_length=4096)  # signed proof of guest ownership
    guest_conversation_ids: List[str] = Field(default_factory=list, max_length=200)


class UserInfo(BaseModel):
    """User information schema"""
    id: int
    username: str
    role: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    registration_status: Optional[str] = None
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    """Login response with user info"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserInfo


class GuestTokenResponse(BaseModel):
    """Guest token response"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    visitor_id: str
