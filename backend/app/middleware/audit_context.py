"""Pure ASGI context is isolated per task and reset even on disconnect or exceptions."""
import logging
from uuid import uuid4
from app.core.audit_logging import request_context, audit
from app.middleware.agri_performance import agricultural_metric


class AuditContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        context = request_context.set({"request_id": request_id})
        response_status = None
        finished = False
        async def observed_send(message):
            nonlocal response_status, finished
            if message["type"] == "http.response.start":
                response_status = message["status"]
                message = {**message, "headers": [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"] + [(b"x-request-id", request_id.encode("ascii"))]}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
        try:
            return await self.app(scope, receive, observed_send)
        finally:
            try:
                if agricultural_metric(scope.get("path", "")):
                    audit("http_completed" if finished else "http_incomplete", status_code=response_status)
            except Exception:
                # Observability failure must never change the business response.
                pass
            finally:
                request_context.reset(context)
