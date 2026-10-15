"""Bounded snapshots of approved log files; results are redacted and never imply all-history counts."""
import json
import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.config import settings
from app.core.audit_logging import redact, clean_fields


@dataclass
class LogEntry:
    timestamp: str
    level: str
    logger_name: str
    message: str
    raw_line: str
    context: dict = field(default_factory=dict)
    event_id: str | None = None
    legacy_time: bool = False


@dataclass
class LogFile:
    name: str
    path: str
    size: int
    modified: str


@dataclass
class LogPage:
    entries: list
    total: int
    truncated: bool
    scanned_bytes: int
    legacy_time_entries: int
    unavailable_files: int
    unparsed_lines: int = 0
    def __iter__(self):
        # Compatibility for direct callers that used entries, total = ...
        yield self.entries
        yield self.total


class LogService:
    LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    MAX_SCAN_BYTES = 8 * 1024 * 1024
    FILE_TAIL_BYTES = 1024 * 1024
    MAX_RECORD_BYTES = 64 * 1024
    MAX_ENTRIES = 20000
    LOG_PATTERN = re.compile(r"^(\S+(?: \d{2}:\d{2}:\d{2})?)\s*\|\s*(\w+)\s*\|\s*(\S+)\s*\|\s*(.*)$")
    ALLOWED_NAMES = tuple(f"{kind}.log{suffix}" for kind in ("app", "error")
                          for suffix in ("", ".1", ".2", ".3", ".4", ".5"))

    def __init__(self):
        self._log_dir = self._get_log_dir()

    def _get_log_dir(self):
        path = Path(settings.LOGS_DIR)
        if not path.is_absolute(): path = Path(__file__).resolve().parents[2] / path
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def _safe_path(self, filename):
        if filename not in self.ALLOWED_NAMES: raise ValueError("Unsupported log file")
        root = Path(self._log_dir).resolve()
        path = root / filename
        # Refuse links, even if their target currently lies in the approved folder.
        if path.is_symlink() or path.resolve().parent != root: raise ValueError("Unsafe log path")
        return path

    def get_log_files(self):
        files = []
        for name in self.ALLOWED_NAMES:
            try:
                path = self._safe_path(name)
                if not path.is_file(): continue
                stat = path.stat()
                files.append(LogFile(name, str(path), stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()))
            except (OSError, ValueError): continue
        return sorted(files, key=lambda item: (item.modified, item.name), reverse=True)

    @staticmethod
    def time_bound(value):
        if value is None: return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if parsed.tzinfo is None: raise ValueError("查询时间必须带时区")
        try: return parsed.astimezone(timezone.utc)
        except OverflowError: raise ValueError("查询时间超出支持范围") from None

    def _parse_log_line(self, line):
        match = self.LOG_PATTERN.match(line)
        if not match or match[2] not in self.LOG_LEVELS: return None
        try:
            dt = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
        except ValueError: return None
        legacy = dt.tzinfo is None
        # Original logs omit the offset. This explicit compatibility assumption is exposed in the result.
        if legacy: dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        try: timestamp = dt.astimezone(timezone.utc).isoformat()
        except (OverflowError, ValueError): return None
        message = match[4].strip()
        fields = {}; event_id = None
        if " | ctx=" in message:
            body, suffix = message.rsplit(" | ctx=", 1)
            try:
                parsed = json.loads(suffix)
                if isinstance(parsed, dict):
                    fields = clean_fields(parsed)
                    event_id = parsed.get("event_id")
                    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id): event_id = None
                    message = body
            except (ValueError, TypeError, RecursionError): pass
        return LogEntry(timestamp, match[2], match[3],
                        redact(message), redact(line), fields, event_id, legacy)

    def read_log_paginated(self, level=None, keyword=None, start_time=None, end_time=None,
                           page=1, page_size=50, **context_filters):
        if not 1 <= page <= 1000 or not 1 <= page_size <= 500: raise ValueError("分页超出允许范围")
        start = self.time_bound(start_time); end = self.time_bound(end_time)
        if start and end and start > end: raise ValueError("起始时间不能晚于结束时间")
        files = self.get_log_files()
        entries = []; seen = set(); occurrences = Counter()
        truncated = False; scanned = 0; legacy_count = 0; unavailable = 0
        candidate_count = 0
        unparsed = 0
        for file in files:
            remaining = self.MAX_SCAN_BYTES - scanned
            if remaining <= 0 or candidate_count >= self.MAX_ENTRIES:
                truncated = True; break
            try:
                with self._safe_path(file.name).open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    length = min(size, self.FILE_TAIL_BYTES, remaining)
                    stream.seek(size - length)
                    data = stream.read(length)
                scanned += len(data)
                if data and not data.endswith(b"\n"):
                    truncated = True
                    data = data[:data.rfind(b"\n") + 1]
                if size > length:
                    truncated = True
                    # The tail may start midway through an event; discard that incomplete line.
                    split = data.find(b"\n")
                    data = data[split + 1:] if split >= 0 else b""
                for raw in reversed(data.splitlines()):
                    if candidate_count >= self.MAX_ENTRIES:
                        truncated = True; break
                    if len(raw) > self.MAX_RECORD_BYTES:
                        truncated = True; continue
                    entry = self._parse_log_line(raw.decode("utf-8", errors="replace"))
                    if entry is None:
                        if raw.strip(): unparsed += 1
                        continue
                    candidate_count += 1
                    family = file.name.split(".")[0]
                    fingerprint = hashlib.sha256(raw).hexdigest()
                    occurrences[(family, fingerprint)] += 1
                    # Preserve repeated legacy lines within one family; merge app/error mirrors only.
                    identity = ("event", entry.event_id) if entry.event_id else (
                        "legacy", fingerprint, occurrences[(family, fingerprint)])
                    if identity in seen: continue
                    seen.add(identity)
                    legacy_count += int(entry.legacy_time)
                    dt = datetime.fromisoformat(entry.timestamp)
                    if level and entry.level != level.upper(): continue
                    if keyword and keyword.lower() not in entry.message.lower(): continue
                    if start and dt < start or end and dt > end: continue
                    if any(value is not None and str(entry.context.get(key)) != str(value)
                           for key, value in context_filters.items()): continue
                    entries.append(entry)
            except (OSError, ValueError):
                unavailable += 1; truncated = True
        entries.sort(key=lambda entry: (entry.timestamp, entry.event_id or entry.raw_line), reverse=True)
        total = len(entries); offset = (page - 1) * page_size
        return LogPage(entries[offset:offset + page_size], total, truncated, scanned, legacy_count, unavailable, unparsed)

    def get_log_content(self, filename="app.log"):
        # Downloads have a hard bound too; reject oversized files rather than silently exporting a tail.
        path = self._safe_path(filename)
        try:
            with path.open("rb") as stream:
                data = stream.read(self.MAX_SCAN_BYTES + 1)
        except FileNotFoundError: return ""
        if len(data) > self.MAX_SCAN_BYTES: raise ValueError("文件超过8MiB下载上限，请在部署环境归档")
        return redact(data.decode("utf-8", errors="replace"))

    def clear_log(self, filename="app.log"):
        path = self._safe_path(filename)
        if not path.exists(): return True
        try:
            with path.open("w", encoding="utf-8") as stream: stream.write("")
            return True
        except OSError: return False

    @property
    def log_dir(self): return self._log_dir


_log_service = None

def get_log_service():
    global _log_service
    if _log_service is None: _log_service = LogService()
    return _log_service
