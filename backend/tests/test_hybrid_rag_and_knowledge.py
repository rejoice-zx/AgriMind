# -*- coding: utf-8 -*-
"""
第二阶段核心测试 - 农业知识库与 Hybrid RAG 检索引擎测试
验证内容:
- 农业知识库 Metadata 字段与多维度筛选 (crop, growth_stage, topic, authority_level)
- BM25Okapi 算法准确性与词频归一化打分
- Query Understanding 农业实体抽取与 Query Router 意图分流
- Hybrid RAG (向量检索 + BM25稀疏检索 + 元数据加权 + 权威等级加分)
- 置信度计算与低置信度专家复核判定 (need_expert)
- REST API /api/knowledge/hybrid-search 端点集成测试
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.services.knowledge_service import knowledge_service
from app.services.rag_service import RAGService, BM25Okapi, QueryUnderstanding
from app.services.auth_service import AuthService
from app.api.knowledge import router as knowledge_router


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_user(test_session: AsyncSession) -> User:
    """创建测试管理员用户"""

    return await AuthService.create_user(
        test_session,
        username="admin_rag",
        password="admin_password",
        display_name="知识库主管",
        role="admin",
        is_active=True
    )


@pytest_asyncio.fixture
async def knowledge_client(test_session: AsyncSession):
    """构建知识库 API 测试客户端"""
    app = FastAPI()
    app.include_router(knowledge_router)

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.clear()


class TestAgricultureKnowledgeMetadata:
    """农业知识库元数据与版本管理测试"""

    @pytest.mark.asyncio
    async def test_create_and_filter_agri_knowledge(self, test_session: AsyncSession):
        """测试创建带有作物、生长期、主题元数据的知识条目并执行筛选"""
        item1 = await knowledge_service.create_knowledge(
            db=test_session,
            question="番茄开花期叶霉病如何用药？",
            answer="可选用40%氟硅唑乳油4000倍液或25%嘧菌酯悬浮剂1500倍液喷雾。",
            keywords=["番茄", "叶霉病", "氟硅唑", "开花期"],
            crop="番茄",
            growth_stage="开花期",
            topic="disease",
            disease="番茄叶霉病",
            source_type="standard",
            source_name="隔离测试标注来源（非真实标准）",
            authority_level="L1",
            version="v1.0",
            sync_vector=False
        )
        assert item1.id is not None
        assert item1.crop == "番茄"
        assert item1.authority_level == "L1"

        item2 = await knowledge_service.create_knowledge(
            db=test_session,
            question="水稻分蘖期施肥管理",
            answer="每亩追施尿素5-7公斤，保持浅水层。",
            keywords=["水稻", "施肥", "分蘖期"],
            crop="水稻",
            growth_stage="分蘖期",
            topic="fertilizer",
            authority_level="L2",
            sync_vector=False
        )

        # 1. 按作物筛选
        tomato_items, total = await knowledge_service.get_knowledge_list(
            db=test_session,
            crop="番茄"
        )
        assert total >= 1
        assert any(it.crop == "番茄" for it in tomato_items)

        # 2. 按生长期 + 主题筛选
        stage_items, s_total = await knowledge_service.get_knowledge_list(
            db=test_session,
            crop="番茄",
            growth_stage="开花期",
            topic="disease"
        )
        assert s_total >= 1
        assert stage_items[0].disease == "番茄叶霉病"

        # 3. 前置属性候选集 ID 过滤
        candidate_ids = await knowledge_service.filter_candidate_ids(
            db=test_session,
            crop="番茄",
            growth_stage="开花期"
        )
        assert item1.id in candidate_ids
        assert item2.id not in candidate_ids


class TestBM25Algorithm:
    """BM25Okapi 关键词打分算法测试"""

    def test_bm25_scoring(self):
        """测试 BM25 词频匹配与排序逻辑"""
        corpus = [
            ["番茄", "叶霉病", "黄色", "斑点", "喷雾", "氟硅唑"],
            ["番茄", "晚疫病", "水渍状", "白霉", "烯酰吗啉"],
            ["水稻", "稻瘟病", "褐色", "梭形斑", "三环唑"]
        ]
        bm25 = BM25Okapi(corpus)
        query_tokens = ["番茄", "叶霉病", "黄斑"]

        scores = bm25.get_scores(query_tokens)
        assert len(scores) == 3
        # 第一条文档完全命中“番茄”和“叶霉病”，得分应当最高
        assert scores[0] > scores[1]
        assert scores[0] > scores[2]


class TestQueryUnderstandingAndRouting:
    """Query Understanding 实体抽取与意图分流测试"""

    def test_entity_extraction(self):
        """测试从自然语言查询中提取作物、生长期、病害等核心实体"""
        q1 = "江苏南京这边的番茄开花期叶片上有黄色斑点，长了霉层，这是叶霉病吗？"
        entities1 = QueryUnderstanding.extract_entities(q1)
        assert entities1.get("crop") == "番茄"
        assert entities1.get("growth_stage") == "开花期"
        assert entities1.get("disease") == "叶霉病"
        assert entities1.get("region") in ["江苏", "南京"]
        assert entities1.get("topic") == "disease"

        q2 = "西红柿坐果期需要施什么复合肥？"
        entities2 = QueryUnderstanding.extract_entities(q2)
        assert entities2.get("crop") == "番茄"  # 规范化别名
        assert entities2.get("growth_stage") == "坐果期"
        assert entities2.get("topic") == "fertilizer"

    def test_intent_routing(self):
        """测试 Query Router 将问题分类到五大意图通道"""
        # DIAGNOSIS: 诊断排查
        intent_diag = QueryUnderstanding.route_intent(
            "上传了叶片照片，叶子发黄长毛得了什么病？",
            {"crop": "番茄"}
        )
        assert intent_diag == "DIAGNOSIS"

        # DECISION: 农事决策
        intent_dec = QueryUnderstanding.route_intent(
            "明天有中雨，今天适合喷药打药吗？",
            {"topic": "weather"}
        )
        assert intent_dec == "DECISION"

        # PLANNING: 方案规划
        intent_plan = QueryUnderstanding.route_intent(
            "帮我制定一份番茄全周期水肥管理计划方案",
            {"crop": "番茄", "topic": "fertilizer"}
        )
        assert intent_plan == "PLANNING"

        # FACT: 标准事实规范
        intent_fact = QueryUnderstanding.route_intent(
            "GB/T标准中关于番茄叶霉病的登记农药有哪些？",
            {"crop": "番茄", "disease": "叶霉病"}
        )
        assert intent_fact == "FACT"


class TestHybridRAGFusion:
    """Hybrid RAG 混合检索与双路融合测试"""

    @pytest.mark.asyncio
    async def test_hybrid_search_fusion_and_authority_bonus(self):
        """测试向量+BM25多路融合、权威等级加分与元数据优先召回"""
        rag = RAGService(similarity_threshold=0.20, context_top_n=3)

        knowledge_items = [
            {
                "id": "kb_tomato_leafmold_standard",
                "question": "番茄叶霉病开花期国家防治规范与药剂说明",
                "answer": "推荐选用40%氟硅唑乳油4000倍液喷雾防治，交替轮换用药。",
                "keywords": ["番茄", "叶霉病", "开花期", "氟硅唑"],
                "crop": "番茄",
                "growth_stage": "开花期",
                "topic": "disease",
                "disease": "番茄叶霉病",
                "authority_level": "L1",
                "source_name": "隔离测试标注来源（非真实标准）"
            },
            {
                "id": "kb_tomato_late_blight",
                "question": "番茄晚疫病低温高湿防治",
                "answer": "选用甲霜·锰锌或烯酰吗啉喷雾。",
                "keywords": ["番茄", "晚疫病", "白霉"],
                "crop": "番茄",
                "growth_stage": "坐果期",
                "topic": "disease",
                "disease": "番茄晚疫病",
                "authority_level": "L2",
                "source_name": "农科文献"
            },
            {
                "id": "kb_general_chat",
                "question": "番茄怎么做西红柿炒鸡蛋",
                "answer": "准备鸡蛋两颗，番茄切块，热油下锅翻炒。",
                "keywords": ["番茄", "西红柿炒蛋"],
                "category": "闲聊",
                "authority_level": "L4"
            }
        ]

        for row in knowledge_items:
            row.update(source_type="test_fixture", is_current=1)
        query = "番茄开花期叶霉病发病了，打什么药？"
        search_result = await rag.hybrid_search(
            query=query,
            knowledge_items=knowledge_items,
            filter_metadata={"crop": "番茄"},
            threshold=0.20,
            top_k=2
        )

        assert search_result.search_method == "hybrid"
        assert search_result.intent in ["DIAGNOSIS", "FACT"]
        assert len(search_result.retrieved_items) >= 1

        top_hit = search_result.retrieved_items[0]
        # 第一条命中应当精准为叶霉病规范条目
        assert top_hit["id"] == "kb_tomato_leafmold_standard"
        assert top_hit["authority_level"] == "L1"
        assert top_hit["bm25_score"] > 0.0
        assert search_result.confidence > 0.60
        assert search_result.need_expert is False

    @pytest.mark.asyncio
    async def test_low_confidence_triggers_expert_referral(self):
        """测试偏门/低置信度问题触发拒答与转专家复核标记 (need_expert = True)"""
        rag = RAGService(similarity_threshold=0.20)
        knowledge_items = [
            {
                "id": "kb_tomato_normal",
                "question": "番茄定植水浇透原则",
                "answer": "定植时必须浇透定根水。",
                "keywords": ["定植", "浇水"]
            }
        ]

        # 查询完全无关且罕见的问题
        query = "火星土壤上种植番茄变异发黑叶片脱落怎么治疗？"
        search_result = await rag.hybrid_search(
            query=query,
            knowledge_items=knowledge_items,
            threshold=0.60,
            top_k=2
        )

        # 相似度极低或无有效匹配，置信度应低于 0.60，自动判定 need_expert = True
        assert search_result.confidence < 0.60
        assert search_result.need_expert is True


class TestHybridSearchAPIEndpoint:
    """REST API /api/knowledge/hybrid-search 端点集成测试"""

    @pytest.mark.asyncio
    async def test_api_hybrid_search(
        self,
        knowledge_client: AsyncClient,
        test_session: AsyncSession,
        admin_user: User
    ):
        """测试前端/工作台调用 POST /api/knowledge/hybrid-search 获取诊断结果"""
        # 预先录入一条番茄知识
        await knowledge_service.create_knowledge(
            db=test_session,
            question="番茄叶霉病初期有哪些特征？",
            answer="叶片正面出现不规则黄色褪绿斑点，背面有灰褐色绒状霉层。",
            keywords=["番茄", "叶霉病", "黄斑", "霉层"],
            crop="番茄",
            growth_stage="开花期",
            topic="disease",
            disease="番茄叶霉病",
            authority_level="L1",
            source_name="标准防病指南",
            source_type="test_fixture",
            sync_vector=False
        )

        token = AuthService.create_access_token({"sub": str(admin_user.id), "username": admin_user.username, "account_subject": admin_user.auth_subject})
        headers = _auth_header(token)

        response = await knowledge_client.post(
            "/api/knowledge/hybrid-search",
            headers=headers,
            json={
                "query": "番茄叶片上有黄色斑点和霉层，是叶霉病吗？",
                "crop": "番茄",
                "top_k": 3,
                "min_confidence": 0.20
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "番茄叶片上有黄色斑点和霉层，是叶霉病吗？"
        assert data["intent"] in ["DIAGNOSIS", "FACT"]
        assert len(data["retrieved_items"]) >= 1
        assert data["retrieved_items"][0]["disease"] == "番茄叶霉病"
        assert "context_text" in data
        assert "confidence" in data


class TestKnowledgeItemMetadataPersistence:
    """知识库扩展元数据（publish_date, url, source_updated_at等）持久化测试"""

    @pytest.mark.asyncio
    async def test_metadata_fields_persistence_and_seed_completeness(
        self,
        test_session: AsyncSession
    ):
        """测试新增溯源元数据字段能够正常存取且种子库100%填齐"""
        from app.services.seed_agriculture_knowledge import TOMATO_KNOWLEDGE_SEED

        # 1. Unverified sample metadata must not become fabricated source evidence.
        assert len(TOMATO_KNOWLEDGE_SEED) >= 65
        for idx, seed in enumerate(TOMATO_KNOWLEDGE_SEED):
            assert seed['source_type'] == 'quarantined'
            assert seed['authority_level'] == '未核验'
            assert seed.get('effective_to') is None
            assert seed.get('publish_date') is None
            assert seed.get('url') is None

        # 2. 验证模型创建与持久化
        item = await knowledge_service.create_knowledge(
            db=test_session,
            question="番茄晚疫病高发期如何科学用药？",
            answer="发病初期喷施68.75%氟吡菌胺·霜霉威悬浮剂800倍液。",
            crop="番茄",
            publish_date="2020-07-27",
            url="http://openstd.samr.gov.cn/example",
            source_updated_at="2020-07-27",
            effective_from="2020-11-01",
            effective_to=None,
            sync_vector=False
        )

        assert item.publish_date == "2020-07-27"
        assert item.url == "http://openstd.samr.gov.cn/example"
        assert item.source_updated_at == "2020-07-27"
        assert item.effective_from == "2020-11-01"
        assert item.effective_to is None

        # 3. 验证更新字段
        updated = await knowledge_service.update_knowledge(
            db=test_session,
            knowledge_id=item.id,
            url="http://new-url.gov.cn",
            sync_vector=False
        )
        assert updated is not None
        assert updated.url == "http://new-url.gov.cn"
