"""User Model"""
from sqlalchemy import Column, Integer, String, Boolean
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime
import uuid


class User(Base):
    """User database model"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    # Numeric SQLite IDs can be reused after deletion. JWTs bind to this account
    # lifetime instead, so old credentials never become credentials for a new row.
    auth_subject = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    role = Column(String(20), default="customer")  # admin, expert, cs, or customer
    display_name = Column(String(100))
    email = Column(String(100))
    phone = Column(String(16), nullable=True)  # Legacy accounts may not have a contact yet.
    registration_status = Column(String(20), nullable=True, index=True)
    registration_reviewed_at = Column(UTCDateTime, nullable=True)
    registration_reviewed_by_subject = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(UTCDateTime, default=utcnow)
    last_login = Column(UTCDateTime)
    
    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"
