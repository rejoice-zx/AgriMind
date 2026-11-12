# -*- coding: utf-8 -*-
"""
阶段三核心测试 - 农业规程数据契约、法定禁限用农药全矩阵与安全覆写测试
覆盖内容:
1. 19 种国家法定禁限用高毒农药全目录扫描与负面清单检测
2. 8 种国家登记农药安全间隔期 (PHI) 匹配与参数校验
3. 农业知识库元数据 (crop, growth_stage, authority_level, disease, url) CRUD 与多维度筛选契约
4. 农资商品规程规格 (ICAMA 登记证号、有效成分、剂型、毒性等级) 契约
5. 农业 Agent 气象环境与农药违规综合安全仲裁矩阵
6. 农事记录安全间隔期追溯与作业类型归一化
"""

import pytest
import pytest_asyncio
from datetime import date, timedelta
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.pesticide_standards import (
    BANNED_PESTICIDES_CATALOG,
    check_banned_pesticide,
    find_all_banned_pesticides,
)
from app.models.knowledge import KnowledgeItem
from app.models.product import Product
from app.models.farming_record import FarmingRecord, is_spraying_record, is_fertilizing_record
from app.models.field import Field
from app.models.farm import Farm
from app.models.crop import CropPlanting
from app.models.user import User
from app.schemas.agent import AgentDecisionRequest
from app.schemas.knowledge import CreateKnowledgeRequest, UpdateKnowledgeRequest
from app.services.agent_service import AgriAgentService, AgriTools
from app.services.knowledge_service import knowledge_service
from app.services.weather_service import WeatherService
from app.api.knowledge import router as knowledge_router
from app.api.dependencies import get_current_user, AdminRequired


# ============================================================================
# 1. 19 种法定禁限用高毒农药全目录测试
# ============================================================================

class TestBannedPesticidesCatalog:
    """19 种法定禁限用高毒农药全目录扫描与负面清单检测"""

    @pytest.mark.parametrize("chemical", BANNED_PESTICIDES_CATALOG)
    def test_all_19_banned_pesticides_detected_individually(self, chemical: str):
        """验证 19 种禁用农药目录中的每一种均能被精准检出"""
        query = f"我的作物得病了，请问可以使用{chemical}进行喷雾防治吗？"
        detected = check_banned_pesticide(query)
        assert detected == chemical
        
        all_found = find_all_banned_pesticides(query)
        assert chemical in all_found

    def test_multiple_banned_pesticides_in_single_text(self):
        """测试单段文本中包含多种禁用农药时全量检出"""
        text = "农户私自复配了克百威和氧乐果，还询问能否加甲胺磷"
        found = find_all_banned_pesticides(text)
        assert len(found) == 3
        assert "克百威" in found
        assert "氧乐果" in found
        assert "甲胺磷" in found

    @pytest.mark.parametrize("safe_chemical", ["代森锰锌", "百菌清", "嘧菌酯", "春雷霉素", "杀虫双"])
    def test_compliant_chemicals_not_flagged(self, safe_chemical: str):
        """非当前目录命中不构成产品登记或适用性批准。"""
        query = f"番茄开花坐果期可以使用{safe_chemical}防治病虫害吗"
        assert check_banned_pesticide(query) is None
        assert find_all_banned_pesticides(query) == []

    def test_acephate_mixed_with_real_methamidophos_detected(self):
        """两种分别命中，长名称不能被子串白名单掩盖。"""
        query = "农户私自复配了乙酰甲胺磷和甲胺磷，准备喷施大棚"
        found = find_all_banned_pesticides(query)
        assert "甲胺磷" in found
        assert set(found) == {"乙酰甲胺磷", "甲胺磷"}

    def test_empty_and_whitespace_inputs_safe(self):
        """验证空字符串或空白字符输入安全无异常"""
        assert check_banned_pesticide("") is None
        assert check_banned_pesticide("   ") is None
        assert find_all_banned_pesticides("") == []


# ============================================================================
# 3. 农业知识库元数据 CRUD 与多维度筛选契约
# ============================================================================

