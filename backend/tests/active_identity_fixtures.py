"""Current HTTP identity fixture; historical ORM rows are data compatibility only."""
from types import SimpleNamespace
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.database import Base, create_database_engine, get_db
from app.models.user import User
from app.models.conversation import Conversation
from app.services.auth_service import AuthService
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.agri_history import router as history_router


@pytest_asyncio.fixture
async def identity_env(tmp_path):
    engine = create_database_engine('sqlite+aiosqlite:///' + (tmp_path / 'identity-lifecycle.db').as_posix())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            users = {name: User(username=name, password_hash='unused', role=role, is_active=True)
                     for name, role in [('admin', 'admin'), ('customer', 'customer'), ('staff', 'cs'), ('other_staff', 'cs')]}
            db.add_all(users.values()); await db.flush()
            ids = {name: user.id for name, user in users.items()}
            subjects = {name: user.auth_subject for name, user in users.items()}
            db.add(Conversation(id='handled-own', customer_id=f"user_{ids['customer']}",
                                title='Historical case', status='human_handling', human_agent_id=ids['staff']))
            await db.commit()
        app = FastAPI()
        for router in (auth_router, users_router, history_router): app.include_router(router)
        async def database():
            async with sessions() as db: yield db
        app.dependency_overrides[get_db] = database
        yield SimpleNamespace(app=app, sessions=sessions, ids=ids, subjects=subjects)
    finally:
        await engine.dispose()


def access(env, name):
    return AuthService.create_tokens(env.ids[name], name, env.subjects[name])[0]
