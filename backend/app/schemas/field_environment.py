from datetime import timezone
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field, AwareDatetime, model_validator
from app.utils.time import utcnow

FacilityType = Literal['unknown', 'open_field', 'greenhouse', 'tunnel', 'other']


class EnvironmentObservationCreate(BaseModel):
    request_id: UUID
    facility_type: FacilityType
    measurement_scope: Literal['onsite_outdoor', 'inside_facility']
    observed_at: AwareDatetime
    temperature: Optional[float] = Field(None, ge=-80, le=80, allow_inf_nan=False)
    humidity: Optional[float] = Field(None, ge=0, le=100, allow_inf_nan=False)
    ventilation: Literal['unknown', 'open', 'closed', 'partial', 'not_applicable'] = 'unknown'
    covering: Literal['unknown', 'covered', 'uncovered', 'partial', 'not_applicable'] = 'unknown'
    notes: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode='after')
    def actual_observation(self):
        self.observed_at = self.observed_at.astimezone(timezone.utc)
        if self.observed_at > utcnow():
            raise ValueError('观测时间不能在未来')
        if self.temperature is None and self.humidity is None and self.ventilation == 'unknown' and self.covering == 'unknown':
            raise ValueError('请至少填写一项实测温湿度或已核实的通风/覆盖状态')
        return self


class FieldEnvironmentResponse(BaseModel):
    facility_type: FacilityType
    facility_label: str
    outdoor_weather_applicable: bool
    observation_status: Literal['missing', 'context_changed', 'scope_mismatch', 'future', 'stale', 'recent']
    observation_max_age_minutes: int
    latest_observation: Optional[dict] = None
    notice: str
    can_approve_operation: bool = False


class EnvironmentSubmissionResponse(BaseModel):
    observation_id: int
    replayed: bool
    environment: FieldEnvironmentResponse


class EnvironmentHistoryPage(BaseModel):
    items: list[dict]
    next_before_id: Optional[int] = None
    snapshot_id: int
