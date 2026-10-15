"""Seed historical rows directly; never activate retired business services."""
from uuid import uuid4
from app.models.conversation import Conversation


class LegacyRows:
    @staticmethod
    async def create_conversation(db, **values):
        row = Conversation(id=str(uuid4()), **values)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    async def get_conversation(db, identity):
        return await db.get(Conversation, identity, populate_existing=True)
