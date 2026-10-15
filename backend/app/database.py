"""Database Configuration and Connection Management"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.core.model_base import Base
from sqlalchemy.pool import StaticPool
from datetime import date, datetime
import os

from app.config import settings


def create_database_engine(url, *, echo=False):
    """File SQLite needs separate connections; a single in-memory DB needs StaticPool."""
    from sqlalchemy.engine import make_url
    from sqlalchemy import event
    parsed = make_url(url)
    options = {"echo": echo}
    if parsed.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if parsed.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    created = create_async_engine(url, **options)
    if parsed.get_backend_name() == "sqlite":
        @event.listens_for(created.sync_engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    from app.core.operation_observation import install_database_observation
    install_database_observation(created)
    return created


engine = create_database_engine(settings.DATABASE_URL, echo=settings.DEBUG)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db():
    """Dependency to get database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize database - create all tables and default admin user"""
    from app.config import validate_startup_configuration
    validate_startup_configuration()
    # Ensure data directory exists
    data_dir = os.path.dirname(settings.DATABASE_URL.replace("sqlite+aiosqlite:///", ""))
    if data_dir and not os.path.exists(data_dir):
        os.makedirs(data_dir, exist_ok=True)
    
    # Import models to register them with Base
    from app.models import (
        user, conversation, knowledge, product, statistics,
        farm, field, crop, farming_record, disease_record, weather, expert_case
    )
    from app.models.user import User
    from app.models.conversation import Conversation, Message
    from app.models.statistics import StatisticsDaily, StatisticsMeta
    from app.services.auth_service import AuthService
    from sqlalchemy import case, select, func
    
    # The numbered schema runner owns per-step transactions and a resumable journal.
    from app.migrations.schema_upgrade_v6 import run_schema_upgrade
    async with engine.connect() as conn:
        await conn.run_sync(lambda connection: run_schema_upgrade(connection, Base.metadata))

    def _coerce_sql_date(value) -> date:
        if isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        return datetime.strptime(str(value), "%Y-%m-%d").date()

    # Bootstrap statistics snapshot once from existing conversation/message data.
    async with AsyncSessionLocal() as session:
        initialized = (
            await session.execute(select(StatisticsMeta).where(StatisticsMeta.key == "initialized"))
        ).scalar_one_or_none()
        if not initialized:
            existing_daily_rows = int(
                (await session.execute(select(func.count(StatisticsDaily.date)))).scalar() or 0
            )
            if existing_daily_rows == 0:
                daily_map: dict[date, dict[str, int]] = {}

                conv_rows = (
                    await session.execute(
                        select(func.date(Conversation.created_at).label("day"), func.count(Conversation.id))
                        .group_by(func.date(Conversation.created_at))
                    )
                ).all()
                for day_value, conversations_count in conv_rows:
                    day = _coerce_sql_date(day_value)
                    slot = daily_map.setdefault(
                        day,
                        {"conversations": 0, "messages": 0, "user_messages": 0, "assistant_messages": 0},
                    )
                    slot["conversations"] = int(conversations_count or 0)

                msg_rows = (
                    await session.execute(
                        select(
                            func.date(Message.timestamp).label("day"),
                            func.count(Message.id).label("messages"),
                            func.sum(case((Message.role == "user", 1), else_=0)).label("user_messages"),
                            func.sum(case((Message.role == "assistant", 1), else_=0)).label("assistant_messages"),
                        ).group_by(func.date(Message.timestamp))
                    )
                ).all()
                for day_value, messages_count, user_messages_count, assistant_messages_count in msg_rows:
                    day = _coerce_sql_date(day_value)
                    slot = daily_map.setdefault(
                        day,
                        {"conversations": 0, "messages": 0, "user_messages": 0, "assistant_messages": 0},
                    )
                    slot["messages"] = int(messages_count or 0)
                    slot["user_messages"] = int(user_messages_count or 0)
                    slot["assistant_messages"] = int(assistant_messages_count or 0)

                for day, bucket in daily_map.items():
                    session.add(
                        StatisticsDaily(
                            date=day,
                            conversations=bucket["conversations"],
                            messages=bucket["messages"],
                            user_messages=bucket["user_messages"],
                            assistant_messages=bucket["assistant_messages"],
                        )
                    )

            session.add(StatisticsMeta(key="initialized", value="1"))
            await session.commit()
    
    # ── 账号引导（先于任何演示数据副作用）────────────────────────────────
    import logging
    _boot_logger = logging.getLogger(__name__)
    from app.config import is_production_environment

    # 1) 显式安全注入的初始化管理员（生产/受限环境的推荐路径；缺口令明确拒绝，fail-closed）
    _admin_user = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
    _admin_pwd = os.getenv("INITIAL_ADMIN_PASSWORD", "").strip()
    if _admin_user:
        if not _admin_pwd:
            raise RuntimeError(
                "INITIAL_ADMIN_USERNAME 已设置但缺少 INITIAL_ADMIN_PASSWORD，拒绝启动")
        async with AsyncSessionLocal() as _bootstrap_session:
            _existing_admin = (await _bootstrap_session.execute(
                select(User).where(User.username == _admin_user))).scalar_one_or_none()
            if not _existing_admin:
                _bootstrap_session.add(User(
                    username=_admin_user,
                    password_hash=AuthService.hash_password(_admin_pwd),
                    display_name="系统管理员",
                    email=None,
                    role="admin",
                    is_active=True,
                ))
                await _bootstrap_session.commit()
                _boot_logger.info("已按环境变量创建初始化管理员: %s（口令未记录）", _admin_user)
            else:
                _boot_logger.info("初始化管理员 %s 已存在，保持不变（不重置口令）", _admin_user)

    # 2) 演示账号（admin/expert/farmer）播种必须是**显式 opt-in**，且绝不在生产环境发生。
    #    旧逻辑只要"非生产"就播种 admin/admin123，导致按默认 .env(development) 部署即带弱口令管理员。
    _allow_demo = os.getenv("ALLOW_DEMO_SEEDING", "").strip().lower() in ("true", "1")
    if is_production_environment() or not _allow_demo:
        _boot_logger.info(
            "跳过演示账号播种（生产环境=%s，ALLOW_DEMO_SEEDING=%s）",
            is_production_environment(), _allow_demo)
        return

    async with AsyncSessionLocal() as session:
        import logging
        logger = logging.getLogger(__name__)

        # 1. Default Admin
        res_admin = await session.execute(select(User).where(User.username == "admin"))
        admin_user = res_admin.scalar_one_or_none()
        if not admin_user:
            admin_user = User(
                username="admin",
                password_hash=AuthService.hash_password("admin123"),
                display_name="系统管理员",
                email="admin@agrimind.com",
                role="admin",
                is_active=True
            )
            session.add(admin_user)
            await session.commit()
            await session.refresh(admin_user)
            logger.info("已创建默认演示管理员账号: admin（演示口令，勿用于生产）")

        # 2. Default Expert
        res_expert = await session.execute(select(User).where(User.username == "expert"))
        expert_user = res_expert.scalar_one_or_none()
        if not expert_user:
            expert_user = User(
                username="expert",
                password_hash=AuthService.hash_password("expert123"),
                display_name="农技专家-李教授",
                email="expert@agrimind.com",
                role="expert",
                is_active=True
            )
            session.add(expert_user)
            await session.commit()
            logger.info("已创建默认演示农技专家账号: expert（演示口令，勿用于生产）")

        # 3. Default Farmer
        res_farmer = await session.execute(select(User).where(User.username == "farmer"))
        farmer_user = res_farmer.scalar_one_or_none()
        if not farmer_user:
            farmer_user = User(
                username="farmer",
                password_hash=AuthService.hash_password("farmer123"),
                display_name="种植大户-老张",
                email="farmer@agrimind.com",
                role="customer",
                is_active=True
            )
            session.add(farmer_user)
            await session.commit()
            logger.info("已创建默认演示农户账号: farmer（演示口令，勿用于生产）")

        # 4. Default Demo Farm, Fields & Crop Planting
        from app.models.farm import Farm
        from app.models.field import Field
        from app.models.crop import CropPlanting
        from app.models.farming_record import FarmingRecord

        res_farm = await session.execute(select(Farm).limit(1))
        existing_farm = res_farm.scalar_one_or_none()
        if not existing_farm and admin_user:
            demo_farm = Farm(
                name="寿光现代数字果蔬示范农场",
                description="国家级日光温室果蔬标准化数字化种植示范基地",
                province="山东省",
                city="潍坊市",
                district="寿光市",
                address="现代农业高新技术示范园区8号温室群",
                latitude=36.88,
                longitude=118.74,
                area=50.0,
                owner_id=farmer_user.id if farmer_user else admin_user.id
            )
            session.add(demo_farm)
            await session.flush()

            field1 = Field(
                farm_id=demo_farm.id,
                name="1号日光温室大棚",
                area=3.5,
                soil_type="沙壤土",
                irrigation_type="水肥一体化膜下滴灌",
                latitude=36.881,
                longitude=118.741,
                remark="配备自动双向放风机与空气温湿度微气象传感器"
            )
            field2 = Field(
                farm_id=demo_farm.id,
                name="2号智能联动温室",
                area=5.0,
                soil_type="轻壤土",
                irrigation_type="智能高架管道喷灌",
                latitude=36.882,
                longitude=118.742,
                remark="配备遮阳保温幕帘与水肥一体化机"
            )
            session.add_all([field1, field2])
            await session.flush()

            crop1 = CropPlanting(
                field_id=field1.id,
                crop="番茄",
                variety="普罗旺斯水果番茄",
                planting_date=date(2026, 8, 25),
                expected_harvest_date=date(2026, 11, 20),
                growth_stage="开花坐果期",
                area=3.5,
                status="active"
            )
            session.add(crop1)
            await session.flush()

            rec1 = FarmingRecord(
                field_id=field1.id,
                crop_planting_id=crop1.id,
                record_type="施药",
                record_date=date(2026, 9, 28),
                description="全棚喷洒预防早疫病与灰霉病，重点喷施植株中下部叶背",
                input_name="68%精甲霜·锰锌水分散粒剂",
                input_amount=100.0,
                unit="g/亩",
                operator="张农艺师"
            )
            session.add(rec1)
            await session.commit()
            logger.info("已初始化寿光现代农业示范田块与种植档案")

        # 5. Pre-warm Tomato Disease Knowledge Seed
        from app.services.seed_agriculture_knowledge import seed_tomato_knowledge
        try:
            await seed_tomato_knowledge(session)
        except Exception as e:
            logger.warning(f"知识种子预装载跳过或轻微告警: {e}")
