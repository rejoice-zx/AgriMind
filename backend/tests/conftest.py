"""Pytest Configuration and Fixtures"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import event

from app.database import Base
from app.models import User, Conversation, Message, KnowledgeItem, Product, SystemSettings
from app.services.auth_service import AuthService


@pytest_asyncio.fixture
async def test_engine():
    """Create test database engine (in-memory SQLite)"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session(test_engine):
    """Create test database session"""
    async_session = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(test_session: AsyncSession):
    """Create a test user for authentication tests"""
    user = await AuthService.create_user(
        test_session,
        username="test_agent",
        password="test_password",
        role="cs",
        display_name="Test Agent"
    )
    return user


@pytest_asyncio.fixture
async def test_admin_user(test_session: AsyncSession):
    """Create a test admin user"""
    user = await AuthService.create_user(
        test_session,
        username="test_admin",
        password="admin_password",
        role="admin",
        display_name="Test Admin"
    )
    return user


@pytest.fixture(autouse=True)
def isolate_upload_data_dir(tmp_path, monkeypatch):
    """测试环境隔离上传目录，避免测试文件写进真实的 data/uploads 生产环境"""
    from app.config import settings
    temp_data_dir = str(tmp_path / "data")
    monkeypatch.setattr(settings, "DATA_DIR", temp_data_dir)
    return temp_data_dir


@pytest.fixture
def controlled_dns(monkeypatch):
    """Explicit SSRF DNS answers; never resolve test domains on the network."""
    import ipaddress
    import socket

    def configure(answers):
        queries = []

        def resolve(host, port, *args, **kwargs):
            queries.append(host)
            if host in answers:
                addresses = answers[host]
            else:
                try:
                    addresses = [str(ipaddress.ip_address(host))]
                except ValueError:
                    raise socket.gaierror(socket.EAI_NONAME, 'Unconfigured test hostname') from None
            if isinstance(addresses, Exception):
                raise addresses
            records = []
            for address in addresses:
                ip = ipaddress.ip_address(address)
                family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
                sockaddr = (str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port)
                records.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr))
            return records

        monkeypatch.setattr(socket, 'getaddrinfo', resolve)
        return queries

    return configure