class TestKnowledgeAgriculturalMetadataContracts:
    """知识库农业元数据 (crop, growth_stage, authority_level, disease, url) 契约测试"""

    @pytest.mark.asyncio
    async def test_create_and_retrieve_agricultural_knowledge(self, test_session: AsyncSession):
        """验证创建包含完整农业规程字段的知识条目并持久化获取"""
        req = CreateKnowledgeRequest(
            question="番茄开花坐果期灰霉病防治技术规程",
            answer="发病初期使用50%腐霉利可湿性粉剂1000倍液喷雾，安全间隔期7天。",
            keywords=["番茄", "灰霉病", "腐霉利"],
            category="病害防治",
            crop="番茄",
            growth_stage="开花坐果期",
            topic="disease",
            disease="灰霉病",
            authority_level="L1",
            source_name="GB/T 23416.2-2009",
            publish_date="2020-05-01",
            url="https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=12345"
        )

        item = await knowledge_service.create_knowledge(
            db=test_session,
            **req.model_dump(exclude_unset=True)
        )
        assert item.id is not None
        assert item.crop == "番茄"
        assert item.growth_stage == "开花坐果期"
        assert item.authority_level == "L1"
        assert item.disease == "灰霉病"
        assert item.source_name == "GB/T 23416.2-2009"
        assert item.publish_date == "2020-05-01"
        assert item.url.startswith("https://")

    @pytest.mark.asyncio
    async def test_update_agricultural_knowledge_metadata(self, test_session: AsyncSession):
        """验证修改知识条目的农业元数据字段生效"""
        item = KnowledgeItem(
            id="k_test_agri_001",
            question="番茄早疫病初期识别",
            answer="叶片初生暗褐色至黑色同心轮纹小斑点。",
            keywords=["番茄", "早疫病"],
            category="病害识别",
            crop="番茄",
            growth_stage="苗期",
            authority_level="L2",
        )
        test_session.add(item)
        await test_session.flush()

        update_req = UpdateKnowledgeRequest(
            growth_stage="开花坐果期",
            authority_level="L1",
            disease="早疫病",
            source_name="NY/T 蔬菜标准规程"
        )
        updated = await knowledge_service.update_knowledge(
            db=test_session,
            knowledge_id="k_test_agri_001",
            **update_req.model_dump(exclude_unset=True)
        )
        assert updated is not None
        assert updated.growth_stage == "开花坐果期"
        assert updated.authority_level == "L1"
        assert updated.disease == "早疫病"
        assert updated.source_name == "NY/T 蔬菜标准规程"

    @pytest.mark.asyncio
    async def test_filter_knowledge_by_crop(self, test_session: AsyncSession):
        """验证按作物精准筛选知识库列表"""
        item1 = KnowledgeItem(id="k_t1", question="番茄整枝打杈", answer="保留主蔓", category="栽培", crop="番茄")
        item2 = KnowledgeItem(id="k_t2", question="黄瓜落蔓技术", answer="落蔓高度40cm", category="栽培", crop="黄瓜")
        test_session.add_all([item1, item2])
        await test_session.flush()

        tomato_items, total = await knowledge_service.get_knowledge_list(test_session, crop="番茄")
        assert any(i.id == "k_t1" for i in tomato_items)
        assert not any(i.id == "k_t2" for i in tomato_items)

    @pytest.mark.asyncio
    async def test_filter_knowledge_by_growth_stage(self, test_session: AsyncSession):
        """验证按生长阶段筛选知识条目"""
        item1 = KnowledgeItem(id="k_s1", question="苗期控温", answer="白天25℃", category="环境", crop="番茄", growth_stage="苗期")
        item2 = KnowledgeItem(id="k_s2", question="采收期保鲜", answer="采摘前停肥", category="采收", crop="番茄", growth_stage="采收期")
        test_session.add_all([item1, item2])
        await test_session.flush()

        seedling_items, _ = await knowledge_service.get_knowledge_list(test_session, growth_stage="苗期")
        assert any(i.id == "k_s1" for i in seedling_items)
        assert not any(i.id == "k_s2" for i in seedling_items)

    @pytest.mark.asyncio
    async def test_filter_knowledge_by_topic(self, test_session: AsyncSession):
        """验证按主题分类(病害/肥料/植保)筛选知识条目"""
        item1 = KnowledgeItem(id="k_tp1", question="番茄晚疫病防治", answer="推荐精甲霜灵", category="植保", topic="disease")
        item2 = KnowledgeItem(id="k_tp2", question="番茄坐果期水肥配比", answer="高钾复合肥", category="农事", topic="fertilizer")
        test_session.add_all([item1, item2])
        await test_session.flush()

        disease_items, _ = await knowledge_service.get_knowledge_list(test_session, topic="disease")
        assert any(i.id == "k_tp1" for i in disease_items)
        assert not any(i.id == "k_tp2" for i in disease_items)


