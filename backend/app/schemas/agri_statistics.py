"""Agricultural record counts; completion is not model or diagnosis accuracy."""
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel


class AgriOverview(BaseModel):
    total_farms: int
    total_fields: int
    active_crop_plantings: int
    total_records: int
    total_conversations: int
    current_knowledge_items: int
    total_disease_records: int
    pending_disease_cases: int
    period_turns: int
    completed_turns: int
    failed_turns: int
    pending_turns: int
    stale_pending_turns: int
    completion_rate: Optional[float]
    avg_response_time_ms: Optional[float]
    response_time_sample_count: int
    period_consultations: int
    pending_consultations: int
    in_review_consultations: int
    resolved_consultations: int


class AgriDailyStats(BaseModel):
    date: date
    turns: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0
    conversations: int = 0
    farming_records: int = 0
    consultations_created: int = 0
    consultations_resolved: int = 0


class AgriCategories(BaseModel):
    knowledge: dict[str, int]
    active_crops: dict[str, int]
    decisions: dict[str, int]
    farming_records: dict[str, int]


class AgriTopQuestion(BaseModel):
    question: str
    count: int


class AgriStatisticsSnapshot(BaseModel):
    schema_version: str = "agriculture-v1"
    scope: str = "current_agricultural_records"
    timezone: str = "Asia/Shanghai"
    generated_at: datetime
    start_date: date
    end_date: date
    overview: AgriOverview
    daily: list[AgriDailyStats]
    categories: AgriCategories
    top_questions: list[AgriTopQuestion]
