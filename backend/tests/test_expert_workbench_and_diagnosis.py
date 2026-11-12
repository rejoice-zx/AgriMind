# -*- coding: utf-8 -*-
"""
第四阶段核心测试 - AgriMind 农技专家工作台与多模态图像诊断端到端闭环测试
验证内容:
1. 多模态图像初诊 (Visual Diagnosis):
   - 典型病害高置信度识别与国家标准规程自动推荐
   - 非典型/疑难病害低置信度 (<0.70) 拦截并自动派发专家工单
2. 专家工作台状态流转 (Ticket Lifecycle):
   - pending_review (待接单) -> in_review (审阅中) -> expert_reviewed (已确诊)
   - 专家查看工单全时空微环境上下文（田块、土壤、微气象、农事履历）
3. Human-in-the-Loop 知识生产闭环 (Knowledge Feedback Loop):
   - 专家出具确诊处方后自动沉淀为 ExpertCase
   - 自动反哺沉淀为知识库 L4 级专家案例条目
4. REST API 与权限边界隔离:
   - POST /api/diagnosis/upload 图片上传
   - POST /api/diagnosis/visual-diagnose
   - GET /api/expert/tickets & GET /api/expert/tickets/{id}
   - POST /api/expert/tickets/{id}/claim & POST /api/expert/tickets/{id}/resolve
   - 角色权限防御（普通用户禁止越权操作专家工作台）
"""

import io
import zlib
import struct
from datetime import date, timedelta
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.crop import CropPlanting
from app.models.disease_record import DiseaseRecord
from app.models.expert_case import ExpertCase
from app.models.farming_record import FarmingRecord
from app.models.farm import Farm
from app.models.field import Field
from app.models.knowledge import KnowledgeItem
from app.models.weather import WeatherSnapshot
from app.models.user import User
from app.schemas.expert import (
    ExpertResolveRequest,
    VisualDiagnosisRequest,
)
from app.services.auth_service import AuthService
from app.services.expert_service import ExpertWorkbenchService
from app.services.seed_agriculture_knowledge import seed_tomato_knowledge
from app.api.expert_workbench import router as expert_router


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def expert_user(test_session: AsyncSession) -> User:
    """创建测试农技专家"""
    return await AuthService.create_user(
        test_session,
        username="expert_li_test",
        password="password123",
        display_name="李建国研究员",
        role="expert",
        is_active=True,
    )


@pytest_asyncio.fixture
async def regular_farmer(test_session: AsyncSession) -> User:
    """创建普通农户（用于权限边界隔离验证）"""
    return await AuthService.create_user(
        test_session,
        username="farmer_wang_test",
        password="password123",
        display_name="老王农户",
        role="user",
        is_active=True,
    )


