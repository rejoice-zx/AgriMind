# -*- coding: utf-8 -*-
"""
第三阶段核心测试 - AgriMind 农业智能决策 Agent 与 6 大核心工具链测试
验证内容:
1. 农业 6 大专业核心工具的功能与边界:
   - Tool 1: get_field_info (田块属性与活跃作物画像)
   - Tool 2: get_weather (微气象与未来7天气象风险预警)
   - Tool 3: get_farming_history (农事操作追溯与安全间隔期警示)
   - Tool 4: search_agri_knowledge (Hybrid RAG 国家标准融合检索)
   - Tool 5: diagnose_crop_image (多模态视觉初诊与典型病斑识别)
   - Tool 6: calculate_gdd (有效积温模型与作物物候预测)
2. 工具注册中心 (AgriToolRegistry):
   - 标准 Function Calling 规范 Schema 导出
   - 工具动态分发与安全执行机制
3. 农业自主规划与风险裁决 Agent (AgriAgentService):
   - 典型场景 1: 番茄灰霉病打药遇雨拦截（降雨硬阻断、推迟建议、安全替代）
   - 典型场景 2: 开花坐果期水肥科学决策（控氮增钾配方）
   - 典型场景 3: 图像视觉初诊高置信度推荐与疑难低置信度流转专家工单
   - 典型场景 4: 有效积温物候预测
4. REST API 接口测试:
   - GET /api/agent/tools
   - POST /api/agent/tools/execute
   - POST /api/agent/decide
"""

from datetime import date, timedelta
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.crop import CropPlanting
from app.models.farming_record import FarmingRecord
from app.models.farm import Farm
from app.models.field import Field
from app.models.weather import WeatherSnapshot
from app.models.user import User
from app.schemas.agent import AgentDecisionRequest
from app.services.agent_service import AgriAgentService, AgriToolRegistry, AgriTools
from app.services.auth_service import AuthService
from app.services.seed_agriculture_knowledge import seed_tomato_knowledge
from app.api.dependencies import get_current_user
from app.api.agri_agent import router as agri_agent_router, require_expert_or_admin


@pytest_asyncio.fixture
async def agent_client(test_session: AsyncSession):
    """构建农业 Agent API 测试客户端（带认证）"""
    app = FastAPI()
    app.include_router(agri_agent_router)

    async def override_get_db():
        yield test_session

    # API archives bind to a real account lifetime; a synthetic ID must not authenticate another row.
    auth_user = User(username="test_expert", password_hash="fictional", role="expert", is_active=True)
    test_session.add(auth_user)
    await test_session.commit()
    await test_session.refresh(auth_user)

    async def override_current_user():
        return auth_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[require_expert_or_admin] = override_current_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_external_services(monkeypatch):
    """Hermetic mock for weather and LLM calls in agent tests"""
    from app.services.weather_service import WeatherService
    from app.services.agent_service import AgriAgentService

    async def mock_weather(*args, **kwargs):
        return {
            "current": {
                "temperature": 22.0, "humidity": 85.0, "rainfall": 1.0,
                "wind_speed": 3.0, "weather": "小雨", "source": "MockWeather"
            },
            "forecasts": [
                {
                    "date": "2026-05-15", "weather": "中雨",
                    "temp_max": 24.0, "temp_min": 17.0,
                    "rainfall_mm": 12.0, "rain_prob": 80,
                    "wind_speed": 3.5, "humidity": 90.0
                }
            ] + [
                {
                    "date": f"2026-05-{16+i}", "weather": "多云",
                    "temp_max": 25.0, "temp_min": 16.0,
                    "rainfall_mm": 0.0, "rain_prob": 10,
                    "wind_speed": 2.0, "humidity": 60.0
                } for i in range(6)
            ],
            "spray_safety": {
                "is_offline_warning": False,
                "has_rain_soon": True,
                "high_temp_risk": False,
                "strong_wind_risk": False,
                "spray_risks": ["明日降雨冲刷 (80% 概率，中雨 12.0mm)"],
                "is_suitable_to_spray": False
            }
        }

    async def mock_llm_synth(*args, **kwargs):
        query = kwargs.get("query", "")
        diagnosis_info = kwargs.get("diagnosis_info")

        if diagnosis_info and diagnosis_info.get("need_expert"):
            return {
                "can_proceed": True,
                "conclusion": "【疑似疑难病害/转交专家】病斑特征模糊，系统已自动转交专家工作台复核。",
                "confidence": 0.45,
                "risks": ["病害种类不明确，存在用药偏差风险"],
                "recommendations": ["保持田间通风，等待专家确诊处方"],
                "need_expert": True,
                "expert_reason": "病斑模糊未识别到典型真菌病害特征，置信度不足需人工专家复核",
                "regulations_cited": ["GB/T 蔬菜主要病虫害防治规程"],
                "reasoning": "视觉初诊置信度仅为0.45，低于安全阈值。"
            }

        if "施肥" in query or "水肥" in query or "冲施" in query:
            return {
                "can_proceed": True,
                "conclusion": "开花坐果期水肥方案：控氮增钾，滴灌平衡水肥。",
                "confidence": 0.92,
                "risks": [],
                "recommendations": ["改用高钾型滴灌肥", "小水勤浇少量多次"],
                "need_expert": False,
                "expert_reason": None,
                "regulations_cited": ["NY/T 5010 蔬菜产地环境条件"],
                "reasoning": "番茄坐果期营养生长转向生殖生长，必须控氮增钾防止旺长落花。"
            }

        return {
            "can_proceed": False,
            "conclusion": "【严禁打药】明日预计有降雨冲刷风险。",
            "confidence": 0.95,
            "risks": ["明日降雨冲刷 (80% 概率，中雨 12.0mm)"],
            "recommendations": ["延后至雨后天晴中上部叶片干燥时再喷雾", "改用大棚闭棚烟熏剂"],
            "need_expert": False,
            "expert_reason": None,
            "regulations_cited": ["GB/T 23416.2 蔬菜病虫害综合防治技术规范 第2部分：茄果类"],
            "reasoning": "气象预报显示降雨概率高达80%，属于植保规程明确禁止施药窗口期。"
        }

    monkeypatch.setattr(WeatherService, "get_real_weather", mock_weather)
    monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", mock_llm_synth)


