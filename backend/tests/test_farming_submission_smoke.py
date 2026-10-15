"""Main submission flow with a synchronous synthetic DB adapter; no model/network/real DB."""
from datetime import date
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.database import Base
from app.models import User, Farm, Field, FarmingRecord
from app.schemas.agriculture import FarmingRecordCreate
from app.services.farming_record_service import FarmingRecordService


class AsyncAdapter:
    def __init__(self, session): self.session = session
    async def execute(self, query): return self.session.execute(query)
    async def scalar(self, query): return self.session.scalar(query)
    async def scalars(self, query): return self.session.scalars(query)
    async def get(self, model, key, **kwargs): return self.session.get(model, key, **kwargs)
    def add(self, row): self.session.add(row)
    async def commit(self): self.session.commit()
    async def rollback(self): self.session.rollback()
    async def refresh(self, row): self.session.refresh(row)
    async def flush(self): self.session.flush()
    async def close(self): self.session.close()


def finish_immediate(coroutine):
    """The adapter performs synchronous SQL; no event-loop/async transport is under test."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Synthetic DB adapter unexpectedly yielded asynchronous I/O')


def test_lost_response_replays_one_record_and_changed_content_cannot_reuse_id():
    engine = create_engine('sqlite:///:memory:')
    try:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            actor = User(username='synthetic-submission-owner', password_hash='synthetic', role='customer')
            session.add(actor); session.flush()
            farm = Farm(owner_id=actor.id, name='synthetic')
            session.add(farm); session.flush()
            field = Field(farm_id=farm.id, name='synthetic')
            session.add(field); session.commit()
            adapter = AsyncAdapter(session)
            payload = FarmingRecordCreate(request_id=uuid4(), record_type='irrigation', record_date=date(2026, 10, 14), description='synthetic actual fact')
            first = finish_immediate(FarmingRecordService.create_farming_record(adapter, field.id, payload, actor.id))
            replay = finish_immediate(FarmingRecordService.create_farming_record(adapter, field.id, payload, actor.id))
            assert first.id == replay.id
            assert session.scalar(select(func.count()).select_from(FarmingRecord)) == 1
            with pytest.raises(HTTPException) as changed:
                finish_immediate(FarmingRecordService.create_farming_record(adapter, field.id, payload.model_copy(update={'description': 'changed'}), actor.id))
            assert changed.value.status_code == 409
            assert session.scalar(select(func.count()).select_from(FarmingRecord)) == 1
            from app.utils.time import utcnow
            farm.archived_at = utcnow(); session.commit()
            recovered = finish_immediate(FarmingRecordService.create_farming_record(adapter, field.id, payload, actor.id))
            assert recovered.id == first.id and recovered.replayed
            with pytest.raises(HTTPException) as archived_write:
                finish_immediate(FarmingRecordService.create_farming_record(adapter, field.id, payload.model_copy(update={'request_id': uuid4()}), actor.id))
            assert archived_write.value.status_code == 409
    finally:
        engine.dispose()
