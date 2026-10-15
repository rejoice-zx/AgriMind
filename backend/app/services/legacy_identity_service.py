"""Minimal ownership compatibility for retired ecommerce conversations.

This module does not activate chat, human-service, product, LLM or statistics services.
The historical conversation ORM remains necessary for ownership compatibility.
Only a visitor identity verified by AuthService may authorize a claim.
"""
from sqlalchemy import update
from app.models.conversation import Conversation


class LegacyIdentityService:
    @staticmethod
    async def migrate_guest_conversations(db, to_customer_id, verified_visitor_id=None,
                                          device_id=None, conversation_ids=None):
        if not verified_visitor_id:
            return 0
        statement = update(Conversation).where(Conversation.customer_id == verified_visitor_id)
        if device_id:
            statement = statement.where(Conversation.temp_device_id == device_id)
        if conversation_ids:
            safe_ids = [value for value in conversation_ids if isinstance(value, str) and value]
            if safe_ids:
                statement = statement.where(Conversation.id.in_(safe_ids))
        result = await db.execute(statement.values(customer_id=to_customer_id))
        await db.commit()
        return result.rowcount or 0
