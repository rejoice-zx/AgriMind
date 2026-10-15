"""Tests for Authentication Module"""
import pytest
from datetime import timedelta

from app.services.auth_service import AuthService, hash_password, verify_password
from app.models.user import User
from app.api.dependencies import RBACChecker, ROLE_PERMISSIONS


class TestPasswordHashing:
    """Tests for password hashing functionality"""
    
    def test_hash_password_returns_different_value(self):
        """Password hash should not equal original password"""
        password = "test_password_123"
        hashed = hash_password(password)
        assert hashed != password
    
    def test_verify_password_correct(self):
        """Correct password should verify successfully"""
        password = "secure_password"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True
    
    def test_verify_password_incorrect(self):
        """Incorrect password should fail verification"""
        password = "secure_password"
        hashed = hash_password(password)
        assert verify_password("wrong_password", hashed) is False
    
    def test_hash_password_unique_salts(self):
        """Same password should produce different hashes (unique salts)"""
        password = "same_password"
        hash1 = hash_password(password)
        hash2 = hash_password(password)
        assert hash1 != hash2
        # But both should verify correctly
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True


class TestJWTTokens:
    """Tests for JWT token functionality"""
    
    def test_create_access_token(self):
        """Access token should be created successfully"""
        data = {"sub": "1", "username": "testuser"}
        token = AuthService.create_access_token(data)
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0
    
    def test_create_refresh_token(self):
        """Refresh token should be created successfully"""
        data = {"sub": "1", "username": "testuser"}
        token = AuthService.create_refresh_token(data)
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0
    
    def test_verify_access_token_valid(self):
        """Valid access token should verify successfully"""
        data = {"sub": "1", "username": "testuser"}
        token = AuthService.create_access_token(data)
        payload = AuthService.verify_token(token, token_type="access")
        assert payload is not None
        assert payload["sub"] == "1"
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"
    
    def test_verify_refresh_token_valid(self):
        """Valid refresh token should verify successfully"""
        data = {"sub": "1", "username": "testuser"}
        token = AuthService.create_refresh_token(data)
        payload = AuthService.verify_token(token, token_type="refresh")
        assert payload is not None
        assert payload["sub"] == "1"
        assert payload["type"] == "refresh"
    
    def test_verify_token_wrong_type(self):
        """Token verified with wrong type should fail"""
        data = {"sub": "1", "username": "testuser"}
        access_token = AuthService.create_access_token(data)
        # Try to verify access token as refresh token
        payload = AuthService.verify_token(access_token, token_type="refresh")
        assert payload is None
    
    def test_verify_invalid_token(self):
        """Invalid token should fail verification"""
        payload = AuthService.verify_token("invalid.token.here", token_type="access")
        assert payload is None
    
    def test_create_tokens_returns_tuple(self):
        """create_tokens should return access, refresh, and expires_in"""
        access, refresh, expires_in = AuthService.create_tokens(1, "testuser", "12345678-1234-4234-8234-123456789abc")
        assert access is not None
        assert refresh is not None
        assert expires_in > 0
        # Verify both tokens are valid
        assert AuthService.verify_token(access, "access") is not None
        assert AuthService.verify_token(refresh, "refresh") is not None


class TestRBACPermissions:
    """Tests for RBAC permission checking"""
    
    def test_admin_has_all_permissions(self):
        """Admin role should have access to all endpoints"""
        admin_endpoints = ROLE_PERMISSIONS.get("admin", [])
        assert len(admin_endpoints) > 0
        # Admin should have more permissions than cs
        cs_endpoints = ROLE_PERMISSIONS.get("cs", [])
        assert len(admin_endpoints) > len(cs_endpoints)
    
    def test_cs_limited_permissions(self):
        """CS role should have limited permissions"""
        cs_endpoints = ROLE_PERMISSIONS.get("cs", [])
        assert "/api/auth" in cs_endpoints
        assert all(prefix not in cs_endpoints for prefix in ("/api/chat", "/api/products", "/api/human"))
        # CS should not have admin-only endpoints
        assert "/api/users" not in cs_endpoints
        assert "/api/settings" not in cs_endpoints
        assert "/api/knowledge" not in cs_endpoints
    
    def test_check_permission_admin(self):
        """Admin should have permission to all endpoints"""
        assert RBACChecker.check_permission("admin", "/api/users/list") is True
        assert RBACChecker.check_permission("admin", "/api/settings") is True
        assert RBACChecker.check_permission("admin", "/api/knowledge") is True
    
    def test_check_permission_cs(self):
        """CS should only have permission to allowed endpoints"""
        assert RBACChecker.check_permission("cs", "/api/chat/messages") is False
        assert RBACChecker.check_permission("cs", "/api/products/list") is False
        assert RBACChecker.check_permission("cs", "/api/users/list") is False
        assert RBACChecker.check_permission("cs", "/api/settings") is False
    
    def test_get_allowed_endpoints(self):
        """get_allowed_endpoints should return correct list for role"""
        admin_endpoints = RBACChecker.get_allowed_endpoints("admin")
        cs_endpoints = RBACChecker.get_allowed_endpoints("cs")
        unknown_endpoints = RBACChecker.get_allowed_endpoints("unknown_role")
        
        assert len(admin_endpoints) > 0
        assert len(cs_endpoints) > 0
        assert len(unknown_endpoints) == 0


@pytest.mark.asyncio
async def test_authenticate_user_success(test_session):
    """Test successful user authentication"""
    # Create a test user
    password = "test_password"
    user = User(
        username="auth_test_user",
        password_hash=hash_password(password),
        role="admin",
        display_name="Auth Test User",
        is_active=True
    )
    test_session.add(user)
    await test_session.commit()
    
    # Authenticate
    authenticated = await AuthService.authenticate_user(
        test_session, "auth_test_user", password
    )
    assert authenticated is not None
    assert authenticated.username == "auth_test_user"


@pytest.mark.asyncio
async def test_authenticate_user_wrong_password(test_session):
    """Test authentication with wrong password"""
    password = "correct_password"
    user = User(
        username="wrong_pass_user",
        password_hash=hash_password(password),
        role="cs",
        display_name="Wrong Pass User",
        is_active=True
    )
    test_session.add(user)
    await test_session.commit()
    
    # Try to authenticate with wrong password
    authenticated = await AuthService.authenticate_user(
        test_session, "wrong_pass_user", "wrong_password"
    )
    assert authenticated is None


@pytest.mark.asyncio
async def test_authenticate_user_inactive(test_session):
    """Test authentication with inactive user"""
    password = "test_password"
    user = User(
        username="inactive_user",
        password_hash=hash_password(password),
        role="cs",
        display_name="Inactive User",
        is_active=False
    )
    test_session.add(user)
    await test_session.commit()
    
    # Try to authenticate inactive user
    authenticated = await AuthService.authenticate_user(
        test_session, "inactive_user", password
    )
    assert authenticated is None


@pytest.mark.asyncio
async def test_create_user(test_session):
    """Test user creation through AuthService"""
    user = await AuthService.create_user(
        db=test_session,
        username="new_user",
        password="new_password",
        display_name="New User",
        email="new@example.com",
        role="cs",
        is_active=False
    )
    
    assert user.id is not None
    assert user.username == "new_user"
    assert user.role == "cs"
    assert user.is_active is False
    # Password should be hashed
    assert user.password_hash != "new_password"
    assert verify_password("new_password", user.password_hash) is True
