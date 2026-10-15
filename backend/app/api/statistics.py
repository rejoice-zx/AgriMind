"""Administrator-only agricultural statistics, derived from domain records."""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.api.dependencies import require_admin
from app.models.user import User
from app.schemas.agri_statistics import AgriStatisticsSnapshot
from app.services.agri_statistics_service import AgriStatisticsService

router = APIRouter(prefix="/api/statistics", tags=["农业业务统计"])


@router.get("/snapshot", response_model=AgriStatisticsSnapshot)
async def get_statistics_snapshot(start_date: date | None = None, end_date: date | None = None,
    days: int = Query(14, ge=1, le=90), top_limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_admin())):
    return await AgriStatisticsService.snapshot(db, start_date, end_date, days, top_limit)


@router.get("/export")
async def export_statistics_report(start_date: date | None = None, end_date: date | None = None,
    days: int = Query(14, ge=1, le=90), db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin())):
    snapshot = await AgriStatisticsService.snapshot(db, start_date, end_date, days)
    return {"content": AgriStatisticsService.export_report(snapshot), "generated_at": snapshot.generated_at.isoformat()}


async def retired_statistics(user: User = Depends(require_admin())):
    raise HTTPException(410, "旧电商统计接口已下线，请使用农业统计 snapshot；本操作不会删除农业业务数据")


for path in ("/overview", "/daily", "/categories", "/top-questions"):
    router.add_api_route(path, retired_statistics, methods=["GET"], include_in_schema=False)
router.add_api_route("/data/delete", retired_statistics, methods=["POST"], include_in_schema=False)
