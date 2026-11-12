"""Phase-3 harvest-safety and crop lifecycle regressions (BUG-015, BUG-031).

Synthetic synchronous SQLite through the existing adapter; no production data.
"""
from datetime import date
import pytest
from fastapi import HTTPException
from app.models import CropPlanting, FarmingRecord
from app.schemas.agriculture import CropPlantingUpdate
from app.services.farm_service import FarmService
from app.services.harvest_safety_service import harvest_safety
from app.core.pesticide_standards import find_all_banned_pesticides
from tests.test_archive_metadata_smoke import synthetic  # noqa: F401  (fixture)
from tests.test_farming_submission_smoke import finish_immediate


def _crop(session, field, status="active"):
    crop = CropPlanting(field_id=field.id, crop="番茄", planting_date=date(2026, 9, 20), status=status)
    session.add(crop); session.commit()
    return crop


def test_planting_date_locked_once_spray_records_depend_on_it(synthetic):
    session, db, owner, *_rest, farm, field = synthetic
    crop = _crop(session, field)
    session.add(FarmingRecord(field_id=field.id, crop_planting_id=None, record_type="spraying",
                              record_date=date(2026, 10, 1), input_name="测试药剂", safety_interval_days=21))
    session.commit()
    with pytest.raises(HTTPException) as blocked:
        finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(planting_date=date(2026, 10, 5)), owner.id))
    assert blocked.value.status_code == 409
    session.refresh(crop)
    assert crop.planting_date == date(2026, 9, 20)
    # Other fields remain editable on an active batch.
    updated = finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(growth_stage="开花坐果期"), owner.id))
    assert updated.growth_stage == "开花坐果期"


def test_planting_date_editable_without_spray_history(synthetic):
    session, db, owner, *_rest, farm, field = synthetic
    crop = _crop(session, field)
    session.add(FarmingRecord(field_id=field.id, crop_planting_id=crop.id, record_type="irrigation", record_date=date(2026, 10, 1)))
    session.commit()
    updated = finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(planting_date=date(2026, 9, 25)), owner.id))
    assert updated.planting_date == date(2026, 9, 25)


def test_ended_batch_profile_is_read_only(synthetic):
    session, db, owner, *_rest, farm, field = synthetic
    crop = _crop(session, field, status="harvested")
    with pytest.raises(HTTPException) as blocked:
        finish_immediate(FarmService.update_crop_planting(db, crop.id, CropPlantingUpdate(variety="改写历史"), owner.id))
    assert blocked.value.status_code == 409


def test_banned_names_in_any_record_type_or_description_block_harvest(synthetic):
    session, db, owner, *_rest, farm, field = synthetic
    crop = _crop(session, field)
    session.add(FarmingRecord(field_id=field.id, crop_planting_id=crop.id, record_type="other",
                              record_date=date(2026, 10, 1), description="补记：当天顺带用了毒死蜱"))
    session.commit()
    result = finish_immediate(harvest_safety(db, field.id, crop.id))
    assert result["status"] == "blocked" and result["can_approve"] is False
    assert any("毒死蜱" in reason for reason in result["reasons"])


def test_banned_list_includes_names_checked_against_2024_catalogue():
    assert find_all_banned_pesticides("氯唑磷颗粒剂") == ["氯唑磷"]
    assert find_all_banned_pesticides("灭线磷") == ["灭线磷"]