# ============================================================================
# 4. 农资商品规程规格 (specifications) 契约测试
# ============================================================================

class TestLegacyProductJSONCompatibility:
    """农资商品规格参数 (ICAMA登记号、有效成分、安全间隔期、剂型) 数据契约"""

    @pytest.mark.asyncio
    async def test_create_pesticide_product_with_agri_specifications(self, test_session: AsyncSession):
        """测试创建包含 ICAMA 登记证号与 PHI 规格参数的农资商品"""
        p = Product(
            id="input_pest_001",
            name="68% 精甲霜·锰锌 水分散粒剂 (100g/袋)",
            price=28.5,
            category="杀菌剂",
            description="针对番茄晚疫病与早疫病的双重内吸与保护杀菌剂",
            specifications={
                "农药登记证号": "PD20180234",
                "有效成分及含量": "精甲霜灵 4% + 代森锰锌 64%",
                "剂型": "水分散粒剂",
                "安全间隔期(PHI)": "7天",
                "毒性等级": "低毒",
                "适用作物": "番茄",
                "防治对象": "晚疫病、早疫病"
            },
            stock=200,
            keywords=["杀菌剂", "番茄晚疫病", "精甲霜锰锌"]
        )
        test_session.add(p)
        await test_session.flush()

        saved = await test_session.get(Product, "input_pest_001", populate_existing=True)
        assert saved is not None
        assert saved.specifications["农药登记证号"] == "PD20180234"
        assert saved.specifications["安全间隔期(PHI)"] == "7天"
        assert saved.specifications["剂型"] == "水分散粒剂"

    @pytest.mark.asyncio
    async def test_update_product_specifications(self, test_session: AsyncSession):
        """测试更新农资规格参数"""
        p = Product(
            id="input_fert_001",
            name="大量元素水溶肥料 20-20-20+TE",
            price=120.0,
            category="水肥",
            specifications={"肥料登记证": "农肥(2021)准字1234号", "配比": "平衡型 20-20-20"},
            stock=50
        )
        test_session.add(p)
        await test_session.flush()

        new_specs = {
            "肥料登记证": "农肥(2021)准字1234号",
            "配比": "平衡型 20-20-20",
            "微量元素": "螯合态铁锌硼 >= 0.5%",
            "推荐亩用量": "5~10kg/亩/次"
        }
        p.specifications = new_specs
        await test_session.commit()
        await test_session.refresh(p)
        updated = p
        assert updated is not None
        assert updated.specifications["推荐亩用量"] == "5~10kg/亩/次"


# ============================================================================
# 5. 农业 Agent 气象环境与农药违规综合安全仲裁矩阵
# ============================================================================

