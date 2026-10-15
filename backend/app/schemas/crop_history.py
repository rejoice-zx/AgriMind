from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, field_validator
from app.schemas.agriculture import CropPlantingResponse


class CropHistoryPage(BaseModel):
    items: list[CropPlantingResponse]
    next_before_id: Optional[int] = None
    snapshot_id: int


class CropStatusEventResponse(BaseModel):
    id: int
    crop_planting_id: int
    previous_status: str
    new_status: str
    reason: Optional[str] = None
    actor_name: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class CropStatusEventPage(BaseModel):
    items: list[CropStatusEventResponse]
    next_before_id: Optional[int] = None
    snapshot_id: int


class CropReopenRequest(BaseModel):
    request_id: UUID
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def actual_reason(cls, value):
        if not value.strip():
            raise ValueError('请填写恢复批次的原因')
        return value.strip()


class CropReopenResponse(BaseModel):
    crop: CropPlantingResponse
    event_id: int
    replayed: bool
