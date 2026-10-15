# -*- coding: utf-8 -*-
"""Knowledge Service Tests"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.knowledge_service import KnowledgeService
from app.models.knowledge import KnowledgeItem


class TestKnowledgeCRUD:
    """知识库CRUD操作测试"""
    
    @pytest.fixture
    def service(self):
        """创建知识库服务实例"""
        return KnowledgeService()
    
    @pytest.mark.asyncio
    async def test_create_knowledge(self, test_session: AsyncSession, service: KnowledgeService):
        """测试创建知识条目"""
        item = await service.create_knowledge(
            db=test_session,
            question="什么是RAG？",
            answer="RAG是检索增强生成技术",
            keywords=["RAG", "检索", "生成"],
            category="技术",
            sync_vector=False
        )
        
        assert item is not None
        assert item.id is not None
        assert item.id.startswith("kb_")
        assert item.question == "什么是RAG？"
        assert item.answer == "RAG是检索增强生成技术"
        assert item.keywords == ["RAG", "检索", "生成"]
        assert item.category == "技术"
    
    @pytest.mark.asyncio
    async def test_create_knowledge_default_values(self, test_session: AsyncSession, service: KnowledgeService):
        """测试创建知识条目默认值"""
        item = await service.create_knowledge(
            db=test_session,
            question="测试问题",
            answer="测试答案",
            sync_vector=False
        )
        
        assert item.keywords == []
        assert item.category == "通用"
        assert item.score == 1.0
    
    @pytest.mark.asyncio
    async def test_get_knowledge(self, test_session: AsyncSession, service: KnowledgeService):
        """测试获取知识条目"""
        created = await service.create_knowledge(
            db=test_session,
            question="测试问题",
            answer="测试答案",
            sync_vector=False
        )
        
        retrieved = await service.get_knowledge(test_session, created.id)
        
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.question == "测试问题"
    
    @pytest.mark.asyncio
    async def test_get_knowledge_not_found(self, test_session: AsyncSession, service: KnowledgeService):
        """测试获取不存在的知识条目"""
        result = await service.get_knowledge(test_session, "non-existent-id")
        
        assert result is None

    @pytest.mark.asyncio
    async def test_get_knowledge_list_pagination(self, test_session: AsyncSession, service: KnowledgeService):
        """测试知识列表分页"""
        # 创建5个知识条目
        for i in range(5):
            await service.create_knowledge(
                db=test_session,
                question=f"问题{i}",
                answer=f"答案{i}",
                sync_vector=False
            )
        
        # 获取第一页
        items, total = await service.get_knowledge_list(
            test_session, page=1, page_size=2
        )
        
        assert len(items) == 2
        assert total == 5
        
        # 获取第二页
        items2, _ = await service.get_knowledge_list(
            test_session, page=2, page_size=2
        )
        
        assert len(items2) == 2
    
    @pytest.mark.asyncio
    async def test_get_knowledge_list_filter_category(self, test_session: AsyncSession, service: KnowledgeService):
        """测试按分类筛选"""
        await service.create_knowledge(
            db=test_session,
            question="技术问题1",
            answer="答案1",
            category="技术",
            sync_vector=False
        )
        await service.create_knowledge(
            db=test_session,
            question="产品问题1",
            answer="答案2",
            category="产品",
            sync_vector=False
        )
        
        items, total = await service.get_knowledge_list(
            test_session, category="技术"
        )
        
        assert total == 1
        assert items[0].category == "技术"
    
    @pytest.mark.asyncio
    async def test_get_knowledge_list_filter_keyword(self, test_session: AsyncSession, service: KnowledgeService):
        """测试按关键词筛选"""
        await service.create_knowledge(
            db=test_session,
            question="什么是RAG技术？",
            answer="RAG是检索增强生成",
            sync_vector=False
        )
        await service.create_knowledge(
            db=test_session,
            question="什么是LLM？",
            answer="LLM是大语言模型",
            sync_vector=False
        )
        
        items, total = await service.get_knowledge_list(
            test_session, keyword="RAG"
        )
        
        assert total == 1
        assert "RAG" in items[0].question
    
    @pytest.mark.asyncio
    async def test_update_knowledge(self, test_session: AsyncSession, service: KnowledgeService):
        """测试更新知识条目"""
        item = await service.create_knowledge(
            db=test_session,
            question="原问题",
            answer="原答案",
            sync_vector=False
        )
        
        updated = await service.update_knowledge(
            db=test_session,
            knowledge_id=item.id,
            question="新问题",
            answer="新答案",
            category="新分类",
            sync_vector=False
        )
        
        assert updated is not None
        assert updated.question == "新问题"
        assert updated.answer == "新答案"
        assert updated.category == "新分类"
    
    @pytest.mark.asyncio
    async def test_delete_knowledge(self, test_session: AsyncSession, service: KnowledgeService):
        """测试删除知识条目"""
        item = await service.create_knowledge(
            db=test_session,
            question="测试问题",
            answer="测试答案",
            sync_vector=False
        )
        
        success = await service.delete_knowledge(test_session, item.id, sync_vector=False)
        
        assert success is True
        
        # 验证已删除
        result = await service.get_knowledge(test_session, item.id)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_delete_knowledge_not_found(self, test_session: AsyncSession, service: KnowledgeService):
        """测试删除不存在的知识条目"""
        success = await service.delete_knowledge(test_session, "non-existent", sync_vector=False)
        
        assert success is False


class TestDuplicateDetection:
    """重复检测测试"""
    
    @pytest.fixture
    def service(self):
        return KnowledgeService()
    
    @pytest.mark.asyncio
    async def test_check_duplicate_found(self, test_session: AsyncSession, service: KnowledgeService):
        """测试检测到重复"""
        await service.create_knowledge(
            db=test_session,
            question="重复问题",
            answer="答案",
            sync_vector=False
        )
        
        duplicate = await service.check_duplicate(test_session, "重复问题")
        
        assert duplicate is not None
        assert duplicate.question == "重复问题"
    
    @pytest.mark.asyncio
    async def test_check_duplicate_not_found(self, test_session: AsyncSession, service: KnowledgeService):
        """测试未检测到重复"""
        await service.create_knowledge(
            db=test_session,
            question="问题A",
            answer="答案",
            sync_vector=False
        )
        
        duplicate = await service.check_duplicate(test_session, "问题B")
        
        assert duplicate is None
    
    @pytest.mark.asyncio
    async def test_check_duplicate_exclude_self(self, test_session: AsyncSession, service: KnowledgeService):
        """测试排除自身的重复检测"""
        item = await service.create_knowledge(
            db=test_session,
            question="问题",
            answer="答案",
            sync_vector=False
        )
        
        # 排除自身时不应检测到重复
        duplicate = await service.check_duplicate(test_session, "问题", exclude_id=item.id)
        
        assert duplicate is None


class TestImportExport:
    """导入导出测试"""
    
    @pytest.fixture
    def service(self):
        return KnowledgeService()
    
    @pytest.mark.asyncio
    async def test_export_all(self, test_session: AsyncSession, service: KnowledgeService):
        """测试导出所有知识条目"""
        await service.create_knowledge(
            db=test_session,
            question="问题1",
            answer="答案1",
            category="分类1",
            sync_vector=False
        )
        await service.create_knowledge(
            db=test_session,
            question="问题2",
            answer="答案2",
            category="分类2",
            sync_vector=False
        )
        
        exported = await service.export_all(test_session)
        
        assert len(exported) == 2
        assert exported[0]["question"] == "问题1"
        assert exported[1]["question"] == "问题2"
    
    @pytest.mark.asyncio
    async def test_import_items(self, test_session: AsyncSession, service: KnowledgeService):
        """测试批量导入"""
        items = [
            {"question": "导入问题1", "answer": "导入答案1", "category": "导入分类"},
            {"question": "导入问题2", "answer": "导入答案2", "category": "导入分类"},
        ]
        
        success_count, skip_count, errors = await service.import_items(
            db=test_session,
            items=items,
            sync_vector=False
        )
        
        assert success_count == 2
        assert skip_count == 0
        assert len(errors) == 0
        
        # 验证导入成功
        all_items, total = await service.get_knowledge_list(test_session)
        assert total == 2
    
    @pytest.mark.asyncio
    async def test_import_skip_duplicates(self, test_session: AsyncSession, service: KnowledgeService):
        """测试导入跳过重复项"""
        # 先创建一个
        await service.create_knowledge(
            db=test_session,
            question="已存在问题",
            answer="已存在答案",
            sync_vector=False
        )
        
        items = [
            {"question": "已存在问题", "answer": "新答案"},  # 重复
            {"question": "新问题", "answer": "新答案"},
        ]
        
        success_count, skip_count, errors = await service.import_items(
            db=test_session,
            items=items,
            skip_duplicates=True,
            sync_vector=False
        )
        
        assert success_count == 1
        assert skip_count == 1
    
    @pytest.mark.asyncio
    async def test_import_empty_question(self, test_session: AsyncSession, service: KnowledgeService):
        """测试导入空问题"""
        items = [
            {"question": "", "answer": "答案"},
            {"question": "有效问题", "answer": "有效答案"},
        ]
        
        success_count, skip_count, errors = await service.import_items(
            db=test_session,
            items=items,
            sync_vector=False
        )
        
        assert success_count == 1
        assert len(errors) == 1


class TestCategories:
    """分类管理测试"""
    
    @pytest.fixture
    def service(self):
        return KnowledgeService()
    
    @pytest.mark.asyncio
    async def test_get_categories(self, test_session: AsyncSession, service: KnowledgeService):
        """测试获取所有分类"""
        await service.create_knowledge(
            db=test_session,
            question="问题1",
            answer="答案1",
            category="分类A",
            sync_vector=False
        )
        await service.create_knowledge(
            db=test_session,
            question="问题2",
            answer="答案2",
            category="分类B",
            sync_vector=False
        )
        await service.create_knowledge(
            db=test_session,
            question="问题3",
            answer="答案3",
            category="分类A",  # 重复分类
            sync_vector=False
        )
        
        categories = await service.get_categories(test_session)
        
        assert len(categories) == 2
        assert "分类A" in categories
        assert "分类B" in categories
    
    @pytest.mark.asyncio
    async def test_get_total_count(self, test_session: AsyncSession, service: KnowledgeService):
        """测试获取总数"""
        for i in range(3):
            await service.create_knowledge(
                db=test_session,
                question=f"问题{i}",
                answer=f"答案{i}",
                sync_vector=False
            )
        
        count = await service.get_total_count(test_session)
        
        assert count == 3
