"""No anonymous files, arbitrary paths, or permissions manufactured through a new reference."""
import os
import re
from pathlib import Path
from sqlalchemy import select
from fastapi import HTTPException
from app.config import settings
from app.models import User, Farm, Field, DiseaseRecord, AgriConversation, AgriTurn, AgriConsultation
from app.models.diagnosis_media import DiagnosisMedia
from app.core.roles import is_agri_staff

NEW_PREFIX = "/api/diagnosis/media/"
OLD_PREFIX = "/static/uploads/diagnosis/"


class DiagnosisMediaService:
    @staticmethod
    def parse(url):
        if not isinstance(url, str): raise HTTPException(404, "图片不可用或无权访问")
        if url.startswith(NEW_PREFIX):
            name = url[len(NEW_PREFIX):]
            if not re.fullmatch(r"[0-9a-f]{32}\.(?:jpeg|jpg|png|webp)", name):
                raise HTTPException(404, "图片不可用或无权访问")
            return name, False
        if url.startswith(OLD_PREFIX):
            name = url[len(OLD_PREFIX):]
            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\.(?:jpeg|jpg|png|webp)", name):
                raise HTTPException(404, "图片不可用或无权访问")
            return name, True
        raise HTTPException(404, "图片不可用或无权访问，请重新上传现场照片")

    @staticmethod
    def path(name, legacy=False):
        root = Path(settings.DATA_DIR) / ("uploads" if legacy else "private_uploads") / "diagnosis"
        resolved = (root / name).resolve()
        try:
            safe = os.path.commonpath([str(root.resolve()), str(resolved)]) == str(root.resolve())
        except ValueError:
            safe = False
        if not safe: raise HTTPException(404, "图片不可用或无权访问")
        return resolved

    @staticmethod
    async def live_user(db, user):
        current = await db.scalar(select(User).where(User.id == user.id, User.auth_subject == user.auth_subject,
            User.is_active.is_(True)).execution_options(populate_existing=True))
        if current is None: raise HTTPException(401, "登录状态已变化，请重新登录")
        return current

    @classmethod
    async def access(cls, db, url, user, *, for_link=False, field_id=None):
        name, legacy = cls.parse(url)
        current = await cls.live_user(db, user)
        media = await db.get(DiagnosisMedia, name) if not legacy else None
        if not legacy and media is None: raise HTTPException(404, "图片不可用或无权访问")
        allowed = bool(media and media.owner_id == current.id)
        if not allowed:
            cases = select(DiseaseRecord.id).join(Field, Field.id == DiseaseRecord.field_id).join(Farm, Farm.id == Field.farm_id).where(DiseaseRecord.image_url == url)
            if for_link: cases = cases.where(DiseaseRecord.field_id == field_id)
            if not is_agri_staff(current): cases = cases.where(Farm.owner_id == current.id)
            allowed = await db.scalar(cases.limit(1)) is not None
        if not allowed:
            turns = select(AgriTurn.id).join(AgriConversation, AgriConversation.id == AgriTurn.conversation_id).where(AgriTurn.image_url == url)
            if for_link: turns = turns.where(AgriConversation.field_id == field_id)
            if is_agri_staff(current):
                turns = turns.join(AgriConsultation, AgriConsultation.turn_id == AgriTurn.id)
            else:
                turns = turns.where(AgriConversation.owner_id == current.id)
            allowed = await db.scalar(turns.limit(1)) is not None
        if not allowed: raise HTTPException(404, "图片不可用或无权访问")
        path = cls.path(name, legacy)
        if not path.is_file(): raise HTTPException(404, "图片文件不存在，请重新上传现场照片")
        mime = media.content_type if media else "image/jpeg" if path.suffix in {".jpg", ".jpeg"} else f"image/{path.suffix[1:]}"
        return path, mime