@pytest_asyncio.fixture
async def farm_env(test_session: AsyncSession):
    """初始化测试农场、地块、活跃番茄种植批次与气象快照"""
    # 1. 种子知识库 (测试环境禁用昂贵的外部向量调用，实现秒级 hermetic 执行)
    await seed_tomato_knowledge(test_session, sync_vector=False)


    # 2. 农场主用户
    farmer = await AuthService.create_user(
        test_session,
        username="farmer_agent_test",
        password="password123",
        display_name="寿光张农户",
        role="user",
        is_active=True,
    )

    # 3. 农场与地块
    farm = Farm(
        name="寿光现代农业示范园",
        owner_id=farmer.id,
        province="山东省",
        city="潍坊市",
        district="寿光市",
        area=120.0,
    )

    test_session.add(farm)
    await test_session.flush()

    field = Field(
        farm_id=farm.id,
        name="1号露地番茄测试田",
        facility_type="open_field",
        area=5.0,
        soil_type="砂质壤土",
        irrigation_type="水肥一体化膜下滴灌",
        latitude=36.88,
        longitude=118.73,
    )
    test_session.add(field)
    await test_session.flush()

    # 4. 作物档案（开花坐果期番茄）
    today = date.today()
    crop = CropPlanting(
        field_id=field.id,
        crop="番茄",
        variety="普罗旺斯水果番茄",
        planting_date=today - timedelta(days=45),
        expected_harvest_date=today + timedelta(days=60),
        growth_stage="开花坐果期",
        area=5.0,
        status="active",
    )
    test_session.add(crop)

    # 5. 农事记录（4天前曾喷洒百菌清）
    record1 = FarmingRecord(
        field_id=field.id,
        crop_planting_id=crop.id,
        record_type="施药",
        record_date=today - timedelta(days=4),
        operator="张师傅",
        input_name="75%百菌清可湿性粉剂",
        input_amount=150.0,
        unit="克/亩",
        description="预防真菌病害常规巡棚喷雾",
    )
    test_session.add(record1)

    # 6. 微气象快照
    weather = WeatherSnapshot(
        field_id=field.id,
        temperature=25.5,
        humidity=72.0,
        rainfall=0.0,
        wind_speed=2.3,
        weather="多云",
    )
    test_session.add(weather)
    await test_session.commit()

    return {
        "farmer": farmer,
        "farm": farm,
        "field": field,
        "crop": crop,
    }


