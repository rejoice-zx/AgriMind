"""Bounded agricultural HTTP telemetry; never stores paths, questions or credentials."""
import time
import asyncio
from app.core.operation_observation import RequestObservation, request_observation
import logging
from app.services.performance_service import get_performance_service


def agricultural_metric(path):
    path = path.rstrip("/")
    if path == "/api/agent/decide": return "agri_decision_request"
    if path == "/api/agent/tools/execute": return "agri_tool_request"
    for prefix, metric in (
        ("/api/agent/conversations", "agri_archive_request"),
        ("/api/agri-consultations", "agri_consultation_request"), ("/api/agent/turns", "agri_consultation_request"),
        ("/api/agri/plan", "agri_plan_request"), ("/api/diagnosis", "agri_diagnosis_request"),
        ("/api/expert/tickets", "agri_expert_case_request"), ("/api/farms", "agri_field_request"),
        ("/api/fields", "agri_field_request"), ("/api/farming-records", "agri_field_request"),
    ):
        if path == prefix or path.startswith(prefix + "/"): return metric
    return None


class AgriPerformanceMiddleware:
    def __init__(self, app, service=None):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        metric = agricultural_metric(scope.get("path", "")) if scope["type"] == "http" else None
        if metric is None:
            return await self.app(scope, receive, send)
        service = self.service or get_performance_service()
        observation = RequestObservation(service)
        handle = request_observation.set(observation)
        raised = None
        started = time.perf_counter()
        status = None
        body_finished = False
        async def observed_send(message):
            nonlocal status, body_finished
            if message["type"] == "http.response.start": status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False): body_finished = True
        try:
            await self.app(scope, receive, observed_send)
        except BaseException as error:
            raised = error
            raise
        finally:
            observation.active = False
            request_observation.reset(handle)
            complete = body_finished and status is not None and 200 <= status < 300
            outcome = ("degraded" if observation.degraded else "success") if complete else (
                "cancelled" if isinstance(raised, asyncio.CancelledError) else
                "rejected" if raised is None and body_finished and status is not None and 400 <= status < 500 else "error")
            try:
                service.record(metric, time.perf_counter() - started,
                    complete, {"status_code": status}, outcome=outcome)
            except Exception:
                logging.getLogger(__name__).warning("农业请求性能记录失败；业务响应保持原结果")
