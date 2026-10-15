"""Single-worker, bounded analysis tasks survive an HTTP waiter's cancellation."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException


class AnalysisCancelled(Exception):
    pass


@dataclass
class RunningAnalysis:
    owner: str
    payload: dict
    cancelled: asyncio.Event
    task: asyncio.Task | None = None


_current = ContextVar('running_analysis', default=None)
_running: dict[tuple[str, str], RunningAnalysis] = {}
MAX_ACTIVE = 16
MAX_PER_OWNER = 2


async def continue_analysis(kind, request_id, owner, payload, work):
    """Hold a strong reference and shield only transport waiting, never the deadline."""
    key = (kind, str(request_id))
    job = _running.get(key)
    if job:
        if job.owner != owner or job.payload != payload:
            raise HTTPException(409, '请求编号已被使用，请查询原回执')
    else:
        if len(_running) >= MAX_ACTIVE or sum(job.owner == owner for job in _running.values()) >= MAX_PER_OWNER:
            raise HTTPException(429, '仍有分析正在处理，请先查询原回执或主动取消')
        job = RunningAnalysis(owner, payload, asyncio.Event())
        async def execute():
            marker = _current.set(job)
            try:
                return await work()
            finally:
                _current.reset(marker)
        job.task = asyncio.create_task(execute(), name='agri-' + kind)
        _running[key] = job
        def finished(task):
            if _running.get(key) is job:
                del _running[key]
            # A disconnected waiter must not leave an unobserved task exception.
            if not task.cancelled():
                task.exception()
        job.task.add_done_callback(finished)
    return await asyncio.shield(job.task)


def signal_cancel(kind, request_id, owner):
    """Only called after the caller has authorized and committed a pending receipt."""
    job = _running.get((kind, str(request_id)))
    if job and job.owner == owner:
        job.cancelled.set()


async def cancellable_analysis(work):
    job = _current.get()
    if job is None:
        return await work
    if job.cancelled.is_set():
        work.close()
        raise AnalysisCancelled('用户主动取消本次分析')
    task = asyncio.create_task(work)
    cancellation = asyncio.create_task(job.cancelled.wait())
    try:
        await asyncio.wait((task, cancellation), return_when=asyncio.FIRST_COMPLETED)
        if job.cancelled.is_set():
            raise AnalysisCancelled('用户主动取消本次分析')
        return await task
    finally:
        for pending in (task, cancellation):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, cancellation, return_exceptions=True)


async def run_owned_analysis(db, request, user, kind, handler):
    """The worker owns its DB session; closing the HTTP dependency cannot close it."""
    from uuid import uuid4
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncConnection
    from app.models.user import User
    request = request.model_copy(update={'request_id': request.request_id or uuid4()})
    owner_id, owner = user.id, user.auth_subject
    bind = db.bind.engine if isinstance(db.bind, AsyncConnection) else db.bind
    sessions = async_sessionmaker(bind, expire_on_commit=False, autoflush=False)
    # Authentication reads must not retain the HTTP session/transaction for the
    # full model wait; all business work and fresh authorization use the worker.
    await db.close()
    async def work():
        async with sessions() as session:
            current = await session.get(User, owner_id)
            if current is None or not current.is_active or current.auth_subject != owner:
                raise HTTPException(401, '账号状态已变化，请重新登录')
            return await handler(session, request, current)
    return await continue_analysis(kind, request.request_id, owner,
        request.model_dump(mode='json', exclude={'request_id'}), work)