class TestAgriTools:
    """测试 6 大农业专业核心工具"""

    @pytest.mark.asyncio
    async def test_tool_registry_definitions(self):
        """测试工具注册表中 6 大工具的规范元数据"""
        tools = AgriToolRegistry.get_tool_definitions()
        assert len(tools) == 6
        tool_names = [t.name for t in tools]
        assert "get_field_info" in tool_names
        assert "get_weather" in tool_names
        assert "get_farming_history" in tool_names
        assert "search_agri_knowledge" in tool_names
        assert "diagnose_crop_image" in tool_names
        assert "calculate_gdd" in tool_names

        for t in tools:
            assert t.description is not None
            assert "type" in t.parameters

    @pytest.mark.asyncio
    async def test_tool_get_field_info(self, test_session: AsyncSession, farm_env):
        """Tool 1: 测试田块与活跃作物读取"""
        field_id = farm_env["field"].id
        info = await AgriTools.get_field_info(test_session, field_id)

        assert info["found"] is True
        assert info["field_name"] == "1号露地番茄测试田"
        assert info["soil_type"] == "砂质壤土"
        assert info["current_crop"] is not None
        assert info["current_crop"]["crop"] == "番茄"
        assert info["current_crop"]["growth_stage"] == "开花坐果期"
        assert info["current_crop"]["days_planted"] >= 45

    @pytest.mark.asyncio
    async def test_tool_get_weather_risk_detection(self, test_session: AsyncSession, farm_env, monkeypatch):
        """Tool 2: 测试气象风险与降雨阻断识别"""
        from app.services.weather_service import WeatherService
        async def mock_weather(*args, **kwargs):
            return {
                "current": {
                    "temperature": 22.0, "humidity": 85.0, "rainfall": 1.0,
                    "wind_speed": 3.0, "weather": "小雨", "source": "Mock"
                },
                "forecasts": [
                    {
                        "date": "2026-05-15", "weather": "中雨",
                        "temp_max": 24.0, "temp_min": 17.0,
                        "rainfall_mm": 12.0, "rain_prob": 80,
                        "wind_speed": 3.5, "humidity": 90.0
                    }
                ] + [
                    {
                        "date": f"2026-05-{16+i}", "weather": "多云",
                        "temp_max": 25.0, "temp_min": 16.0,
                        "rainfall_mm": 0.0, "rain_prob": 10,
                        "wind_speed": 2.0, "humidity": 60.0
                    } for i in range(6)
                ],
                "spray_safety": {
                    "is_offline_warning": False,
                    "has_rain_soon": True,
                    "high_temp_risk": False,
                    "strong_wind_risk": False,
                    "spray_risks": ["明日降雨冲刷 (80% 概率，中雨 12.0mm)"],
                    "is_suitable_to_spray": False
                }
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_weather)

        field_id = farm_env["field"].id
        weather = await AgriTools.get_weather(test_session, field_id=field_id, days=7)

        assert weather["field_id"] == field_id
        assert len(weather["forecasts"]) == 7
        assert "spray_safety" in weather
        safety = weather["spray_safety"]
        # 明天有中雨（预报模拟），应当检测出降雨淋失风险
        assert safety["has_rain_soon"] is True
        assert safety["is_suitable"] is False
        assert any("降雨" in r or "中雨" in r for r in safety["risks"])

    @pytest.mark.asyncio
    async def test_tool_get_farming_history(self, test_session: AsyncSession, farm_env):
        """Tool 3: 测试农事操作追溯与安全间隔期预警"""
        field_id = farm_env["field"].id
        history = await AgriTools.get_farming_history(test_session, field_id=field_id)

        assert history["total_records_found"] >= 1
        assert history["last_spray"] is not None
        assert history["last_spray"]["input_name"] == "75%百菌清可湿性粉剂"
        # 4天前施药，应触发高频施药安全警告
        assert len(history["safety_warnings"]) > 0
        assert "百菌清" in history["safety_warnings"][0]

    @pytest.mark.asyncio
    async def test_tool_search_agri_knowledge(self, test_session: AsyncSession, farm_env):
        """Tool 4: 配置低门限后检索已标注的农业候选，不代表来源核验"""
        from app.services.settings_service import SettingsService, RAGConfig
        await SettingsService.set_rag_config(test_session, RAGConfig(similarity_threshold=0.2))
        # This isolated ranking fixture has no date restriction. Seed labels such as 至今 are not ISO evidence.
        from sqlalchemy import select
        from app.models.knowledge import KnowledgeItem
        target = await test_session.scalar(select(KnowledgeItem).where(KnowledgeItem.question == "番茄灰霉病的综合防治技术规程与推荐农药配方是什么？"))
        target.effective_from = None
        target.effective_to = None
        target.source_type = "test_fixture"
        target.source_name = "合成检索样本（仅用于排序测试）"
        await test_session.commit()
        res = await AgriTools.search_agri_knowledge(
            test_session,
            query="番茄灰霉病的综合防治技术规程与推荐农药配方是什么？",
            crop="番茄",
            disease="番茄灰霉病",
        )
        assert res["total_recalled"] > 0
        assert res["confidence"] > 0.40
        # 验证合成样本的排序召回，不代表生产来源通过核验。
        top_item = res["items"][0]
        assert top_item["crop"] == "番茄"
        assert "腐霉利" in top_item["answer"] or "灰霉病" in top_item["question"]


    @pytest.mark.asyncio
    async def test_tool_diagnose_crop_image(self, monkeypatch):
        """Tool 5: 测试农业图像视觉初诊"""
        from app.services.vision_service import VisionService
        async def mock_diagnose_leaf(image_path, crop_name="番茄", symptoms_hint=None, **kwargs):
            if "gray_mold" in image_path:
                return {
                    "image_url": image_path,
                    "crop": crop_name,
                    "suspected_disease": "番茄灰霉病",
                    "confidence": 0.91,
                    "features_detected": ["灰褐色致密霉层"],
                    "severity": "初发期",
                    "need_expert": False,
                    "description": "识别到典型番茄灰霉病。",
                    "source_model": "Mock",
                }
            elif "early_blight" in image_path:
                return {
                    "image_url": image_path,
                    "crop": crop_name,
                    "suspected_disease": "番茄早疫病",
                    "confidence": 0.89,
                    "features_detected": ["同心轮纹"],
                    "severity": "初发期",
                    "need_expert": False,
                    "description": "识别到早疫病同心轮纹。",
                    "source_model": "Mock",
                }
            else:
                return {
                    "image_url": image_path,
                    "crop": crop_name,
                    "suspected_disease": "疑似疑难病斑",
                    "confidence": 0.45,
                    "features_detected": ["边缘模糊"],
                    "severity": "未知",
                    "need_expert": True,
                    "description": "病斑模糊建议转接专家。",
                    "source_model": "Mock",
                }
        monkeypatch.setattr(VisionService, "diagnose_leaf_image", mock_diagnose_leaf)

        # 测试典型灰霉病图像
        gray_res = await AgriTools.diagnose_crop_image(
            image_url="http://storage.farm/tomato_gray_mold_leaf.jpg",
            crop="番茄",
        )
        assert "灰霉病" in gray_res["suspected_disease"]
        assert gray_res["confidence"] >= 0.85
        assert gray_res["need_expert"] is False

        # 测试典型早疫病图像（带同心轮纹）
        early_res = await AgriTools.diagnose_crop_image(
            image_url="http://storage.farm/early_blight_sample.png",
            crop="番茄",
            symptoms_hint="叶片有同心轮纹黑色坏死斑",
        )
        assert "早疫病" in early_res["suspected_disease"]
        assert early_res["confidence"] >= 0.85

        # 测试非典型未知图像流转专家
        unknown_res = await AgriTools.diagnose_crop_image(
            image_url="http://storage.farm/blurry_leaf_unknown.jpg",
            crop="番茄",
        )
        assert unknown_res["need_expert"] is True
        assert unknown_res["confidence"] < 0.60


    @pytest.mark.asyncio
    async def test_tool_calculate_gdd(self):
        """Tool 6: 测试有效积温与物候发育预测模型"""
        # 模拟番茄苗期30天的日气温序列
        daily_temps = [
            {"date": f"2026-05-{i:02d}", "t_max": 28.0, "t_min": 18.0}
            for i in range(1, 31)
        ]
        # 每日均温 23℃，T_base=10℃ -> 每日 GDD = 13℃·d -> 30天共 390℃·d
        gdd_res = AgriTools.calculate_gdd(daily_temps=daily_temps, crop="番茄", t_base=10.0)

        assert gdd_res["accumulated_gdd"] == 390.0
        assert gdd_res["predicted_growth_stage"] is None
        assert gdd_res["lifecycle_completion_pct"] is None
        assert gdd_res["calibrated"] is False


