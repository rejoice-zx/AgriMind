"""
Database Backup and Restore Service

Provides functionality to backup and restore the SQLite database.
Requirements: 14.5
"""
import asyncio
import re
from uuid import uuid4
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
import aiofiles

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeItem
from app.models.product import Product
from app.models.settings import SystemSettings


class BackupService:
    """Service for database backup and restore operations"""
    
    BACKUP_DIR = Path(settings.DATA_DIR) / "backups"
    DB_PATH = Path(settings.DATABASE_URL.replace("sqlite+aiosqlite:///", ""))

    @staticmethod
    def _needs_manifest(name: str) -> bool:
        return bool(re.fullmatch(r'backup_\d{8}_\d{6}_[0-9a-f]{32}\.db', name))

    @staticmethod
    def _valid_metadata(metadata, path):
        return (isinstance(metadata, dict) and metadata.get('filename') == path.name and
                isinstance(metadata.get('created_at'), str) and type(metadata.get('size_bytes')) is int and
                metadata['size_bytes'] == path.stat().st_size)

    @classmethod
    async def _get_safe_backup_path(cls, backup_filename: str) -> Path:
        await cls.ensure_backup_dir()

        name = Path(backup_filename).name
        if name != backup_filename:
            raise ValueError("Invalid backup filename")

        if not name.startswith("backup_") or not name.endswith(".db"):
            raise ValueError("Invalid backup filename")

        backup_dir = cls.BACKUP_DIR.resolve()
        backup_path = (cls.BACKUP_DIR / name).resolve()
        if backup_dir not in backup_path.parents:
            raise ValueError("Invalid backup filename")

        return backup_path
    
    @classmethod
    async def ensure_backup_dir(cls) -> Path:
        """Ensure backup directory exists"""
        if not cls.BACKUP_DIR.exists():
            cls.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return cls.BACKUP_DIR
    
    @classmethod
    def generate_backup_filename(cls) -> str:
        """Generate a unique backup filename with timestamp"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"backup_{timestamp}_{uuid4().hex}.db"

    @staticmethod
    def _snapshot_database(source: Path, destination: Path) -> None:
        """SQLite online backup includes committed WAL pages; never overwrite a file."""
        from app.core.recovery_bundle import snapshot_database
        snapshot_database(source, destination)
    
    @classmethod
    async def create_backup(cls, description: str = "") -> Tuple[bool, str, Optional[str]]:
        """
        Create a backup of the current database
        
        Args:
            description: Optional description for the backup
            
        Returns:
            Tuple of (success, message, backup_filename)
        """
        try:
            await cls.ensure_backup_dir()
            
            # Check if database exists
            if not cls.DB_PATH.exists():
                return False, "Database file not found", None
            
            # Generate backup filename
            backup_filename = cls.generate_backup_filename()
            backup_path = cls.BACKUP_DIR / backup_filename
            
            await asyncio.to_thread(cls._snapshot_database, cls.DB_PATH, backup_path)
            from app.core.recovery_bundle import create_bundle
            bundle = await asyncio.to_thread(create_bundle, backup_path, Path(settings.DATA_DIR),
                backup_path.with_suffix('.zip'), description=description, app_version=settings.APP_VERSION)
            
            # Create metadata file
            metadata = {
                "filename": backup_filename,
                "created_at": datetime.now().isoformat(),
                "description": description,
                "size_bytes": backup_path.stat().st_size,
                "scope": "database_and_local_diagnosis_media",
                "media_included": True,
                "bundle_filename": backup_path.with_suffix('.zip').name,
                "bundle_size_bytes": backup_path.with_suffix('.zip').stat().st_size,
                "schema_revision": bundle['schema_revision'],
                "media_files": len(bundle['files']) - 1,
                "restore_mode": "offline_only",
            }
            
            metadata_path = backup_path.with_suffix(".json")
            async with aiofiles.open(metadata_path, "x", encoding="utf-8") as f:
                await f.write(json.dumps(metadata, indent=2, ensure_ascii=False))
            
            return True, f"数据库与本地诊断媒体备份包已创建，可下载并在独立目录核验恢复：{backup_filename}", backup_filename
            
        except Exception as e:
            return False, f"Backup failed: {str(e)}", None
    
    @classmethod
    async def restore_backup(cls, backup_filename: str) -> Tuple[bool, str]:
        """
        Restore database from a backup file
        
        Args:
            backup_filename: Name of the backup file to restore
            
        Returns:
            Tuple of (success, message)
        """
        try:
            backup_path = await cls._get_safe_backup_path(backup_filename)
            
            # Check if backup exists
            if not backup_path.exists():
                return False, f"Backup file not found: {backup_filename}"
            
            return False, "在线覆盖恢复已停用。请停服后在独立目录验证数据库、媒体和版本，再由管理员切换。"
            
        except Exception as e:
            return False, f"Restore failed: {str(e)}"

    @classmethod
    async def list_backups(cls) -> List[Dict[str, Any]]:
        """
        List all available backups
        
        Returns:
            List of backup metadata dictionaries
        """
        backups = []
        
        await cls.ensure_backup_dir()
        
        for backup_file in cls.BACKUP_DIR.glob("backup_*.db"):
            metadata_file = backup_file.with_suffix(".json")
            if cls._needs_manifest(backup_file.name) and not metadata_file.exists():
                continue  # An interrupted new snapshot must not appear as a successful backup.
            
            if metadata_file.exists():
                try:
                    async with aiofiles.open(metadata_file, "r", encoding="utf-8") as f:
                        content = await f.read()
                        metadata = json.loads(content)
                        if not cls._valid_metadata(metadata, backup_file):
                            raise ValueError('Invalid backup metadata')
                        backups.append(metadata)
                except Exception:
                    if cls._needs_manifest(backup_file.name):
                        continue
                    # If metadata file is corrupted, create basic info
                    backups.append({
                        "filename": backup_file.name,
                        "created_at": datetime.fromtimestamp(
                            backup_file.stat().st_mtime
                        ).isoformat(),
                        "description": "",
                        "size_bytes": backup_file.stat().st_size,
                    })
            else:
                # No metadata file, create basic info
                backups.append({
                    "filename": backup_file.name,
                    "created_at": datetime.fromtimestamp(
                        backup_file.stat().st_mtime
                    ).isoformat(),
                    "description": "",
                    "size_bytes": backup_file.stat().st_size,
                })
        
        # Sort by creation time, newest first
        backups.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        return backups
    
    @classmethod
    async def delete_backup(cls, backup_filename: str) -> Tuple[bool, str]:
        """
        Delete a backup file
        
        Args:
            backup_filename: Name of the backup file to delete
            
        Returns:
            Tuple of (success, message)
        """
        try:
            backup_path = await cls._get_safe_backup_path(backup_filename)
            metadata_path = backup_path.with_suffix(".json")
            
            if not backup_path.exists():
                return False, f"Backup file not found: {backup_filename}"
            
            # Delete backup file
            backup_path.unlink()
            bundle_path = backup_path.with_suffix('.zip')
            if bundle_path.is_file() or bundle_path.is_symlink():
                bundle_path.unlink()
            
            # Delete metadata file if exists
            if metadata_path.exists():
                metadata_path.unlink()
            
            return True, f"Backup deleted successfully: {backup_filename}"
            
        except Exception as e:
            return False, f"Delete failed: {str(e)}"
    
    @classmethod
    async def get_backup_info(cls, backup_filename: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific backup
        
        Args:
            backup_filename: Name of the backup file
            
        Returns:
            Backup metadata dictionary or None if not found
        """
        try:
            backup_path = await cls._get_safe_backup_path(backup_filename)
        except Exception:
            return None
        metadata_path = backup_path.with_suffix(".json")
        
        if not backup_path.exists():
            return None
        if cls._needs_manifest(backup_filename) and not metadata_path.exists():
            return None
        
        if metadata_path.exists():
            try:
                async with aiofiles.open(metadata_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    metadata = json.loads(content)
                    if not cls._valid_metadata(metadata, backup_path):
                        raise ValueError('Invalid backup metadata')
                    return metadata
            except Exception:
                if cls._needs_manifest(backup_filename):
                    return None
        
        # Return basic info if metadata not available
        return {
            "filename": backup_filename,
            "created_at": datetime.fromtimestamp(
                backup_path.stat().st_mtime
            ).isoformat(),
            "description": "",
            "size_bytes": backup_path.stat().st_size,
        }

    @classmethod
    async def bundle_path(cls, backup_filename: str) -> Path:
        backup_path = await cls._get_safe_backup_path(backup_filename)
        info = await cls.get_backup_info(backup_filename)
        path = backup_path.with_suffix('.zip')
        if not info or info.get('bundle_filename') != path.name or path.is_symlink() or not path.is_file():
            raise ValueError('该备份没有已完成的媒体包；请重新创建备份')
        if not path.resolve().is_relative_to(cls.BACKUP_DIR.resolve()):
            raise ValueError('Invalid bundle path')
        return path
    
    @classmethod
    async def export_to_json(cls, db: AsyncSession) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Export all database data to JSON format
        
        Args:
            db: Database session
            
        Returns:
            Tuple of (success, message, data_dict)
        """
        try:
            data = {
                "exported_at": datetime.now().isoformat(),
                "users": [],
                "knowledge_items": [],
                "products": [],
                "conversations": [],
                "settings": [],
            }
            
            # Export users (excluding password hashes for security)
            users_result = await db.execute(select(User))
            for user in users_result.scalars().all():
                data["users"].append({
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "display_name": user.display_name,
                    "email": user.email,
                    "is_active": user.is_active,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                })
            
            # Export knowledge items
            knowledge_result = await db.execute(select(KnowledgeItem))
            for item in knowledge_result.scalars().all():
                data["knowledge_items"].append({
                    column.name: (value.isoformat() if isinstance(value, (datetime, date)) else value)
                    for column in KnowledgeItem.__table__.columns
                    for value in [getattr(item, column.name)]
                })
            
            # Export products
            products_result = await db.execute(select(Product))
            for product in products_result.scalars().all():
                data["products"].append({
                    "id": product.id,
                    "name": product.name,
                    "price": product.price,
                    "category": product.category,
                    "description": product.description,
                    "specifications": product.specifications,
                    "stock": product.stock,
                    "keywords": product.keywords,
                    "created_at": product.created_at.isoformat() if product.created_at else None,
                })
            
            # Export conversations with messages
            conversations_result = await db.execute(select(Conversation))
            for conv in conversations_result.scalars().all():
                conv_data = {
                    "id": conv.id,
                    "title": conv.title,
                    "status": conv.status,
                    "customer_id": getattr(conv, "customer_id", None),
                    "human_agent_id": conv.human_agent_id,
                    "created_at": conv.created_at.isoformat() if conv.created_at else None,
                    "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
                    "messages": [],
                }
                
                # Get messages for this conversation
                messages_result = await db.execute(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.timestamp)
                )
                for msg in messages_result.scalars().all():
                    conv_data["messages"].append({
                        "id": msg.id,
                        "role": msg.role,
                        "content": msg.content,
                        "confidence": msg.confidence,
                        "rag_trace": msg.rag_trace,
                        "human_agent_name": getattr(msg, "human_agent_name", None),
                        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                    })
                
                data["conversations"].append(conv_data)
            
            # Export settings
            settings_result = await db.execute(select(SystemSettings))
            for setting in settings_result.scalars().all():
                data["settings"].append({
                    "key": setting.key,
                    "value": setting.value,
                    "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
                })
            
            # Agricultural tables are part of the JSON export as well as the physical DB backup.
            # Export only these domain tables; users/passwords and configuration keep their existing filters.
            from app.models import (Farm, Field, CropPlanting, FarmingRecord, DiseaseRecord,
                WeatherSnapshot, ExpertCase, PlanExecution, AgriConversation, AgriTurn, AgriConsultation, DiseaseReview, DiagnosisMedia, DiagnosisRequest)
            from app.models.agri_plan_snapshot import AgriPlanSnapshot
            from app.models.field_environment import FieldEnvironmentObservation
            from app.models.crop_status_event import CropStatusEvent
            from app.models.consultation_review import ConsultationReview
            for model in (Farm, Field, CropPlanting, FarmingRecord, DiseaseRecord, WeatherSnapshot,
                          ExpertCase, PlanExecution, AgriConversation, AgriTurn, AgriConsultation, DiseaseReview, DiagnosisMedia, DiagnosisRequest, AgriPlanSnapshot, FieldEnvironmentObservation, CropStatusEvent, ConsultationReview):
                rows = (await db.scalars(select(model))).all()
                data[model.__tablename__] = [
                    {column.name: (value.isoformat() if isinstance(value, (datetime, date)) else value)
                     for column in model.__table__.columns
                     for value in [getattr(row, column.name)]} for row in rows
                ]
            return True, "Data exported successfully", data
            
        except Exception as e:
            return False, f"Export failed: {str(e)}", None
