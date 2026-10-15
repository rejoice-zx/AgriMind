"""Bounded operation outcomes; no payloads, SQL text, URLs or credentials."""
import asyncio
import logging
import time
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

OUTCOMES = frozenset({'success', 'error', 'degraded', 'cancelled', 'rejected', 'skipped'})
MODEL_METRICS = frozenset({'agri_decision_model', 'agri_vision_model', 'embedding_api'})
request_observation = ContextVar('agri_request_observation', default=None)


@dataclass
class RequestObservation:
    service: object
    degraded: bool = False
    active: bool = True


@dataclass
class OperationObservation:
    outcome: str = 'error'


current_operation = ContextVar('agri_current_operation', default=None)


def set_operation_outcome(outcome):
    if outcome not in OUTCOMES:
        raise ValueError('Unknown operation outcome')
    sample = current_operation.get()
    if sample is not None:
        sample.outcome = outcome


def record_operation(name, duration, outcome):
    if outcome not in OUTCOMES:
        raise ValueError('Unknown operation outcome')
    request = request_observation.get()
    if request is not None and request.active and name in MODEL_METRICS and outcome in {'error', 'degraded'}:
        request.degraded = True
    try:
        from app.services.performance_service import get_performance_service
        service = request.service if request is not None and request.active else get_performance_service()
        service.record(name, duration, outcome == 'success', outcome=outcome)
    except Exception:
        logging.getLogger(__name__).warning('操作采样失败；保留业务结果')


@contextmanager
def observe_operation(name, default='error'):
    sample = OperationObservation(default)
    started = time.perf_counter()
    try:
        yield sample
    except BaseException as error:
        sample.outcome = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'error'
        raise
    finally:
        record_operation(name, time.perf_counter() - started, sample.outcome)


def observed_model(name):
    def decorate(method):
        @wraps(method)
        async def wrapped(*args, **kwargs):
            with observe_operation(name, default='degraded') as sample:
                handle = current_operation.set(sample)
                try:
                    return await method(*args, **kwargs)
                finally:
                    current_operation.reset(handle)
        return wrapped
    return decorate


def install_database_observation(engine):
    """Time cursor execution only inside active agricultural request scopes.

    Pool waits, connection opening, commits and application processing are excluded.
    SQL and parameter values are deliberately never inspected or retained.
    """
    from sqlalchemy import event
    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def before(connection, cursor, statement, parameters, context, many):
        request = request_observation.get()
        if request is not None and request.active:
            context._agri_observation = (time.perf_counter(), request)

    def finish(context, outcome):
        sample = getattr(context, '_agri_observation', None) if context is not None else None
        if sample is not None:
            del context._agri_observation
            started, request = sample
            if request.active:
                try:
                    request.service.record('agri_database_statement', time.perf_counter() - started,
                                           outcome == 'success', outcome=outcome)
                except Exception:
                    logging.getLogger(__name__).warning('数据库采样失败；保留执行结果')

    @event.listens_for(engine.sync_engine, 'after_cursor_execute')
    def after(connection, cursor, statement, parameters, context, many):
        finish(context, 'success')

    @event.listens_for(engine.sync_engine, 'handle_error')
    def error(context):
        outcome = 'cancelled' if isinstance(context.original_exception, asyncio.CancelledError) else 'error'
        finish(context.execution_context, outcome)
