# -*- coding: utf-8 -*-
"""Logs management API endpoints."""

from typing import Optional
from datetime import datetime
from uuid import UUID
from starlette.concurrency import run_in_threadpool
from app.core.audit_logging import audit

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response
from fastapi.responses import PlainTextResponse

from app.api.dependencies import require_admin
from app.models.user import User
from app.schemas.logs import (
    LogClearRequest,
    LogClearResponse,
    LogEntryResponse,
    LogFileResponse,
    LogFilesResponse,
    LogListResponse,
    LogType,
)
from app.services.log_service import get_log_service

def private_response(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(private_response)])


@router.get("/files", response_model=LogFilesResponse)
async def get_log_files(
    current_user: User = Depends(require_admin()),
):
    service = get_log_service()
    files = await run_in_threadpool(service.get_log_files)

    return LogFilesResponse(
        files=[
            LogFileResponse(name=f.name, size=f.size, modified=f.modified)
            for f in files
        ]
    )


@router.get("", response_model=LogListResponse)
async def get_logs(
    page: int = Query(default=1, ge=1, le=1000, description="Page number"),
    page_size: int = Query(default=50, ge=1, le=500, description="Page size"),
    level: Optional[str] = Query(
        default=None,
        description="Log level: DEBUG/INFO/WARNING/ERROR/CRITICAL",
    ),
    keyword: Optional[str] = Query(default=None, max_length=200, description="Message keyword"),
    start_time: Optional[datetime] = Query(default=None, description="Start time (ISO format)"),
    end_time: Optional[datetime] = Query(default=None, description="End time (ISO format)"),
    request_id: Optional[UUID] = None,
    business_request_id: Optional[UUID] = None,
    conversation_id: Optional[UUID] = None,
    consultation_id: Optional[UUID] = None,
    field_id: Optional[int] = Query(None, ge=1),
    record_id: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(require_admin()),
):
    if level and level.upper() not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid log level",
        )

    service = get_log_service()
    try:
        snapshot = await run_in_threadpool(service.read_log_paginated,
            level=level,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
            page=page,
            page_size=page_size,
            request_id=request_id, business_request_id=business_request_id,
            conversation_id=conversation_id, consultation_id=consultation_id,
            field_id=field_id, record_id=record_id,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None

    return LogListResponse(
        items=[
            LogEntryResponse(
                timestamp=e.timestamp,
                level=e.level,
                source=e.logger_name,
                message=e.message,
                context=e.context, event_id=e.event_id,
            )
            for e in snapshot.entries
        ],
        total=snapshot.total,
        truncated=snapshot.truncated, scanned_bytes=snapshot.scanned_bytes,
        legacy_time_entries=snapshot.legacy_time_entries, unavailable_files=snapshot.unavailable_files,
        unparsed_lines=snapshot.unparsed_lines,
        page=page,
        page_size=page_size,
    )


@router.get("/download")
async def download_log(
    log_type: LogType = Query(default=LogType.app, description="Log type: app or error"),
    current_user: User = Depends(require_admin()),
):
    filename = f"{log_type.value}.log"
    service = get_log_service()
    try:
        content = await run_in_threadpool(service.get_log_content, filename)
    except ValueError as error:
        raise HTTPException(413, str(error)) from None
    except OSError:
        raise HTTPException(503, "日志暂不可读") from None
    if not content:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Log file not found",
        )

    audit("log_downloaded", actor_id=current_user.id)
    return PlainTextResponse(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "private, no-store"},
    )


@router.post("/clear", response_model=LogClearResponse)
async def clear_logs(
    body: LogClearRequest,
    current_user: User = Depends(require_admin()),
):
    service = get_log_service()
    filename = f"{body.log_type.value}.log"

    success = await run_in_threadpool(service.clear_log, filename)
    audit("log_clear_succeeded" if success else "log_clear_failed", actor_id=current_user.id)
    return LogClearResponse(
        success=success,
        message=f"Log file {filename} cleared" if success else "Failed to clear log file",
        cleared_count=1 if success else 0,
    )
