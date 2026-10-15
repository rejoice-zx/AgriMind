# -*- coding: utf-8 -*-
"""
农业智能系统第一阶段核心测试 - Agriculture Domain Models & API Tests
验证内容:
- 农场(Farm)、田块(Field)、种植批次(CropPlanting)模型与业务
- 农事记录(FarmingRecord)、病害记录(DiseaseRecord)、气象快照(WeatherSnapshot)
- 专家复核案例(ExpertCase)
- 租户/权限边界与级联关系
- REST API 端点集成测试 (HTTP 状态码、输入校验与响应结构)
"""

import pytest
import pytest_asyncio
from datetime import date, timedelta
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils.time import utcnow
from app.models.user import User
from app.models.farm import Farm
from app.models.field import Field
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord
from app.models.disease_record import DiseaseRecord
from app.models.weather import WeatherSnapshot
from app.models.expert_case import ExpertCase
from app.schemas.agriculture import (
    FarmCreate, FarmUpdate,
    FieldCreate, FieldUpdate,
    CropPlantingCreate, CropPlantingUpdate,
    FarmingRecordCreate,
    DiseaseRecordCreate,
    WeatherSnapshotCreate,
    ExpertCaseCreate
)
from app.services.farm_service import FarmService
from app.services.farming_record_service import FarmingRecordService
from app.services.auth_service import AuthService
from app.api.farms import router as farms_router
from app.api.farming_records import router as farming_records_router


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def farmer_user(test_session: AsyncSession) -> User:
    """创建测试农户"""
    return await AuthService.create_user(
        test_session,
        username="farmer_zhang",
        password="farmer_password",
        display_name="张三农户",
        role="customer",
        is_active=True
    )


@pytest_asyncio.fixture
async def another_farmer(test_session: AsyncSession) -> User:
    """创建另一个测试农户（用于越权隔离测试）"""
    return await AuthService.create_user(
        test_session,
        username="farmer_li",
        password="farmer_password",
        display_name="李四农户",
        role="customer",
        is_active=True
    )


@pytest_asyncio.fixture
async def expert_user(test_session: AsyncSession) -> User:
    """创建测试农技专家"""
    return await AuthService.create_user(
        test_session,
        username="agri_expert_wang",
        password="expert_password",
        display_name="王牌农技专家",
        role="expert",
        is_active=True
    )



