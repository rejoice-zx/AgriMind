"""Bind a short-lived confirmation to server-generated task facts and its owner."""
import hashlib
import hmac
import json
import time
from app.config import settings


def signature(item, field_id, user_id, issued):
    # Execution state is derived from saved receipts, not from client-supplied display fields.
    payload = {"item": item.model_dump(exclude={"proof", "execution_status", "execution_record_id"}), "field_id": field_id, "user_id": user_id, "issued": issued}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(settings.JWT_SECRET_KEY.encode(), b"agri-plan-confirmation-v1:" + canonical, hashlib.sha256).hexdigest()


def issue_confirmation(item, field_id, user_id):
    issued = int(time.time())
    return f"{issued}.{signature(item, field_id, user_id, issued)}"


def verify_confirmation(item, field_id, user_id):
    try:
        issued_text, digest = (item.proof or "").split(".", 1)
        issued = int(issued_text)
        return 0 <= time.time() - issued <= 900 and hmac.compare_digest(digest, signature(item, field_id, user_id, issued))
    except (TypeError, ValueError):
        return False


def execution_fingerprint(item, field_id):
    payload = [field_id, item.crop_planting_id, item.date, item.record_type, item.action]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