class TestAgriAgentServiceDecisions:
    """测试农业 Agent 自主协同决策与风险裁决机制"""

    @pytest.mark.asyncio
    async def test_agent_spray_decision_with_rain_interception(self, test_session: AsyncSession, farm_env, monkeypatch):
        """
        场景 1: 用户询问是否可以在1号棚打药防治灰霉病。
        预期: Agent 自主调度田块、天气、农事历史与 RAG 知识，
             检测到明日降雨，果断下达【严禁打药】阻断决策，并列出降雨冲刷风险与雨后备选建议。
        """
        from app.services.weather_service import WeatherService
        async def mock_rainy_weather(*args, **kwargs):
            return {
                "current": {
                    "temperature": 22.0, "humidity": 85.0, "rainfall": 1.0,
                    "wind_speed": 3.0, "weather": "小雨", "source": "Mock"
                },
                "forecasts": [
                    {
                        "date": "2026-05-15", "weather": "中雨",
                        "temp_max": 24.0, "temp_min": 17.0,
                        "rainfall_mm": 12.0, "rain_prob": 80,
                        "wind_speed": 3.5, "humidity": 90.0
                    }
                ] + [
                    {
                        "date": f"2026-05-{16+i}", "weather": "多云",
                        "temp_max": 25.0, "temp_min": 16.0,
                        "rainfall_mm": 0.0, "rain_prob": 10,
                        "wind_speed": 2.0, "humidity": 60.0
                    } for i in range(6)
                ],
                "spray_safety": {
                    "is_offline_warning": False,
                    "has_rain_soon": True,
                    "high_temp_risk": False,
                    "strong_wind_risk": False,
                    "spray_risks": ["明日降雨冲刷 (80% 概率，中雨 12.0mm)"],
                    "is_suitable_to_spray": False
                }
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_rainy_weather)

        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="明天可以给1号棚番茄喷洒农药防灰霉病吗？",
            field_id=field_id,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.decision_type == "SPRAY_DECISION"
        assert decision.can_proceed is False
        assert "气象风险" in decision.conclusion
        assert "80%" in decision.conclusion
        assert len(decision.risks) >= 1
        assert any("降雨" in r or "中雨" in r for r in decision.risks)
        assert len(decision.recommendations) >= 2
        assert len(decision.tool_traces) >= 4  # 调度了 field, weather, history, knowledge

    @pytest.mark.asyncio
    async def test_agent_fertilization_decision(self, test_session: AsyncSession, farm_env, monkeypatch):
        """
        场景 2: 用户询问1号棚番茄水肥管理方案。
        预期: Agent 识别作物正处于“开花坐果期”，给出控氮增钾、滴灌少量多次的科学方案。
        """
        from app.services.weather_service import WeatherService
        async def mock_mild_weather(*args, **kwargs):
            return {
                "current": {
                    "temperature": 24.0, "humidity": 65.0, "rainfall": 0.0,
                    "wind_speed": 1.5, "weather": "晴", "source": "Mock"
                },
                "forecasts": [
                    {
                        "date": f"2026-05-{15+i}", "weather": "晴",
                        "temp_max": 26.0, "temp_min": 16.0,
                        "rainfall_mm": 0.0, "rain_prob": 5,
                        "wind_speed": 1.5, "humidity": 60.0
                    } for i in range(7)
                ],
                "spray_safety": {
                    "has_rain_soon": False,
                    "high_temp_risk": False,
                    "strong_wind_risk": False,
                    "spray_risks": [],
                    "is_suitable_to_spray": True
                }
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_mild_weather)

        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="1号棚番茄现在该怎么施肥冲施？",
            field_id=field_id,
        )

        # The approval branch requires applicable retrieval evidence as well as a valid model/weather result.
        from app.models.knowledge import KnowledgeItem
        from app.services.settings_service import SettingsService, RAGConfig
        test_session.add(KnowledgeItem(id="fertilizer_applicable_fixture", question="隔离测试适用依据", answer="测试资料，需现场核对",
            crop=farm_env["crop"].crop, growth_stage=farm_env["crop"].growth_stage, disease=None, topic='fertilizer', region='全国', source_type="test_fixture", source_name="隔离测试资料", authority_level="L4"))
        await test_session.commit()
        await SettingsService.set_rag_config(test_session, RAGConfig(similarity_threshold=0.0))
        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.decision_type == "FERTILIZATION"
        assert decision.can_proceed is True
        assert len(decision.recommendations) >= 1

    @pytest.mark.asyncio
    async def test_agent_multimodal_diagnosis_and_expert_escalation(self, test_session: AsyncSession, farm_env):
        """
        场景 3: 疑似疑难病害图片诊断，低置信度流转专家工单。
        """
        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="番茄叶片发黄干枯，帮我看下这是什么病？",
            field_id=field_id,
            image_url="http://storage.farm/unclear_leaf_spot.jpg",
            enable_multimodal=True,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.decision_type == "DIAGNOSIS"
        assert decision.need_expert is True
        assert decision.expert_reason is not None
        assert any(w in decision.conclusion or w in decision.expert_reason for w in ["专家", "诊断", "无法", "图片", "置信度"])

    @pytest.mark.asyncio
    async def test_agent_banned_pesticides_input_hard_interception(self, test_session: AsyncSession, farm_env, monkeypatch):
        """
        场景 4: 用户提问涉及国家明令禁限用高毒农药（如克百威、氧乐果）。
        即便 LLM 发生幻觉判定 can_proceed=True，系统底层安全网必须强制硬阻断 (can_proceed=False) 并流转专家复核。
        """
        async def mock_hallucinating_llm(*args, **kwargs):
            return {
                "can_proceed": True,  # 模拟 LLM 幻觉放行
                "conclusion": "【适宜作业】可以使用该农药进行虫害防治。",
                "confidence": 0.95,
                "risks": [],
                "recommendations": ["按照常规浓度稀释喷洒"],
                "need_expert": False,
                "expert_reason": None,
            }
        monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", mock_hallucinating_llm)

        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="番茄地下害虫严重，我可以用克百威或者氧乐果灌根吗？",
            field_id=field_id,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.can_proceed is False
        assert decision.need_expert is True
        assert any("高危禁用农药拦截" in r for r in decision.risks)
        assert any("克百威" in r for r in decision.risks)
        assert any("氧乐果" in r for r in decision.risks)
        assert any(w in decision.conclusion for w in ["法规硬阻断", "严禁", "禁用农药"])

    @pytest.mark.asyncio
    async def test_agent_banned_pesticides_output_recommendation_filtering(self, test_session: AsyncSession, farm_env, monkeypatch):
        """
        场景 5: 用户提问合规，但 LLM 建议中包含了禁用农药（如氟虫腈）。
        输出安全拦截网必须剔除禁用农药条目，保留安全建议或回退兜底，并注入模型输出违规告警。
        """
        async def mock_leaking_llm(*args, **kwargs):
            return {
                "can_proceed": True,
                "conclusion": "推荐综合防控用药方案。",
                "confidence": 0.88,
                "risks": [],
                "recommendations": [
                    "使用氟虫腈 1500倍液喷雾防治蓟马",
                    "使用百菌清 600倍液防治叶斑病",
                ],
                "need_expert": False,
                "expert_reason": None,
            }
        monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", mock_leaking_llm)

        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="番茄蓟马和叶斑病怎么综合防治？",
            field_id=field_id,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        # 氟虫腈必须被输出过滤网强制剔除
        assert not any("氟虫腈" in rec for rec in decision.recommendations)
        # 百菌清属于合规农药，应当保留
        assert any("百菌清" in rec for rec in decision.recommendations)
        # 风险项中记录模型输出违规拦截告警
        assert any("模型输出违规拦截" in r for r in decision.risks)
        assert decision.can_proceed is False
        assert decision.need_expert is True

    @pytest.mark.asyncio
    async def test_agent_failsafe_on_missing_or_corrupted_llm_fields(self, test_session: AsyncSession, farm_env, monkeypatch):
        """
        场景 6: LLM 返回空字典或损坏字典（缺失 can_proceed / confidence）。
        代码层绝不能 Fail-Open 默认放行 (True / 0.90)，必须 Fail-Safe 保持防御 (can_proceed=False, confidence=0.60, need_expert=True)。
        """
        async def mock_empty_llm(*args, **kwargs):
            return {}  # 完全缺失所有字段
        monkeypatch.setattr(AgriAgentService, "_synthesize_with_llm", mock_empty_llm)

        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="番茄可以打药吗？",
            field_id=field_id,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.can_proceed is False
        assert decision.confidence <= 0.60  # fallback 0.60, then capped by missing knowledge evidence
        assert decision.need_expert is True

    @pytest.mark.asyncio
    async def test_agent_tool_trace_duration_measurement(self, test_session: AsyncSession, farm_env):
        """
        场景 7: 审计工具与端到端物理耗时度量。
        验证 ToolTrace.duration_ms 和 AgentDecisionResponse.total_duration_ms 真实记录非负耗时。
        """
        field_id = farm_env["field"].id
        req = AgentDecisionRequest(
            query="明天可以给1号棚番茄喷洒农药防灰霉病吗？",
            field_id=field_id,
        )

        decision = await AgriAgentService.decide(test_session, req, current_user=farm_env["farmer"])

        assert decision.total_duration_ms > 0.0
        assert len(decision.tool_traces) > 0
        for trace in decision.tool_traces:
            assert trace.duration_ms >= 0.0
            assert trace.status in ("success", "error")


