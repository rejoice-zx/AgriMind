"""Read a coherent SQLite snapshot from domain records, without legacy counters or writes."""
from datetime import date, datetime, time, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select, func, case
from app.models import (Farm, Field, CropPlanting, FarmingRecord, DiseaseRecord,
    KnowledgeItem, AgriConversation, AgriTurn, AgriConsultation)
from app.schemas.agri_statistics import (AgriOverview, AgriDailyStats, AgriCategories,
    AgriTopQuestion, AgriStatisticsSnapshot)
from app.core.weather_policy import AGRI_TIMEZONE, finite_number
from app.utils.time import utcnow


class AgriStatisticsService:
    @staticmethod
    def date_range(start_date, end_date, days, now):
        if (start_date is None) != (end_date is None):
            raise HTTPException(422, "开始和结束日期需同时提供")
        if start_date is None:
            end_date = now.astimezone(AGRI_TIMEZONE).date()
            start_date = end_date - timedelta(days=days - 1)
        if start_date > end_date or (end_date - start_date).days >= 90:
            raise HTTPException(422, "请选择按顺序排列且不超过90天的日期范围")
        # Clamp the unrepresentable UTC portion of business day 0001-01-01.
        lower = datetime.min.replace(tzinfo=timezone.utc) if start_date == date.min else datetime.combine(start_date, time.min, AGRI_TIMEZONE).astimezone(timezone.utc)
        upper = datetime.combine(end_date, time.max, AGRI_TIMEZONE).astimezone(timezone.utc)
        return start_date, end_date, lower, upper

    @staticmethod
    async def snapshot(db, start_date=None, end_date=None, days=14, top_limit=10):
        now = utcnow()
        start_date, end_date, lower, upper = AgriStatisticsService.date_range(start_date, end_date, days, now)
        # A SAVEPOINT establishes an actual SQLite read transaction even with legacy driver BEGIN behavior.
        async with db.begin_nested():
            return await AgriStatisticsService._read(db, start_date, end_date, lower, upper, now, top_limit)

    @staticmethod
    async def _read(db, start_date, end_date, lower, upper, now, top_limit):
        async def count(model, *conditions):
            if model is FarmingRecord: conditions = (*conditions, FarmingRecord.revoked_at.is_(None))
            return int(await db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

        turn_window = (AgriTurn.created_at >= lower, AgriTurn.created_at <= upper)
        ticket_window = (AgriConsultation.created_at >= lower, AgriConsultation.created_at <= upper)
        turn_counts = dict((await db.execute(select(AgriTurn.status, func.count()).where(*turn_window).group_by(AgriTurn.status))).all())
        ticket_counts = dict((await db.execute(select(AgriConsultation.status, func.count()).where(*ticket_window).group_by(AgriConsultation.status))).all())
        completed = turn_counts.get("completed", 0)
        failed = turn_counts.get("failed", 0)
        pending = turn_counts.get("pending", 0)
        # Extract only timing values. Invalid historical JSON must not break or fabricate latency statistics.
        duration_type = case((func.json_valid(AgriTurn.decision) == 1,
            func.json_type(AgriTurn.decision, "$.total_duration_ms")), else_=None)
        duration = case((duration_type.in_(["integer", "real"]),
            AgriTurn.decision["total_duration_ms"].as_float()), else_=None)
        samples, average = 0, 0.0
        durations = await db.stream_scalars(select(duration).where(*turn_window, AgriTurn.status == "completed"))
        async for raw_value in durations:
            value = finite_number(raw_value, 0)
            if value is not None:
                samples += 1
                average += (value - average) / samples

        overview = AgriOverview(
            total_farms=await count(Farm), total_fields=await count(Field),
            active_crop_plantings=await count(CropPlanting, CropPlanting.status == "active"),
            total_records=await count(FarmingRecord), total_conversations=await count(AgriConversation),
            current_knowledge_items=await count(KnowledgeItem, KnowledgeItem.is_current == 1),
            total_disease_records=await count(DiseaseRecord),
            pending_disease_cases=await count(DiseaseRecord, DiseaseRecord.status.in_(["pending_review", "in_review"])),
            period_turns=sum(turn_counts.values()), completed_turns=completed, failed_turns=failed,
            pending_turns=pending,
            stale_pending_turns=await count(AgriTurn, *turn_window, AgriTurn.status == "pending", AgriTurn.created_at < now - timedelta(minutes=30)),
            completion_rate=completed / (completed + failed) if completed + failed else None,
            avg_response_time_ms=average if samples else None, response_time_sample_count=samples,
            period_consultations=sum(ticket_counts.values()), pending_consultations=ticket_counts.get("pending", 0),
            in_review_consultations=ticket_counts.get("in_review", 0), resolved_consultations=ticket_counts.get("resolved", 0),
        )
        daily = {}
        for index in range((end_date - start_date).days + 1):
            day = start_date + timedelta(days=index)
            daily[day.isoformat()] = AgriDailyStats(date=day)
        def business_day(column):
            return func.date(column, "+8 hours")
        # Turn outcomes are attributed to the question date, using their CURRENT recorded state.
        rows = (await db.execute(select(business_day(AgriTurn.created_at), AgriTurn.status, func.count())
            .where(*turn_window).group_by(business_day(AgriTurn.created_at), AgriTurn.status))).all()
        for day, state, number in rows:
            item = daily.get(day)
            if item:
                item.turns += number
                if state == "completed": item.completed += number
                elif state == "failed": item.failed += number
                elif state == "pending": item.pending += number
        for model, column, target, extra in (
            (AgriConversation, AgriConversation.created_at, "conversations", ()),
            (AgriConsultation, AgriConsultation.created_at, "consultations_created", ()),
            (AgriConsultation, AgriConsultation.updated_at, "consultations_resolved", (AgriConsultation.status == "resolved",)),
        ):
            rows = (await db.execute(select(business_day(column), func.count()).select_from(model)
                .where(column >= lower, column <= upper, *extra).group_by(business_day(column)))).all()
            for day, number in rows:
                if day in daily: setattr(daily[day], target, number)
        rows = (await db.execute(select(FarmingRecord.record_date, func.count()).where(
            FarmingRecord.record_date >= start_date, FarmingRecord.record_date <= end_date, FarmingRecord.revoked_at.is_(None)).group_by(FarmingRecord.record_date))).all()
        for day, number in rows:
            if day.isoformat() in daily: daily[day.isoformat()].farming_records = number

        async def distribution(column, *conditions):
            label = func.coalesce(func.nullif(func.trim(column), ""), "未标记")
            rows = (await db.execute(select(label, func.count()).where(*conditions).group_by(label).order_by(label))).all()
            return {str(key): value for key, value in rows}
        decision_type = case((func.json_valid(AgriTurn.decision) == 1, AgriTurn.decision["decision_type"].as_string()), else_=None)
        categories = AgriCategories(
            knowledge=await distribution(KnowledgeItem.category, KnowledgeItem.is_current == 1),
            active_crops=await distribution(CropPlanting.crop, CropPlanting.status == "active"),
            decisions=await distribution(decision_type, *turn_window, AgriTurn.status == "completed"),
            farming_records=await distribution(FarmingRecord.record_type, FarmingRecord.record_date >= start_date, FarmingRecord.record_date <= end_date, FarmingRecord.revoked_at.is_(None)),
        )
        question = func.trim(AgriTurn.query)
        top_rows = (await db.execute(select(question, func.count()).where(*turn_window, question != "")
            .group_by(question).order_by(func.count().desc(), question.asc()).limit(top_limit))).all()
        return AgriStatisticsSnapshot(generated_at=now, start_date=start_date, end_date=end_date,
            overview=overview, daily=list(daily.values()), categories=categories,
            top_questions=[AgriTopQuestion(question=query, count=number) for query, number in top_rows])

    @staticmethod
    def export_report(snapshot):
        o = snapshot.overview
        rate = "无已结束提问" if o.completion_rate is None else f"{o.completion_rate:.1%}"
        latency = "无有效时长样本" if o.avg_response_time_ms is None else f"{o.avg_response_time_ms:.2f} ms（{o.response_time_sample_count}条样本）"
        lines = ["# 农业业务统计报告", f"生成时间：{snapshot.generated_at.isoformat()}",
            f"日期范围：{snapshot.start_date} 至 {snapshot.end_date}，北京时间",
            "口径：当前留存农业记录；资产为当前全库数量，提问及通用咨询按创建日期筛选。完成率不是诊断正确率。",
            "", f"农场：{o.total_farms}；田块：{o.total_fields}；活跃种植批次：{o.active_crop_plantings}",
            f"农事履历条目：{o.total_records}；农业档案：{o.total_conversations}；标记当前的知识条目：{o.current_knowledge_items}",
            f"期间提问：{o.period_turns}；已完成：{o.completed_turns}；失败：{o.failed_turns}；处理中：{o.pending_turns}（含超时待核对{o.stale_pending_turns}）",
            f"分析完成率（已完成 / 已完成及失败）：{rate}", f"已完成分析平均耗时：{latency}",
            f"期间创建通用咨询：{o.period_consultations}；待接单：{o.pending_consultations}；处理中：{o.in_review_consultations}；已答复：{o.resolved_consultations}",
            f"独立图像病例：{o.total_disease_records}；当前待复核：{o.pending_disease_cases}", "",
            "|日期|提问|当前已完成|当前失败|当前处理中|农事履历|咨询新建|当日答复|", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for item in snapshot.daily:
            lines.append(f"|{item.date}|{item.turns}|{item.completed}|{item.failed}|{item.pending}|{item.farming_records}|{item.consultations_created}|{item.consultations_resolved}|")
        # Do not export raw user questions or arbitrary labels into Markdown syntax.
        return "\n".join(lines)
