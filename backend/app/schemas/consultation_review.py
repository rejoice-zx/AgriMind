"""Human attestations are explicit records, not a machine verification of labels."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, AwareDatetime, field_validator


class LabelReviewEvidence(BaseModel):
    record_id: int | None = Field(None, ge=1)
    product_name: str = Field(..., min_length=1, max_length=200)
    registration_number: str = Field(..., min_length=1, max_length=100)
    applicable_crop: str = Field(..., min_length=1, max_length=50)
    label_reference: str = Field(..., min_length=5, max_length=1000)
    usage_scope: str = Field(..., min_length=5, max_length=2000)
    phi_days: int = Field(..., ge=0, le=3650, strict=True)

    @field_validator('product_name', 'registration_number', 'applicable_crop', 'label_reference', 'usage_scope', mode='before')
    @classmethod
    def meaningful(cls, value):
        return value.strip() if isinstance(value, str) else value


class ReviewChecks(BaseModel):
    registration_checked: bool = False
    crop_and_use_checked: bool = False
    records_complete: bool = False
    phi_checked: bool = False
    site_and_weather_checked: bool = False


class OperationReview(BaseModel):
    purpose: Literal['spray', 'harvest']
    outcome: Literal['needs_information', 'rejected', 'approved'] = 'needs_information'
    crop_planting_id: int | None = Field(None, ge=1)
    context_fingerprint: str = Field(..., pattern='^[0-9a-f]{64}$')
    planned_operation_date: date | None = None
    planned_harvest_date: date | None = None
    valid_until: AwareDatetime | None = None
    evidence_references: str = Field('', max_length=4000)
    review_notes: str = Field('', max_length=4000)
    checks: ReviewChecks = Field(default_factory=ReviewChecks)
    products: list[LabelReviewEvidence] = Field(default_factory=list, max_length=200)