@pytest.mark.asyncio
async def test_user_model_default_role_is_customer(test_session: AsyncSession):
    """验证 User 模型默认权限为 customer（遵循最小特权原则）"""
    assert User.role.default.arg == "customer"
    u = User(username="test_default_customer_user", password_hash="pwd")
    test_session.add(u)
    await test_session.flush()
    assert u.role == "customer"



class TestAgriAgentAPIEndpoints:
    """测试农业 Agent REST API 端点集成"""

    @pytest.mark.asyncio
    async def test_api_get_tools(self, agent_client: AsyncClient):
        """测试 GET /api/agent/tools"""
        resp = await agent_client.get("/api/agent/tools")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) == 6
        names = [t["name"] for t in tools]
        assert "get_field_info" in names
        assert "get_weather" in names

    @pytest.mark.asyncio
    async def test_api_execute_tool(self, agent_client: AsyncClient, farm_env):
        """测试 POST /api/agent/tools/execute"""
        payload = {
            "tool_name": "calculate_gdd",
            "arguments": {
                "t_max": 28.0,
                "t_min": 16.0,
                "t_base": 10.0,
                "crop": "番茄",
            },
        }
        resp = await agent_client.post("/api/agent/tools/execute", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["result"]["accumulated_gdd"] == 12.0

    @pytest.mark.asyncio
    async def test_api_decide_endpoint(self, agent_client: AsyncClient, farm_env):
        """测试 POST /api/agent/decide 接口端到端"""
        payload = {
            "query": "明天可以给1号棚番茄喷洒农药防灰霉病吗？",
            "field_id": farm_env["field"].id,
        }
        resp = await agent_client.post("/api/agent/decide", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision_type"] == "SPRAY_DECISION"
        assert data["conversation_id"] and data["turn_id"]
        assert data["can_proceed"] is False
        assert "气象风险" in data["conclusion"]
        assert "80%" in data["conclusion"]
        assert len(data["tool_traces"]) >= 3

    @pytest.mark.asyncio
    async def test_api_unauthorized_access(self, test_session: AsyncSession):
        """测试未认证请求调用 /api/agent/decide 被安全拦截 (401)"""
        app = FastAPI()
        app.include_router(agri_agent_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/agent/decide", json={"query": "test"})
            assert resp.status_code in (401, 403)
