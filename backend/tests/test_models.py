"""Tests for Database Models"""
import pytest
from datetime import datetime
import uuid

from app.models import User, Conversation, Message, KnowledgeItem, Product, SystemSettings


@pytest.mark.asyncio
async def test_user_model_creation(test_session):
    """Test User model can be created and persisted"""
    user = User(
        username="testuser",
        password_hash="hashed_password",
        role="admin",
        display_name="Test User",
        email="test@example.com",
        is_active=True
    )
    
    test_session.add(user)
    await test_session.commit()
    await test_session.refresh(user)
    
    assert user.id is not None
    assert user.username == "testuser"
    assert user.role == "admin"
    assert user.is_active is True


@pytest.mark.asyncio
async def test_conversation_and_message_models(test_session):
    """Test Conversation and Message models with relationship"""
    conv_id = str(uuid.uuid4())
    conversation = Conversation(
        id=conv_id,
        title="Test Conversation",
        status="normal",
        customer_id="visitor_test123"
    )
    
    test_session.add(conversation)
    await test_session.commit()
    
    message = Message(
        conversation_id=conv_id,
        role="user",
        content="Hello, this is a test message",
        confidence=0.95
    )
    
    test_session.add(message)
    await test_session.commit()
    await test_session.refresh(message)
    
    assert message.id is not None
    assert message.conversation_id == conv_id
    assert message.role == "user"


@pytest.mark.asyncio
async def test_knowledge_item_model(test_session):
    """Test KnowledgeItem model"""
    item = KnowledgeItem(
        id="KB001",
        question="What is the return policy?",
        answer="You can return items within 30 days.",
        keywords=["return", "policy", "refund"],
        category="售后服务",
        score=1.0
    )
    
    test_session.add(item)
    await test_session.commit()
    await test_session.refresh(item)
    
    assert item.id == "KB001"
    assert item.category == "售后服务"
    assert "return" in item.keywords


@pytest.mark.asyncio
async def test_product_model(test_session):
    """Test Product model"""
    product = Product(
        id="PROD001",
        name="Test Product",
        price=99.99,
        category="电子产品",
        description="A test product description",
        specifications={"color": "black", "size": "medium"},
        stock=10,
        keywords=["test", "product"]
    )
    
    test_session.add(product)
    await test_session.commit()
    await test_session.refresh(product)
    
    assert product.id == "PROD001"
    assert product.price == 99.99
    assert product.is_out_of_stock is False
    
    # Test out of stock property
    product.stock = 0
    assert product.is_out_of_stock is True


@pytest.mark.asyncio
async def test_system_settings_model(test_session):
    """Test SystemSettings model"""
    setting = SystemSettings(
        key="llm_provider",
        value="openai"
    )
    
    test_session.add(setting)
    await test_session.commit()
    await test_session.refresh(setting)
    
    assert setting.key == "llm_provider"
    assert setting.value == "openai"