class TestAgriAgentSafetyMatrix:
    """Agent 气象环境与农药违规综合安全仲裁矩阵"""

    @pytest_asyncio.fixture
    async def mock_field_env(self, test_session: AsyncSession):
        user = User(id=10, username="test_farmer_matrix", password_hash="hash", role="customer", is_active=True)
        farm = Farm(id=10, name="矩阵示范棚", owner_id=10, province="山东", city="潍坊", district="寿光", area=10.0)
        field = Field(id=10, farm_id=10, name="10号露地测试田", facility_type="open_field", area=2.0, latitude=36.88, longitude=118.73)
        crop = CropPlanting(field_id=10, crop="番茄", variety="普罗旺斯", growth_stage="开花坐果期", planting_date=date.today() - timedelta(days=40), status="active")
        test_session.add_all([user, farm, field, crop])
        await test_session.flush()
        return field

    @pytest.mark.asyncio
    async def test_agent_strong_wind_interception(self, test_session: AsyncSession, mock_field_env, monkeypatch):
        """场景: 预报大风 6.5m/s (超过4.0m/s阈值) -> 严禁喷药防飘移"""
        async def mock_windy_weather(*args, **kwargs):
            return {
                "current": {"temperature": 20.0, "humidity": 50.0, "rainfall": 0.0, "wind_speed": 6.5, "weather": "大风", "source": "Mock"},
                "forecasts": [{"date": "2026-05-15", "temp_max": 22.0, "temp_min": 15.0, "rainfall_mm": 0.0, "rain_prob": 0, "wind_speed": 6.5, "weather": "大风", "humidity": 50}],
                "spray_safety": {"has_rain_soon": False, "high_temp_risk": False, "strong_wind_risk": True, "spray_risks": ["大风飘移严重 (6.5m/s)"], "is_suitable_to_spray": False}
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_windy_weather)

        req = AgentDecisionRequest(query="下午可以打药防灰霉病吗？", field_id=mock_field_env.id)
        decision = await AgriAgentService.decide(test_session, req, current_user=await test_session.get(User, 10))
        assert decision.can_proceed is False
        assert any("飘移" in r or "大风" in r for r in decision.risks)

    @pytest.mark.asyncio
    async def test_agent_high_temperature_interception(self, test_session: AsyncSession, mock_field_env, monkeypatch):
        """场景: 预报气温 35.0℃ (超过32.0℃阈值) -> 严禁高温喷药防药害蒸发"""
        async def mock_hot_weather(*args, **kwargs):
            return {
                "current": {"temperature": 35.0, "humidity": 40.0, "rainfall": 0.0, "wind_speed": 1.5, "weather": "晴热", "source": "Mock"},
                "forecasts": [{"date": "2026-05-15", "temp_max": 35.5, "temp_min": 24.0, "rainfall_mm": 0.0, "rain_prob": 0, "wind_speed": 1.5, "weather": "晴热", "humidity": 40}],
                "spray_safety": {"has_rain_soon": False, "high_temp_risk": True, "strong_wind_risk": False, "spray_risks": ["高温药害风险 (35.0℃)"], "is_suitable_to_spray": False}
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_hot_weather)

        req = AgentDecisionRequest(query="中午气温高，现在喷农药可以吗？", field_id=mock_field_env.id)
        decision = await AgriAgentService.decide(test_session, req, current_user=await test_session.get(User, 10))
        assert decision.can_proceed is False
        assert any("高温" in r or "药害" in r for r in decision.risks)

    @pytest.mark.asyncio
    async def test_agent_banned_pesticide_overrides_good_weather(self, test_session: AsyncSession, mock_field_env, monkeypatch):
        """场景: 即使天气非常适宜 (22℃、无风、无雨)，若用户咨询使用禁用农药(神农丹/涕灭威)，必须坚决硬阻断"""
        async def mock_ideal_weather(*args, **kwargs):
            return {
                "current": {"temperature": 22.0, "humidity": 60.0, "rainfall": 0.0, "wind_speed": 1.5, "weather": "晴好", "source": "Mock"},
                "forecasts": [{"date": "2026-05-15", "temp_max": 24.0, "temp_min": 16.0, "rainfall_mm": 0.0, "rain_prob": 0, "wind_speed": 1.5, "weather": "晴好", "humidity": 60}],
                "spray_safety": {"has_rain_soon": False, "high_temp_risk": False, "strong_wind_risk": False, "spray_risks": [], "is_suitable_to_spray": True}
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_ideal_weather)

        req = AgentDecisionRequest(query="天气很好，我打算用神农丹(涕灭威)撒施防治线虫，可以吗？", field_id=mock_field_env.id)
        decision = await AgriAgentService.decide(test_session, req, current_user=await test_session.get(User, 10))
        assert decision.can_proceed is False
        assert decision.need_expert is True
        assert any("高危禁用农药拦截" in r for r in decision.risks)
        assert any("神农丹" in r or "涕灭威" in r for r in decision.risks)

    @pytest.mark.asyncio
    async def test_agent_rain_forecast_interception(self, test_session: AsyncSession, mock_field_env, monkeypatch):
        """场景: 预报近期有降雨 (has_rain_soon=True) -> 严禁喷药防雨水冲刷失效"""
        async def mock_rainy_weather(*args, **kwargs):
            return {
                "current": {"temperature": 18.0, "humidity": 85.0, "rainfall": 2.0, "wind_speed": 2.0, "weather": "小雨", "source": "Mock"},
                "forecasts": [{"date": "2026-05-15", "temp_max": 20.0, "temp_min": 14.0, "rainfall_mm": 5.0, "rain_prob": 90, "wind_speed": 2.0, "weather": "中雨", "humidity": 85}],
                "spray_safety": {"has_rain_soon": True, "high_temp_risk": False, "strong_wind_risk": False, "spray_risks": ["近期有降雨风险，药液易流失"], "is_suitable_to_spray": False}
            }
        monkeypatch.setattr(WeatherService, "get_real_weather", mock_rainy_weather)

        req = AgentDecisionRequest(query="现在有点小雨，可以给番茄喷药防晚疫病吗？", field_id=mock_field_env.id)
        decision = await AgriAgentService.decide(test_session, req, current_user=await test_session.get(User, 10))
        assert decision.can_proceed is False
        assert any("降雨" in r or "雨" in r for r in decision.risks)


