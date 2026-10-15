# -*- coding: utf-8 -*-
"""Logs API schemas."""

from enum import Enum
from typing import List, Dict, Any

from pydantic import BaseModel, Field


class LogType(str, Enum):
    app = "app"
    error = "error"


class LogEntryResponse(BaseModel):
    timestamp: str = Field(description="Log timestamp")
    level: str = Field(description="Log level")
    source: str = Field(default="", description="Logger source")
    message: str = Field(description="Log message")
    context: Dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None


class LogFileResponse(BaseModel):
    name: str = Field(description="File name")
    size: int = Field(description="File size in bytes")
    modified: str = Field(description="Modified time")


class LogListResponse(BaseModel):
    items: List[LogEntryResponse] = Field(description="Log entries")
    total: int = Field(description="Total entries")
    page: int = Field(description="Current page")
    page_size: int = Field(description="Page size")
    truncated: bool = False
    query_scope: str = "recent_file_tails"
    scanned_bytes: int = 0
    max_scan_bytes: int = 8 * 1024 * 1024
    file_tail_bytes: int = 1024 * 1024
    legacy_time_entries: int = 0
    unavailable_files: int = 0
    unparsed_lines: int = 0


class LogFilesResponse(BaseModel):
    files: List[LogFileResponse] = Field(description="Log files")


class LogClearRequest(BaseModel):
    log_type: LogType = Field(description="Log type: app or error")


class LogClearResponse(BaseModel):
    success: bool = Field(description="Whether clear action succeeded")
    message: str = Field(description="Result message")
    cleared_count: int = Field(default=0, description="Cleared file count")