@pytest_asyncio.fixture
async def expert_client(test_session: AsyncSession):
    """构建专家工作台 API 测试客户端"""
    app = FastAPI()
    app.include_router(expert_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_vision_service(monkeypatch):
    """Hermetic mock for vision diagnosis in expert workbench tests"""
    from app.services.vision_service import VisionService

    async def default_mock_diagnose(image_path, crop_name="番茄", symptoms_hint=None, **kwargs):
        if "early_blight" in str(image_path):
            return {
                "image_url": str(image_path),
                "crop": crop_name,
                "suspected_disease": "番茄早疫病",
                "confidence": 0.92,
                "features_detected": ["同心轮纹", "深褐色坏死斑"],
                "severity": "初发期",
                "need_expert": False,
                "description": "检测到典型番茄早疫病同心轮纹病斑。",
                "source_model": "Qwen3-VL-Mock",
            }
        return {
            "image_url": str(image_path),
            "crop": crop_name,
            "suspected_disease": "疑似疑难病斑",
            "confidence": 0.45,
            "features_detected": ["边缘模糊"],
            "severity": "未知",
            "need_expert": True,
            "description": "病斑模糊未识别到典型真菌病害特征，建议转接专家。",
            "source_model": "Qwen3-VL-Mock",
        }

    monkeypatch.setattr(VisionService, "diagnose_leaf_image", default_mock_diagnose)


@pytest_asyncio.fixture
async def workbench_env(test_session: AsyncSession, regular_farmer: User):
    """初始化测试农场、田块与开花坐果期番茄种植档案"""
    await seed_tomato_knowledge(test_session, sync_vector=False)

    farm = Farm(
        name="寿光蔬菜高新技术示范园",
        owner_id=regular_farmer.id,
        province="山东省",
        city="潍坊市",
        district="寿光市",
        area=80.0,
    )
    test_session.add(farm)
    await test_session.flush()

    field = Field(
        farm_id=farm.id,
        name="2号智能温室大棚",
        area=6.0,
        soil_type="砂质壤土",
        irrigation_type="水肥一体化膜下滴灌",
        latitude=36.89,
        longitude=118.75,
    )
    test_session.add(field)
    await test_session.flush()

    today = date.today()
    crop = CropPlanting(
        field_id=field.id,
        crop="番茄",
        variety="普罗旺斯水果番茄",
        planting_date=today - timedelta(days=50),
        expected_harvest_date=today + timedelta(days=55),
        growth_stage="开花坐果期",
        area=6.0,
        status="active",
    )
    test_session.add(crop)

    # 农事记录（5天前施肥）
    farming = FarmingRecord(
        field_id=field.id,
        crop_planting_id=crop.id,
        record_type="施肥",
        record_date=today - timedelta(days=5),
        input_name="高钾水溶肥15-5-30",
        input_amount=6.0,
        unit="公斤/亩",
        description="坐果期第1次随滴灌冲施",
    )
    test_session.add(farming)

    # 微气象快照（高温高湿）
    weather = WeatherSnapshot(
        field_id=field.id,
        temperature=27.5,
        humidity=85.0,
        rainfall=0.0,
        wind_speed=1.5,
        weather="多云",
    )
    test_session.add(weather)
    await test_session.commit()

    return {
        "farm": farm,
        "field": field,
        "crop": crop,
    }


class TestMultimodalDiagnosisFlow:
    """测试多模态图像初诊与工单自动分流机制"""

    @pytest.mark.asyncio
    async def test_high_confidence_visual_diagnosis(self, test_session: AsyncSession, workbench_env, regular_farmer, monkeypatch):
        """测试典型病斑（早疫病）高置信度识别与国家标准推荐"""
        from app.services.vision_service import VisionService
        async def mock_diagnose(*args, **kwargs):
            return {
                "image_url": "http://storage.farm/early_blight_concentric_leaf.jpg",
                "crop": "番茄",
                "suspected_disease": "番茄早疫病",
                "confidence": 0.92,
                "features_detected": ["同心轮纹", "深褐色坏死斑"],
                "severity": "初发期",
                "need_expert": False,
                "description": "检测到典型番茄早疫病同心轮纹病斑。",
                "source_model": "Qwen3-VL-Mock",
            }
        monkeypatch.setattr(VisionService, "diagnose_leaf_image", mock_diagnose)

        field_id = workbench_env["field"].id
        req = VisualDiagnosisRequest(
            image_url="http://storage.farm/early_blight_concentric_leaf.jpg",
            field_id=field_id,
            crop="番茄",
            symptoms_hint="叶片有清晰同心轮纹黑色坏死斑",
            auto_create_ticket=True,
        )

        res = await ExpertWorkbenchService.visual_diagnose(test_session, req, current_user=regular_farmer)

        assert res.need_expert is False
        assert res.confidence >= 0.85
        assert "早疫病" in res.suspected_disease
        assert res.disease_record_id is not None
        # 高置信度初诊，状态应为 preliminary
        rec = await test_session.get(DiseaseRecord, res.disease_record_id)
        assert rec is not None
        assert rec.status == "preliminary"


    @pytest.mark.asyncio
    async def test_low_confidence_auto_create_expert_ticket(self, test_session: AsyncSession, workbench_env, regular_farmer):
        """测试疑难/模糊照片低置信度 (<0.70) 拦截并自动派发专家复核工单"""
        field_id = workbench_env["field"].id
        req = VisualDiagnosisRequest(
            image_url="http://storage.farm/unclear_blurry_leaf_spot.jpg",
            field_id=field_id,
            crop="番茄",
            symptoms_hint="病斑边缘模糊褪绿，无法确认是否缺素",
            auto_create_ticket=True,
        )

        res = await ExpertWorkbenchService.visual_diagnose(test_session, req, current_user=regular_farmer)

        assert res.need_expert is True
        assert res.confidence < 0.70
        assert res.ticket_id is not None
        assert "专家工作台复核工单" in res.advice

        # 检查数据库中 DiseaseRecord 状态应为 pending_review
        rec = await test_session.get(DiseaseRecord, res.disease_record_id)
        assert rec is not None
        assert rec.status == "pending_review"


class TestExpertWorkbenchLifecycleAndFeedback:
    """测试专家工作台全生命周期与 Human-in-the-Loop 知识反哺闭环"""

    @pytest.mark.asyncio
    async def test_ticket_lifecycle_claim_and_resolve(
        self,
        test_session: AsyncSession,
        workbench_env,
        expert_user: User,
    ):
        """
        全生命周期测试：
        1. 产生待复核工单 (pending_review)
        2. 专家获取详情（含微气象与农事履历）
        3. 专家认领工单 (in_review)
        4. 专家提交确诊处方 (expert_reviewed)
        5. 自动沉淀 ExpertCase
        6. 自动反哺沉淀至知识库 KnowledgeItem (L4 级真实专家案例)
        """
        field_id = workbench_env["field"].id

        # 1. 模拟生成待办工单
        diag_req = VisualDiagnosisRequest(
            image_url="http://storage.farm/complex_leaf_disease.jpg",
            field_id=field_id,
            crop="番茄",
            symptoms_hint="疑似复合感染",
        )
        diag_res = await ExpertWorkbenchService.visual_diagnose(test_session, diag_req, current_user=expert_user)
        ticket_id = diag_res.disease_record_id

        # 2. 查询待办列表
        tickets, total = await ExpertWorkbenchService.list_tickets(test_session, status="pending_review")
        assert total >= 1
        assert any(t.ticket_id == ticket_id for t in tickets)

        # 3. 专家查看详情，验证时空微气象与农事履历聚合
        detail = await ExpertWorkbenchService.get_ticket_detail(test_session, ticket_id)
        assert detail is not None
        assert detail.field_name == "2号智能温室大棚"
        assert detail.current_weather is not None
        assert detail.current_weather["humidity"] == 85.0  # 高湿微环境
        assert len(detail.recent_farming_records) >= 1

        # 4. 专家认领工单
        claimed = await ExpertWorkbenchService.claim_ticket(test_session, ticket_id, expert_user)
        assert claimed.status == "in_review"
        assert claimed.expert_id == expert_user.id
        assert claimed.expert_name == "李建国研究员"

        # 5. 专家出具确诊方案并完成知识反哺
        resolve_in = ExpertResolveRequest(
            confirmed_disease="番茄灰霉病与生理性缺钙复合症",
            treatment_suggestion="【专家处方】：1. 选用50%腐霉利1500倍液+糖醇钙叶面喷施；2. 加强大棚夜间通风排湿，控制相对湿度至75%以下；3. 安全间隔期7天。",
            notes="温室内湿度高达85%，残花未及时清理导致灰霉病菌侵染，伴随高温高湿幼果缺钙脐腐。",
            is_knowledge_candidate=1,
        )

        resolve_res = await ExpertWorkbenchService.resolve_ticket(
            test_session,
            ticket_id=ticket_id,
            expert_user=expert_user,
            resolve_in=resolve_in,
        )

        assert resolve_res.status == "expert_reviewed"
        assert resolve_res.confirmed_disease == "番茄灰霉病与生理性缺钙复合症"
        assert resolve_res.expert_case_id is not None
        assert resolve_res.knowledge_item_id is not None  # 知识反哺条目 ID

        # 6. 验证数据库中 ExpertCase 实体
        case = await test_session.get(ExpertCase, resolve_res.expert_case_id)
        assert case is not None
        assert case.confirmed_disease == "番茄灰霉病与生理性缺钙复合症"
        assert case.expert_id == expert_user.id

        # 7. 验证知识库 KnowledgeItem 中沉淀的 L4 专家案例
        kb_item = await test_session.get(KnowledgeItem, str(resolve_res.knowledge_item_id))
        assert kb_item is not None
        assert kb_item.authority_level == "L4"
        assert kb_item.source_type == "unverified"  # pending admin review before retrieval
        assert "李建国研究员" in kb_item.source_name
        assert "番茄灰霉病与生理性缺钙复合症" in kb_item.answer


def make_valid_png(width: int = 200, height: int = 200) -> bytes:
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data))
    raw = b''.join(b'\x00' + b'\x50\x90\x50' * width for _ in range(height))
    comp = zlib.compress(raw)
    idat_chunk = struct.pack('>I', len(comp)) + b'IDAT' + comp + struct.pack('>I', zlib.crc32(b'IDAT' + comp))
    iend_chunk = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND'))
    return sig + ihdr_chunk + idat_chunk + iend_chunk