# ============================================================================
# 6. 农事记录安全间隔期追溯与作业类型归一化
# ============================================================================

class TestFarmingRecordSafetyCalculations:
    """农事记录追溯、类型识别与安全间隔期警示"""

    @pytest_asyncio.fixture
    async def history_farm(self, test_session: AsyncSession, test_user):
        farm = Farm(name="History test farm", owner_id=test_user.id)
        test_session.add(farm)
        await test_session.flush()
        return farm

    def test_record_type_normalization_spraying(self):
        """测试多端施药类型归一化识别"""
        assert is_spraying_record("施药") is True
        assert is_spraying_record("spraying") is True
        assert is_spraying_record("spray") is True
        assert is_spraying_record("pest_control") is True
        assert is_spraying_record("打药") is True
        assert is_spraying_record("灌溉") is False

    def test_record_type_normalization_fertilizing(self):
        """测试多端施肥类型归一化识别"""
        assert is_fertilizing_record("施肥") is True
        assert is_fertilizing_record("fertilizing") is True
        assert is_fertilizing_record("fertilize") is True
        assert is_fertilizing_record("水肥管理") is True
        assert is_fertilizing_record("追肥") is True
        assert is_fertilizing_record("冲施水肥") is True
        assert is_fertilizing_record("修剪整枝") is False

    @pytest.mark.asyncio
    async def test_get_farming_history_short_interval_warning(self, test_session: AsyncSession, history_farm):
        """测试距上次用药仅2天触发高频用药药害与抗药性告警"""
        field = Field(id=20, farm_id=history_farm.id, name="20号测试棚")
        test_session.add(field)
        await test_session.flush()

        rec = FarmingRecord(
            field_id=20,
            record_type="施药",
            record_date=date.today() - timedelta(days=2),
            input_name="80% 代森锰锌 可湿性粉剂",
            input_amount=150.0,
            unit="g/亩",
            operator="张农户"
        )
        test_session.add(rec)
        await test_session.flush()

        history = await AgriTools.get_farming_history(test_session, field_id=20)
        assert history["total_records_found"] == 1
        assert history["last_spray"] is not None
        assert history["last_spray"]["days_ago"] == 2
        assert len(history["safety_warnings"]) >= 1
        assert any("仅过去 2 天" in w for w in history["safety_warnings"])

    def test_record_type_normalization_irrigation_and_field_mgmt(self):
        """测试灌溉与田间管理农事类型归一化识别"""
        from app.models.farming_record import IRRIGATION_TYPES, FIELD_MANAGEMENT_TYPES
        assert "灌溉" in IRRIGATION_TYPES
        assert "irrigation" in IRRIGATION_TYPES
        assert "整枝" in FIELD_MANAGEMENT_TYPES
        assert "field_management" in FIELD_MANAGEMENT_TYPES

    @pytest.mark.asyncio
    async def test_get_farming_history_safe_interval_no_warning(self, test_session: AsyncSession, history_farm):
        """测试距上次代森锰锌用药已过20天(超过15天PHI) -> 安全合规无近期高频警告"""
        field = Field(id=21, farm_id=history_farm.id, name="21号合规大棚")
        test_session.add(field)
        await test_session.flush()

        rec = FarmingRecord(
            field_id=21,
            record_type="施药",
            record_date=date.today() - timedelta(days=20),
            input_name="80% 代森锰锌 可湿性粉剂",
            input_amount=150.0,
            unit="g/亩",
            safety_interval_days=15,
            operator="李农户"
        )
        test_session.add(rec)
        await test_session.flush()

        history = await AgriTools.get_farming_history(test_session, field_id=21)
        assert history["total_records_found"] == 1
        assert history["last_spray"]["days_ago"] == 20
        assert len(history["safety_warnings"]) == 0
