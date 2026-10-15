"""Tests for Backup and Restore Service"""
import pytest
from pathlib import Path
import shutil
import os

from app.services.backup_service import BackupService
from app.models.user import User
from app.models.knowledge import KnowledgeItem
from app.models.product import Product
from app.models.conversation import Conversation, Message
from app.models.settings import SystemSettings


class TestBackupService:
    """Test backup service functionality"""
    
    @pytest.fixture(autouse=True)
    def setup_backup_dir(self, tmp_path):
        """Setup temporary backup directory"""
        # Override backup directory for tests
        original_backup_dir = BackupService.BACKUP_DIR
        BackupService.BACKUP_DIR = tmp_path / "backups"
        yield
        BackupService.BACKUP_DIR = original_backup_dir
    
    @pytest.mark.asyncio
    async def test_ensure_backup_dir(self, tmp_path):
        """Test backup directory creation"""
        BackupService.BACKUP_DIR = tmp_path / "test_backups"
        
        result = await BackupService.ensure_backup_dir()
        
        assert result.exists()
        assert result.is_dir()
    
    def test_generate_backup_filename(self):
        """Test backup filename generation"""
        filename = BackupService.generate_backup_filename()
        
        assert filename.startswith("backup_")
        assert filename.endswith(".db")
        assert filename != BackupService.generate_backup_filename()
    
    @pytest.mark.asyncio
    async def test_list_backups_empty(self, tmp_path):
        """Test listing backups when none exist"""
        BackupService.BACKUP_DIR = tmp_path / "empty_backups"
        
        backups = await BackupService.list_backups()
        
        assert backups == []
    
    @pytest.mark.asyncio
    async def test_delete_backup_not_found(self, tmp_path):
        """Test deleting non-existent backup"""
        BackupService.BACKUP_DIR = tmp_path / "backups"
        await BackupService.ensure_backup_dir()
        
        success, message = await BackupService.delete_backup("backup_20200101_000000.db")
        
        assert not success
        assert "not found" in message.lower()
    
    @pytest.mark.asyncio
    async def test_get_backup_info_not_found(self, tmp_path):
        """Test getting info for non-existent backup"""
        BackupService.BACKUP_DIR = tmp_path / "backups"
        await BackupService.ensure_backup_dir()
        
        info = await BackupService.get_backup_info("backup_20200101_000000.db")
        
        assert info is None

    @pytest.mark.asyncio
    async def test_delete_backup_rejects_path_traversal(self, tmp_path):
        BackupService.BACKUP_DIR = tmp_path / "backups"
        await BackupService.ensure_backup_dir()

        success, message = await BackupService.delete_backup("../backup_20200101_000000.db")
        assert not success
        assert "invalid" in message.lower()

    @pytest.mark.asyncio
    async def test_restore_backup_rejects_path_traversal(self, tmp_path):
        BackupService.BACKUP_DIR = tmp_path / "backups"
        await BackupService.ensure_backup_dir()

        success, message = await BackupService.restore_backup("../backup_20200101_000000.db")
        assert not success
        assert "invalid" in message.lower()


class TestExportData:
    """Test data export functionality"""
    
    @pytest.mark.asyncio
    async def test_export_to_json_empty_db(self, test_session):
        """Test exporting empty database"""
        success, message, data = await BackupService.export_to_json(test_session)
        
        assert success
        assert data is not None
        assert "exported_at" in data
        assert "users" in data
        assert "knowledge_items" in data
        assert "products" in data
        assert "conversations" in data
        assert "settings" in data
    
    @pytest.mark.asyncio
    async def test_export_to_json_with_data(self, test_session):
        """Test exporting database with data"""
        # Add test data
        user = User(
            username="export_test_user",
            password_hash="test_hash",
            role="admin",
            display_name="Export Test"
        )
        test_session.add(user)
        
        knowledge = KnowledgeItem(
            id="K_EXPORT_001",
            question="Export test question?",
            answer="Export test answer",
            category="测试"
        )
        test_session.add(knowledge)
        
        product = Product(
            id="P_EXPORT_001",
            name="Export Test Product",
            price=99.99,
            category="测试",
            stock=10
        )
        test_session.add(product)
        
        await test_session.commit()
        
        # Export
        success, message, data = await BackupService.export_to_json(test_session)
        
        assert success
        assert data is not None
        assert len(data["users"]) >= 1
        assert len(data["knowledge_items"]) >= 1
        assert len(data["products"]) >= 1
        
        # Verify user data (password hash should not be included)
        exported_user = next((u for u in data["users"] if u["username"] == "export_test_user"), None)
        assert exported_user is not None
        assert "password_hash" not in exported_user
        assert exported_user["role"] == "admin"
        
        # Verify knowledge data
        exported_knowledge = next((k for k in data["knowledge_items"] if k["id"] == "K_EXPORT_001"), None)
        assert exported_knowledge is not None
        assert exported_knowledge["question"] == "Export test question?"
        
        # Verify product data
        exported_product = next((p for p in data["products"] if p["id"] == "P_EXPORT_001"), None)
        assert exported_product is not None
        assert exported_product["name"] == "Export Test Product"
        assert exported_product["price"] == 99.99