class TestExpertWorkbenchAPIEndpoints:
    """测试专家工作台 REST API 端点集成与权限校验"""

    @pytest.mark.asyncio
    async def test_image_upload_endpoint(self, expert_client: AsyncClient, expert_user: User):
        """测试 POST /api/diagnosis/upload 图片上传接口 (合法 200x200 PNG)"""
        token = AuthService.create_access_token(data={"sub": str(expert_user.id), "role": expert_user.role, "account_subject": expert_user.auth_subject})
        headers = _auth_header(token)

        png_bytes = make_valid_png(200, 200)
        files = {"file": ("tomato_blight_leaf.png", io.BytesIO(png_bytes), "image/png")}

        resp = await expert_client.post("/api/diagnosis/upload", files=files, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "url" in data
        assert "filename" in data
        assert data["content_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_anonymous_upload_rejected(self, expert_client: AsyncClient):
        """测试匿名用户未登录上传图片被拦截 (401 Unauthorized)"""
        png_bytes = make_valid_png(200, 200)
        files = {"file": ("leaf.png", io.BytesIO(png_bytes), "image/png")}
        resp = await expert_client.post("/api/diagnosis/upload", files=files)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_fake_extension_upload_rejected(self, expert_client: AsyncClient, expert_user: User):
        """测试伪造扩展名纯文本文件被拦截 (400 Bad Request)"""
        token = AuthService.create_access_token(data={"sub": str(expert_user.id), "role": expert_user.role, "account_subject": expert_user.auth_subject})
        headers = _auth_header(token)

        files = {"file": ("fake.jpg", io.BytesIO(b"this is plain text not image header"), "image/jpeg")}
        resp = await expert_client.post("/api/diagnosis/upload", files=files, headers=headers)
        assert resp.status_code == 400
        assert "不是有效的图片格式" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_low_resolution_upload_rejected(self, expert_client: AsyncClient, expert_user: User):
        """测试低于 200x200 像素微型图片被拦截 (400 Bad Request)"""
        token = AuthService.create_access_token(data={"sub": str(expert_user.id), "role": expert_user.role, "account_subject": expert_user.auth_subject})
        headers = _auth_header(token)

        tiny_png = make_valid_png(50, 50)
        files = {"file": ("tiny.png", io.BytesIO(tiny_png), "image/png")}
        resp = await expert_client.post("/api/diagnosis/upload", files=files, headers=headers)
        assert resp.status_code == 400
        assert "分辨率过低" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_visual_diagnose_api(self, expert_client: AsyncClient, workbench_env, expert_user: User, monkeypatch):
        """测试 POST /api/diagnosis/visual-diagnose 端点"""
        from app.services.vision_service import VisionService
        async def mock_diagnose(*args, **kwargs):
            return {
                "image_url": upload.json()["url"],
                "crop": "番茄",
                "suspected_disease": "番茄灰霉病",
                "confidence": 0.90,
                "features_detected": ["灰褐色致密霉层"],
                "severity": "初发期",
                "need_expert": False,
                "description": "多模态识别到典型灰霉病特征。",
                "source_model": "Qwen3-VL-Mock",
            }
        monkeypatch.setattr(VisionService, "diagnose_leaf_image", mock_diagnose)

        token = AuthService.create_access_token(data={"sub": str(expert_user.id), "role": expert_user.role, "account_subject": expert_user.auth_subject})
        headers = _auth_header(token)

        upload = await expert_client.post("/api/diagnosis/upload", files={"file": ("leaf.png", make_valid_png(300, 300), "image/png")}, headers=headers)
        assert upload.status_code == 200
        payload = {
            "image_url": upload.json()["url"],
            "field_id": workbench_env["field"].id,
            "crop": "番茄",
            "symptoms_hint": "叶片密生灰褐色霉层",
            "auto_create_ticket": True,
        }
        resp = await expert_client.post("/api/diagnosis/visual-diagnose", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "灰霉病" in data["suspected_disease"]
        assert data["confidence"] >= 0.85
        assert data["need_expert"] is False


    @pytest.mark.asyncio
    async def test_expert_ticket_apis_flow(
        self,
        expert_client: AsyncClient,
        workbench_env,
        expert_user: User,
    ):
        """测试专家工作台工单列表、认领与确诊 API 全流程"""
        # 生成专家登录 Token
        token = AuthService.create_access_token(data={"sub": str(expert_user.id), "role": expert_user.role, "account_subject": expert_user.auth_subject})
        headers = _auth_header(token)

        # 1. 触发一个低置信度工单
        upload = await expert_client.post("/api/diagnosis/upload", files={"file": ("leaf.png", make_valid_png(300, 300), "image/png")}, headers=headers)
        assert upload.status_code == 200
        diag_payload = {
            "image_url": upload.json()["url"],
            "field_id": workbench_env["field"].id,
            "crop": "番茄",
        }
        diag_resp = await expert_client.post("/api/diagnosis/visual-diagnose", json=diag_payload, headers=headers)
        ticket_id = diag_resp.json()["disease_record_id"]

        # 2. GET /api/expert/tickets
        list_resp = await expert_client.get("/api/expert/tickets?status=pending_review", headers=headers)
        assert list_resp.status_code == 200
        tickets = list_resp.json()["items"]
        assert any(t["ticket_id"] == ticket_id for t in tickets)

        # 3. GET /api/expert/tickets/{id}
        detail_resp = await expert_client.get(f"/api/expert/tickets/{ticket_id}", headers=headers)
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["ticket_id"] == ticket_id
        assert detail["field_name"] == "2号智能温室大棚"

        # 4. POST /api/expert/tickets/{id}/claim
        claim_resp = await expert_client.post(f"/api/expert/tickets/{ticket_id}/claim", headers=headers)
        assert claim_resp.status_code == 200
        assert claim_resp.json()["status"] == "in_review"

        # 5. POST /api/expert/tickets/{id}/resolve
        resolve_payload = {
            "confirmed_disease": "番茄晚疫病 (急性扩散期)",
            "treatment_suggestion": "【专家紧急处置】：立即采用68.75%氟吡菌胺·霜霉威水剂800倍液全株细喷雾，连用2次，间隔5天；严禁大棚喷灌降温，排湿隔离。",
            "notes": "典型的水浸状边缘不清暗绿色大斑，叶背有稀疏白霉，属于高危侵染，需严密防控。",
            "is_knowledge_candidate": 1,
        }
        resolve_resp = await expert_client.post(
            f"/api/expert/tickets/{ticket_id}/resolve",
            json=resolve_payload,
            headers=headers,
        )
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "expert_reviewed"
        assert data["confirmed_disease"] == "番茄晚疫病 (急性扩散期)"
        assert data["knowledge_item_id"] is not None

    @pytest.mark.asyncio
    async def test_permission_defense_for_regular_user(
        self,
        expert_client: AsyncClient,
        regular_farmer: User,
    ):
        """测试权限隔离：普通农户禁止访问专家工作台端点，返回 403 Forbidden"""
        farmer_token = AuthService.create_access_token(data={"sub": str(regular_farmer.id), "role": regular_farmer.role, "account_subject": regular_farmer.auth_subject})
        headers = _auth_header(farmer_token)


        resp = await expert_client.get("/api/expert/tickets", headers=headers)
        assert resp.status_code == 403
        assert "需要农技专家" in resp.json()["detail"]
