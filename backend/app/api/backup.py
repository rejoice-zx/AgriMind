"""
Backup and Restore API Endpoints

Provides endpoints for database backup, restore, and data migration.
Requirements: 14.3, 14.5
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
import json
import io

from app.database import get_db
from app.api.dependencies import require_admin
from app.models.user import User
from app.services.backup_service import BackupService
from app.schemas.backup import (
    BackupInfo,
    CreateBackupRequest,
    CreateBackupResponse,
    RestoreBackupRequest,
    RestoreBackupResponse,
    ListBackupsResponse,
    DeleteBackupResponse,
    ExportDataResponse,
    MigrationStatusResponse,
)


router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.post("", response_model=CreateBackupResponse)
async def create_backup(
    request: CreateBackupRequest = None,
    current_user: User = Depends(require_admin())
):
    """
    创建数据库备份
    
    需要管理员权限
    
    参数:
    - description: 备份描述（可选）
    
    返回:
    - 备份创建结果和备份信息
    """
    description = request.description if request else ""
    success, message, filename = await BackupService.create_backup(description)
    
    backup_info = None
    if success and filename:
        info = await BackupService.get_backup_info(filename)
        if info:
            backup_info = BackupInfo(**info)
    
    return CreateBackupResponse(
        success=success,
        message=message,
        backup=backup_info
    )


@router.get("", response_model=ListBackupsResponse)
async def list_backups(
    current_user: User = Depends(require_admin())
):
    """
    获取所有备份列表
    
    需要管理员权限
    
    返回:
    - 备份列表（按创建时间倒序）
    """
    backups = await BackupService.list_backups()
    
    return ListBackupsResponse(
        backups=[BackupInfo(**b) for b in backups],
        total=len(backups)
    )


@router.post("/restore", response_model=RestoreBackupResponse)
async def restore_backup(
    request: RestoreBackupRequest,
    current_user: User = Depends(require_admin())
):
    """
    从备份恢复数据库
    
    需要管理员权限
    
    在线覆盖恢复已停用，返回离线恢复提示。
    
    参数:
    - filename: 要恢复的备份文件名
    
    返回:
    - 恢复结果
    """
    success, message = await BackupService.restore_backup(request.filename)
    
    return RestoreBackupResponse(
        success=success,
        message=message
    )


@router.get('/bundle/{filename}', summary='下载数据库与本地诊断媒体备份包')
async def download_backup_bundle(filename: str, current_user: User = Depends(require_admin())):
    try:
        path = await BackupService.bundle_path(filename)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None
    return FileResponse(path, media_type='application/zip', filename=path.name, headers={'Cache-Control': 'no-store'})


@router.delete("/{filename}", response_model=DeleteBackupResponse)
async def delete_backup(
    filename: str,
    current_user: User = Depends(require_admin())
):
    """
    删除指定备份
    
    需要管理员权限
    
    参数:
    - filename: 要删除的备份文件名
    
    返回:
    - 删除结果
    """
    success, message = await BackupService.delete_backup(filename)
    
    return DeleteBackupResponse(
        success=success,
        message=message
    )


@router.get("/export", response_model=ExportDataResponse)
async def export_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin())
):
    """
    导出数据库数据为JSON格式
    
    需要管理员权限
    
    返回:
    - 导出的数据（JSON格式）
    """
    success, message, data = await BackupService.export_to_json(db)
    
    return ExportDataResponse(
        success=success,
        message=message,
        data=data
    )


@router.get("/export/download")
async def download_export(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin())
):
    """
    下载导出的数据库数据（JSON文件）
    
    需要管理员权限
    
    返回:
    - JSON文件下载
    """
    success, message, data = await BackupService.export_to_json(db)
    
    if not success or not data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=message
        )
    
    # Create JSON content
    json_content = json.dumps(data, indent=2, ensure_ascii=False)
    
    # Return as downloadable file
    return StreamingResponse(
        io.BytesIO(json_content.encode("utf-8")),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=database_export_{data['exported_at'][:10]}.json"
        }
    )


@router.post("/migrate", response_model=MigrationStatusResponse)
async def run_migration(
    clear_existing: bool = Query(False, description="是否清除现有数据"),
    current_user: User = Depends(require_admin())
):
    """
    运行JSON到SQLite数据迁移
    
    需要管理员权限
    
    将data目录下的JSON文件迁移到SQLite数据库:
    - users.json -> users表
    - knowledge_base.json -> knowledge_items表
    - products.json -> products表
    - conversations/*.json -> conversations和messages表
    - settings.json -> system_settings表
    
    参数:
    - clear_existing: 是否清除现有数据（默认False）
    
    返回:
    - 迁移结果
    """
    # Legacy imports can commit a destructive clear before input validation.
    # Keep the authenticated route as an explicit retirement response.
    raise HTTPException(410, "在线旧数据迁移已停用；请在独立目标库验证迁移和恢复后再切换")
