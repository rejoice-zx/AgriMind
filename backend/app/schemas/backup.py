"""Backup and Restore API Schemas"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class BackupInfo(BaseModel):
    """Backup file information"""
    filename: str = Field(..., description="Backup filename")
    created_at: str = Field(..., description="Backup creation timestamp")
    description: str = Field(default="", description="Backup description")
    size_bytes: int = Field(..., description="Backup file size in bytes")
    scope: str = "legacy_unverified"
    media_included: bool = False
    restore_mode: str = "offline_only"
    bundle_filename: Optional[str] = None
    bundle_size_bytes: Optional[int] = None
    schema_revision: Optional[int] = None
    media_files: Optional[int] = None


class CreateBackupRequest(BaseModel):
    """Request to create a new backup"""
    description: str = Field(default="", description="Optional description for the backup")


class CreateBackupResponse(BaseModel):
    """Response after creating a backup"""
    success: bool = Field(..., description="Whether backup was successful")
    message: str = Field(..., description="Status message")
    backup: Optional[BackupInfo] = Field(None, description="Backup information if successful")


class RestoreBackupRequest(BaseModel):
    """Request to restore from a backup"""
    filename: str = Field(..., description="Backup filename to restore from")


class RestoreBackupResponse(BaseModel):
    """Response after restoring from a backup"""
    success: bool = Field(..., description="Whether restore was successful")
    message: str = Field(..., description="Status message")


class ListBackupsResponse(BaseModel):
    """Response containing list of available backups"""
    backups: List[BackupInfo] = Field(default_factory=list, description="List of available backups")
    total: int = Field(..., description="Total number of backups")


class DeleteBackupResponse(BaseModel):
    """Response after deleting a backup"""
    success: bool = Field(..., description="Whether deletion was successful")
    message: str = Field(..., description="Status message")


class ExportDataResponse(BaseModel):
    """Response containing exported data"""
    success: bool = Field(..., description="Whether export was successful")
    message: str = Field(..., description="Status message")
    data: Optional[Dict[str, Any]] = Field(None, description="Exported data")


class MigrationStatusResponse(BaseModel):
    """Response containing migration status"""
    success: bool = Field(..., description="Whether migration was successful")
    message: str = Field(..., description="Status message")
    results: Optional[Dict[str, Any]] = Field(None, description="Migration results by table")
