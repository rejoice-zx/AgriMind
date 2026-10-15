"""Server correlation and bounded, allowlisted audit fields; never log business payloads."""
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from uuid import uuid4

request_context = ContextVar("agri_request_context", default=None)
FIELDS = {"request_id", "business_request_id", "owner_id", "field_id", "conversation_id", "turn_id",
          "consultation_id", "record_id", "expert_case_id", "actor_id", "event", "status_code", "error_type",
          "probe_stage", "probe_code", "asset_type", "asset_id", "archive_status"}
_LABEL = r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|authorization|password|passwd|access[_-]?token|refresh[_-]?token|token|secret|credentials?|proof|signature)(?:[_-][a-z0-9]+)*"
_VALUE = r'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;&}\]\r\n]+)'''
_LABELED = re.compile(r'''(?i)(["']?\b''' + _LABEL + r'''["']?\s*[:=]\s*)''' + _VALUE)
_BEARER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+")
_USERINFO = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.I)


def redact(value):
    value = _BEARER.sub(lambda match: match[0].split()[0] + " ***", str(value))
    value = _USERINFO.sub(r"\1***@", value)
    return _LABELED.sub(lambda match: match[1] + "***", value)


def clean_fields(values):
    return {key: value for key, value in values.items() if key in FIELDS and value is not None
            and isinstance(value, (str, int)) and len(str(value)) <= 100
            and re.fullmatch(r"[A-Za-z0-9_.:-]+", str(value))}


def bind_audit(**values):
    # Only enrich a live HTTP context; direct service calls must not leak context to later callers.
    current = request_context.get()
    if current is not None:
        request_context.set({**current, **clean_fields(values)})


def audit(event, **values):
    try:
        fields = clean_fields({**(request_context.get() or {}), **values, "event": event})
        logging.getLogger("app.audit").info("agricultural_business_event", extra={"audit_fields": fields})
    except Exception:
        pass  # An unavailable log sink must not undo or misreport committed business data.


class AuditFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, tz_name="Asia/Shanghai"):
        super().__init__(fmt or "%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt)
        self.set_timezone(tz_name)

    def set_timezone(self, tz_name):
        try:
            from zoneinfo import ZoneInfo
            self._tz = ZoneInfo(tz_name)
        except Exception:
            self._tz = timezone(timedelta(hours=8)) if tz_name == "Asia/Shanghai" else timezone.utc

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self._tz).isoformat(timespec="microseconds")

    def format(self, record):
        # Same LogRecord is delivered to app/error handlers; keep the event identity identical.
        if not hasattr(record, "agri_event_id"):
            record.agri_event_id = uuid4().hex
            record.agri_context = clean_fields({**(request_context.get() or {}),
                                               **getattr(record, "audit_fields", {})})
        fields = {**record.agri_context, "event_id": record.agri_event_id}
        line = redact(super().format(record)).replace("\r", "\\r").replace("\n", "\\n")
        return line + " | ctx=" + json.dumps(fields, ensure_ascii=True, separators=(",", ":"))