@pytest_asyncio.fixture
async def agri_client(test_session: AsyncSession):
    """构建农业 API 测试客户端"""
    app = FastAPI()
    app.include_router(farms_router)
    app.include_router(farming_records_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


class TestFarmManagement:
    """农场与田块管理测试"""

    @pytest.mark.asyncio
    async def test_create_and_get_farm(self, test_session: AsyncSession, farmer_user: User):
        """测试创建农场与查询农场"""
        farm_in = FarmCreate(
            name="阳光番茄种植基地",
            province="江苏省",
            city="南京市",
            district="江宁区",
            address="横溪街道现代农业示范园",
            latitude=31.85,
            longitude=118.82,
            area=120.5,
            description="优质温室大棚番茄生产基地"
        )
        farm = await FarmService.create_farm(test_session, farm_in, owner_id=farmer_user.id)

        assert farm.id is not None
        assert farm.name == "阳光番茄种植基地"
        assert farm.owner_id == farmer_user.id
        assert farm.area == 120.5

        # 详情查询
        fetched = await FarmService.get_farm_by_id(test_session, farm.id)
        assert fetched is not None
        assert fetched.city == "南京市"

    @pytest.mark.asyncio
    async def test_farm_permission_isolation(
        self,
        test_session: AsyncSession,
        farmer_user: User,
        another_farmer: User
    ):
        """测试不同农户之间的权限隔离与越权防御"""
        farm_in = FarmCreate(name="张三的有机农场", area=50.0)
        farm = await FarmService.create_farm(test_session, farm_in, owner_id=farmer_user.id)

        # 李四尝试修改张三的农场，应当被拒绝
        with pytest.raises(HTTPException) as exc_info:
            await FarmService.update_farm(
                test_session,
                farm_id=farm.id,
                farm_in=FarmUpdate(name="被篡改的农场名"),
                user_id=another_farmer.id,
                is_admin=False
            )
        assert exc_info.value.status_code == 403

        # 张三自己修改，应当成功
        updated = await FarmService.update_farm(
            test_session,
            farm_id=farm.id,
            farm_in=FarmUpdate(name="张三现代智慧农场"),
            user_id=farmer_user.id,
            is_admin=False
        )
        assert updated.name == "张三现代智慧农场"

    @pytest.mark.asyncio
    async def test_create_field_under_farm(self, test_session: AsyncSession, farmer_user: User):
        """测试在农场下创建地块"""
        farm_in = FarmCreate(name="绿源科技农场", area=80.0)
        farm = await FarmService.create_farm(test_session, farm_in, owner_id=farmer_user.id)

        # 创建1号田
        field_in = FieldCreate(
            name="1号温室大棚",
            area=30.0,
            soil_type="沙壤土",
            irrigation_type="滴灌",
            remark="主要种植早春番茄"
        )
        field = await FarmService.create_field(
            test_session,
            farm_id=farm.id,
            field_in=field_in,
            user_id=farmer_user.id
        )

        assert field.id is not None
        assert field.farm_id == farm.id
        assert field.soil_type == "沙壤土"

        # 列表查询地块
        fields = await FarmService.list_fields_by_farm(test_session, farm_id=farm.id, user_id=farmer_user.id)
        assert len(fields) == 1
        assert fields[0].name == "1号温室大棚"


class TestCropAndFarmingLifecycle:
    """作物种植周期与农事记录测试"""

    @pytest.mark.asyncio
    async def test_crop_planting_lifecycle(self, test_session: AsyncSession, farmer_user: User):
        """测试作物定植、生长期推进（苗期->开花期->坐果期）"""
        farm = await FarmService.create_farm(test_session, FarmCreate(name="测试农场"), owner_id=farmer_user.id)
        field = await FarmService.create_field(test_session, farm.id, FieldCreate(name="2号试验田", area=20.0), user_id=farmer_user.id)

        # 添加番茄定植
        crop_in = CropPlantingCreate(
            crop="番茄",
            variety="普罗旺斯",
            planting_date=date(2026, 8, 20),
            expected_harvest_date=date(2026, 11, 30),
            growth_stage="苗期",
            area=20.0
        )
        planting = await FarmService.create_crop_planting(test_session, field.id, crop_in, user_id=farmer_user.id)
        assert planting.id is not None
        assert planting.crop == "番茄"
        assert planting.growth_stage == "苗期"

        # 推进生长期为开花期
        updated = await FarmService.update_crop_planting(
            test_session,
            crop_id=planting.id,
            crop_in=CropPlantingUpdate(growth_stage="开花期"),
            user_id=farmer_user.id
        )
        assert updated.growth_stage == "开花期"

    @pytest.mark.asyncio
    async def test_farming_records(self, test_session: AsyncSession, farmer_user: User):
        """测试记录施肥、灌溉农事操作"""
        farm = await FarmService.create_farm(test_session, FarmCreate(name="施肥试验农场"), owner_id=farmer_user.id)
        field = await FarmService.create_field(test_session, farm.id, FieldCreate(name="露天菜田", area=15.0), user_id=farmer_user.id)

        # 添加施肥记录
        rec_in = FarmingRecordCreate(
            record_type="施肥",
            record_date=date(2026, 9, 25),
            operator="张师傅",
            description="滴灌追施水溶肥",
            input_name="高钾水溶肥20-10-30",
            input_amount=10.0,
            unit="kg/亩"
        )
        record = await FarmingRecordService.create_farming_record(
            test_session,
            field_id=field.id,
            record_in=rec_in,
            user_id=farmer_user.id
        )
        assert record.id is not None
        assert record.record_type == "施肥"
        assert record.input_amount == 10.0

        # 查询农事记录列表
        records, total = await FarmingRecordService.list_farming_records(
            test_session,
            field_id=field.id,
            record_type="施肥"
        )
        assert total == 1
        assert records[0].input_name == "高钾水溶肥20-10-30"


class TestDiseaseAndExpertWorkflow:
    """病虫害初诊与专家复核工作流测试"""

    @pytest.mark.asyncio
    async def test_disease_diagnosis_and_expert_review(
        self,
        test_session: AsyncSession,
        farmer_user: User,
        expert_user: User
    ):
        """测试人工巡查 -> 申请复核 -> 认领 -> 专家归档"""
        farm = await FarmService.create_farm(test_session, FarmCreate(name="病害示范点"), owner_id=farmer_user.id)
        field = await FarmService.create_field(test_session, farm.id, FieldCreate(name="番茄大棚A"), user_id=farmer_user.id)

        # 1. 农户/AI初诊发现叶片异常（疑似叶霉病，置信度0.75）
        disease_in = DiseaseRecordCreate(
            disease_name="番茄叶霉病",
            symptoms="叶片正面出现边缘不清晰的淡黄色褪绿斑，叶背对应处长出绒状霉层",
            severity="medium",
            diagnosis_method="manual",
            image_url="https://storage.agrimind.local/samples/tomato_leaf_mold_01.jpg",
            status="preliminary"
        )
        record = await FarmingRecordService.create_disease_record(
            test_session,
            field_id=field.id,
            disease_in=disease_in,
            user_id=farmer_user.id
        )
        assert record.status == "preliminary"
        assert record.diagnosis_confidence is None
        from app.services.disease_review_service import DiseaseReviewService
        await DiseaseReviewService.request_review(test_session, record.id)
        await DiseaseReviewService.claim(test_session, record.id, expert_user)

        # 2. 农技专家工作台复核确诊，并给出综合治理方案
        case_in = ExpertCaseCreate(
            disease_record_id=record.id,
            confirmed_disease="番茄叶霉病（Fulvia fulva）",
            treatment_suggestion="立即加强棚内通风降湿，控制相对湿度在80%以下；发病初期推荐选用40%氟硅唑乳油4000倍液或25%嘧菌酯悬浮剂1500倍液喷雾防治，交替用药。",
            notes="环境高湿为诱发主因，注意不要在阴雨天喷雾",
            is_knowledge_candidate=1
        )
        expert_case = await FarmingRecordService.create_expert_case(
            test_session,
            case_in=case_in,
            expert_id=expert_user.id
        )

        assert expert_case.id is not None
        assert expert_case.confirmed_disease == "番茄叶霉病（Fulvia fulva）"
        assert expert_case.expert_id == expert_user.id

        # 3. 验证原病害记录状态同步流转为 expert_reviewed
        updated_disease = await FarmingRecordService.get_disease_record_by_id(test_session, record.id)
        assert updated_disease.status == "expert_reviewed"
        assert "立即加强棚内通风降湿" in updated_disease.expert_result


class TestWeatherSnapshots:
    """微气象与时序环境数据测试"""

    @pytest.mark.asyncio
    async def test_weather_history(self, test_session: AsyncSession, farmer_user: User):
        """测试记录田块气象快照与获取最新数据"""
        farm = await FarmService.create_farm(test_session, FarmCreate(name="气象监测农场"), owner_id=farmer_user.id)
        field = await FarmService.create_field(test_session, farm.id, FieldCreate(name="气象站1号田"), user_id=farmer_user.id)

        # 连续记录两次气象快照
        t1 = utcnow() - timedelta(hours=1)
        t2 = utcnow()

        await FarmingRecordService.record_weather_snapshot(
            test_session,
            field_id=field.id,
            weather_in=WeatherSnapshotCreate(
                timestamp=t1,
                temperature=22.5,
                humidity=85.0,
                rainfall=0.0,
                weather="阴"
            )
        )
        await FarmingRecordService.record_weather_snapshot(
            test_session,
            field_id=field.id,
            weather_in=WeatherSnapshotCreate(
                timestamp=t2,
                temperature=24.0,
                humidity=92.0,
                rainfall=15.0,
                weather="中雨"
            )
        )

        # 获取最新一条气象快照
        latest = await FarmingRecordService.get_latest_weather(test_session, field.id)
        assert latest is not None
        assert latest.temperature == 24.0
        assert latest.humidity == 92.0
        assert latest.rainfall == 15.0

        # 获取时序列表
        history = await FarmingRecordService.list_weather_history(test_session, field.id, limit=10)
        assert len(history) == 2


class TestAgricultureAPIEndpoints:
    """农业模块 HTTP REST API 集成测试"""

    @pytest.mark.asyncio
    async def test_farm_and_field_api_flow(
        self,
        agri_client: AsyncClient,
        test_session: AsyncSession,
        farmer_user: User
    ):
        """测试通过 HTTP API 创建农场、创建地块、添加作物种植完整链路"""
        token = AuthService.create_access_token({"sub": str(farmer_user.id), "username": farmer_user.username, "account_subject": farmer_user.auth_subject})
        headers = _auth_header(token)

        # 1. POST /api/farms 创建农场
        res_farm = await agri_client.post(
            "/api/farms",
            headers=headers,
            json={
                "name": "API测试农场",
                "province": "江苏省",
                "city": "南京市",
                "area": 60.0
            }
        )
        assert res_farm.status_code == 201, f"Failed with {res_farm.status_code}: {res_farm.text}"
        farm_data = res_farm.json()
        farm_id = farm_data["id"]
        assert farm_data["name"] == "API测试农场"

        # 2. GET /api/farms 获取农场列表
        res_list = await agri_client.get("/api/farms", headers=headers)
        assert res_list.status_code == 200
        assert res_list.json()["total"] >= 1

        # 3. POST /api/farms/{farm_id}/fields 创建地块
        res_field = await agri_client.post(
            f"/api/farms/{farm_id}/fields",
            headers=headers,
            json={
                "name": "1号试验大棚",
                "area": 25.0,
                "soil_type": "壤土",
                "irrigation_type": "滴灌"
            }
        )
        assert res_field.status_code == 201
        field_data = res_field.json()
        field_id = field_data["id"]

        # 4. POST /api/fields/{field_id}/crops 添加番茄种植
        res_crop = await agri_client.post(
            f"/api/fields/{field_id}/crops",
            headers=headers,
            json={
                "crop": "番茄",
                "variety": "粉娘",
                "planting_date": "2026-09-01",
                "growth_stage": "开花期",
                "area": 25.0
            }
        )
        assert res_crop.status_code == 201
        assert res_crop.json()["crop"] == "番茄"

        # 5. POST /api/fields/{field_id}/farming-records 记录施肥
        res_farming = await agri_client.post(
            f"/api/fields/{field_id}/farming-records",
            headers=headers,
            json={
                "record_type": "施肥",
                "record_date": "2026-09-20",
                "input_name": "硫酸钾",
                "input_amount": 15.0,
                "unit": "kg/亩"
            }
        )
        assert res_farming.status_code == 201
        assert res_farming.json()["record_type"] == "施肥"

        # 6. POST /api/fields/{field_id}/disease-records 记录病害
        res_disease = await agri_client.post(
            f"/api/fields/{field_id}/disease-records",
            headers=headers,
            json={
                "disease_name": "番茄早疫病",
                "symptoms": "下部叶片出现轮纹状黑褐斑"
            }
        )
        assert res_disease.status_code == 201
        assert res_disease.json()["disease_name"] == "番茄早疫病"
