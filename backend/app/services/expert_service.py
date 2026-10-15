# -*- coding: utf-8 -*-
"""
AgriMind 专家工作台与多模态诊断服务 (ExpertWorkbenchService)

核心功能:
1. 视觉多模态图像诊断 (Visual Diagnosis):
   - 解析病叶图像表型、病斑特征与危害等级
   - 低置信度 (<0.70) 自动触发专家复核工单流转
2. 专家工作台工单流转 (Ticket Lifecycle):
   - pending_review (待接单) -> in_review (审阅中) -> expert_reviewed (已确诊)
   - 提供已录入田块档案、最新已存气象和农事记录，不补造现场环境
3. Human-in-the-Loop 知识生产闭环 (Knowledge Feedback Loop):
   - 专家确诊出具处方后，自动归档沉淀为 ExpertCase
   - 专家明确勾选时将填写内容保存为 L4 专家案例；不自动重建向量索引
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status as http_status
from sqlalchemy import func, select, insert, literal, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crop import CropPlanting
from app.models.disease_record import DiseaseRecord
from app.models.farm import Farm
from app.models.farming_record import FarmingRecord
from app.models.field import Field
from app.models.weather import WeatherSnapshot
from app.models.user import User
from app.schemas.expert import (
    ExpertResolveRequest,
    ExpertResolveResponse,
    ExpertTicketDetail,
    ExpertTicketItem,
    VisualDiagnosisRequest,
    VisualDiagnosisResponse,
)
from app.services.agent_service import AgriTools
from app.services.farm_service import FarmService
from app.services.disease_review_service import DiseaseReviewService
from app.models.disease_review import DiseaseReview
from app.core.weather_policy import finite_number
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class ExpertWorkbenchService:
    """农技专家工作台与多模态闭环服务"""

    @classmethod
    async def visual_diagnose(
        cls,
        db: AsyncSession,
        request: VisualDiagnosisRequest,
        current_user: Optional[User] = None,
        commit: bool = True,
    ) -> VisualDiagnosisResponse:
        """
        多模态图像初诊入口：
        1. 校验关联田块权限，再进行视觉特征识别与置信度打分
        2. 根据置信度触发状态流转（按模型评分与复核标记分流，不把模型评分当作正确率）
        3. 关联合法田块且图片未被明确拒绝时保存 DiseaseRecord，否则仅返回诊断
        """
        if current_user is not None and not getattr(current_user, "is_active", False):
            raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="用户已被禁用")
        crop_name = (request.crop or "").strip() or "未提供"
        field_id = request.field_id

        # 资源上下文必须在任何作物读取、模型调用或持久化之前校验。
        # 未选田块允许单次诊断，但不能借用其他农户田块保存结果。
        active_planting_id = None
        field_binding = None
        if field_id is not None:
            if current_user is None:
                raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="关联田块诊断需要登录用户")
            if field_id <= 0:
                raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="地块不存在")
            field = await FarmService.ensure_field_writable(db, field_id, current_user)
            owner_id = await db.scalar(select(Farm.owner_id).where(Farm.id == field.farm_id))
            field_binding = (field.farm_id, field.created_at, owner_id, current_user.id, current_user.auth_subject)
            # 查找该田块下的活跃种植批次
            stmt = select(CropPlanting).where(
                CropPlanting.field_id == field_id,
                CropPlanting.status == "active"
            )
            if request.crop_planting_id is not None:
                stmt = stmt.where(CropPlanting.id == request.crop_planting_id)
            candidates = list((await db.scalars(stmt.limit(2))).all())
            if request.crop_planting_id is not None and not candidates:
                raise HTTPException(422, '指定批次不属于本田块或已结束，请重新选择')
            if len(candidates) > 1:
                raise HTTPException(422, '此田块有多个在种批次，请明确选择本次诊断的作物批次')
            cp = candidates[0] if candidates else None
            if cp:
                active_planting_id = cp.id
                crop_name = cp.crop

        # 1. 调度视觉诊断代理工具
        diag = await AgriTools.diagnose_crop_image(
            image_url=request.image_url,
            crop=crop_name,
            symptoms_hint=request.symptoms_hint,
            db=db,
        )

        if not isinstance(diag, dict):
            raise HTTPException(503, "视觉服务未返回有效结构，本次未保存病害记录")
        if diag.get("diagnosis_status") == "failed":
            return VisualDiagnosisResponse(
                image_url=request.image_url, crop=crop_name,
                diagnosis_status="failed", error_code=diag.get("error_code", "vision_error"),
                error_message=diag["description"], suspected_disease="本次图像诊断未完成",
                confidence=None, features_detected=[], severity="unknown", need_expert=True,
                advice=diag["description"] + " 本次未生成病害记录或专家工单。",
            )
        suspected_raw = diag.get("suspected_disease")
        suspected = suspected_raw.strip() if isinstance(suspected_raw, str) and suspected_raw.strip() else "未知病害"
        confidence = finite_number(diag.get("confidence"), 0, 1)
        score_label = f"{confidence:.0%}" if confidence is not None else "未提供有效评分"
        severity_raw = diag.get("severity")
        severity = severity_raw if isinstance(severity_raw, str) and severity_raw in {"low", "medium", "high", "severe"} else "unknown"
        features = [value for value in diag.get("features_detected", []) if isinstance(value, str)] if isinstance(diag.get("features_detected"), list) else []
        from app.core.confidence_policy import VISION_EXPERT_GATE
        # Older adapters have no explicit gate classification; retain their conservative contract.
        vision_requires_expert = diag.get("mandatory_review", bool(diag.get("need_expert", False))) is not False or confidence is None
        need_expert = vision_requires_expert or bool(diag.get("need_expert", False)) or confidence < VISION_EXPERT_GATE
        # The switch controls confidence-only automatic dispatch, never the
        # authoritative visual safety gate. Preserve the diagnosis and advice.
        dispatch_ticket = need_expert and (request.auto_create_ticket or vision_requires_expert)

        # 2. 状态机分流
        status = "pending_review" if dispatch_ticket else "preliminary"

        # 现有视觉工具明确拒绝图片时，返回重拍建议，不能写入虚假病害。
        refusal_labels = {"未提供病叶图像", "未能获取合法图片文件", "图片解析失败"}
        non_plant_markers = (
            "非真实植物", "非真实植株", "非真实叶片", "人工绘制", "非植物图像",
            "简笔画", "几何图形", "人工合成图", "电脑绘图", "无植物主体",
        )
        image_rejected = suspected in refusal_labels or any(
            marker in str(value)
            for value in [suspected, *features]
            for marker in non_plant_markers
        )

        # Complete retrieval before persistence: a failed enrichment must not leave an unreturned case.
        standard_treatment = None
        if not image_rejected and not need_expert:
            k_res = await AgriTools.search_agri_knowledge(db=db,
                query=f"{crop_name}{suspected}相关防治资料", crop=crop_name, top_k=1)
            if isinstance(k_res, dict) and k_res.get("items"):
                answer = k_res["items"][0].get("answer")
                standard_treatment = answer if isinstance(answer, str) else None

        disease_record = None
        if field_id is not None and not image_rejected:
            from app.core.roles import AGRI_VALUES
            farm_id, field_created, owner_id, actor_id, actor_subject = field_binding
            valid_scope = select(Field.id).join(Farm, Farm.id == Field.farm_id).join(User, User.id == actor_id).where(
                Field.id == field_id, Field.farm_id == farm_id, Field.created_at == field_created,
                Field.archived_at.is_(None), Farm.archived_at.is_(None),
                Farm.owner_id == owner_id, User.auth_subject == actor_subject, User.is_active.is_(True),
                or_(User.role.in_(AGRI_VALUES), Farm.owner_id == User.id)).exists()
            guards = [valid_scope]
            if active_planting_id is not None:
                guards.append(select(CropPlanting.id).where(CropPlanting.id == active_planting_id,
                    CropPlanting.field_id == field_id, CropPlanting.status == 'active').exists())
            values = dict(field_id=field_id, crop_planting_id=active_planting_id, disease_name=suspected,
                symptoms=request.symptoms_hint or (diag.get("description") if isinstance(diag.get("description"), str) else None),
                severity=severity, diagnosis_method="ai_vision", diagnosis_confidence=confidence,
                image_url=request.image_url, status=status, created_at=utcnow(), updated_at=utcnow())
            try:
                statement = insert(DiseaseRecord).from_select(list(values),
                    select(*[literal(value, type_=DiseaseRecord.__table__.c[key].type) for key, value in values.items()]).where(*guards)).returning(DiseaseRecord.id)
                record_id = (await db.execute(statement)).scalar_one_or_none()
                if record_id is None:
                    raise HTTPException(409, "诊断期间账号、田块归属或种植档案已变化，本次未保存，请刷新核对")
                db.add(DiseaseReview(disease_record_id=record_id, initial_crop=crop_name, image_features=features))
                if commit: await db.commit()
                else: await db.flush()
                disease_record = await db.get(DiseaseRecord, record_id, populate_existing=True)
            except BaseException:
                await db.rollback()
                raise

        if image_rejected:
            advice = "图片未通过有效植物照片校验，请重新上传清晰的真实病叶照片。本次未保存病害记录，也未创建专家复核工单。"
        elif not need_expert:
            advice = f"模型初步提示疑似【{suspected}】，模型评分{score_label}；该评分不是经校准的诊断正确率。请结合现场情况与有效投入品标签核对，检索内容不自动等同于国家标准。"
        elif disease_record is not None and dispatch_ticket:
            advice = f"模型初诊需复核（评分：{score_label}），已自动派发【农技专家工作台复核工单 #{disease_record.id}】，等待有权限的专家认领处理。"
        elif disease_record is not None:
            advice = f"模型初诊需复核（评分：{score_label}），建议农技专家复核。已保存诊断记录；按请求关闭普通低置信度自动派单，本次未创建专家复核工单。"
        else:
            advice = f"模型初诊需复核（评分：{score_label}），建议农技专家复核。未选择关联田块，本次仅提供诊断结果，未保存病害记录或创建专家复核工单。"
        if field_id is None and not need_expert and not image_rejected:
            advice += " 未选择关联田块，本次仅提供诊断结果，未保存病害记录或创建专家复核工单。"

        return VisualDiagnosisResponse(
            image_url=request.image_url,
            crop=crop_name,
            suspected_disease=suspected,
            confidence=confidence,
            features_detected=features,
            severity=severity,
            need_expert=need_expert,
            disease_record_id=disease_record.id if disease_record is not None else None,
            ticket_id=disease_record.id if dispatch_ticket and disease_record is not None else None,
            advice=advice,
            standard_treatment=standard_treatment,
        )

    @staticmethod
    def ticket_item(record):
        field = record.field
        farm = field.farm if field else None
        crop = record.crop_planting
        review = record.review
        feedback = DiseaseReviewService.feedback(record)
        expert = review.assigned_expert if review else None
        expert_id = review.assigned_expert_id if review else None
        expert_name = (expert.display_name or expert.username) if expert else None
        if not expert_name and feedback:
            expert_name = feedback.expert_name
        return ExpertTicketItem(ticket_id=record.id, disease_record_id=record.id, field_id=record.field_id,
            field_name=field.name if field else None, farm_name=farm.name if farm else None,
            region=f"{farm.province or ''}{farm.city or ''}" if farm else None,
            crop=(review.initial_crop if review and review.initial_crop else (crop.crop if crop else "未录入")),
            variety=crop.variety if crop else None, growth_stage=(crop.growth_stage or "未录入") if crop else "未录入",
            image_url=record.image_url, initial_suspected_disease=record.disease_name,
            initial_confidence=record.diagnosis_confidence, symptoms=record.symptoms, status=record.status,
            expert_id=expert_id, expert_name=expert_name, created_at=record.created_at, updated_at=record.updated_at,
            claim_available=DiseaseReviewService.claim_available(record), feedback=feedback,
            diagnosis_method=record.diagnosis_method or "unknown")

    @classmethod
    async def list_tickets(cls, db, status=None, page=1, page_size=20, offset=None):
        condition = DiseaseRecord.status == status if status else DiseaseRecord.status.in_(["pending_review", "in_review", "expert_reviewed"])
        total = await db.scalar(select(func.count()).select_from(DiseaseRecord).where(condition))
        rows = (await db.scalars(DiseaseReviewService.record_query().where(condition)
            .order_by(DiseaseRecord.created_at.desc(), DiseaseRecord.id.desc()).offset(offset if offset is not None else (page - 1) * page_size).limit(page_size))).all()
        return [cls.ticket_item(record) for record in rows], total

    @classmethod
    async def get_ticket_detail(cls, db, ticket_id):
        record = await db.scalar(DiseaseReviewService.record_query().where(DiseaseRecord.id == ticket_id))
        if record is None: return None
        field = record.field
        latest = await db.scalar(select(WeatherSnapshot).where(WeatherSnapshot.field_id == record.field_id)
            .order_by(WeatherSnapshot.timestamp.desc(), WeatherSnapshot.id.desc()).limit(1))
        weather = None
        if latest:
            weather = {"temperature": finite_number(latest.temperature), "humidity": finite_number(latest.humidity),
                "rainfall_mm": finite_number(latest.rainfall), "wind_speed": finite_number(latest.wind_speed),
                "weather": latest.weather, "source": latest.source,
                "timestamp": latest.timestamp.isoformat() if latest.timestamp else None}
        records = (await db.scalars(select(FarmingRecord).where(FarmingRecord.field_id == record.field_id, FarmingRecord.revoked_at.is_(None))
            .order_by(FarmingRecord.record_date.desc(), FarmingRecord.id.desc()).limit(5))).all()
        recent = [{"id": item.id, "record_type": item.record_type, "record_date": item.record_date.isoformat(),
            "input_name": item.input_name, "input_amount": item.input_amount, "unit": item.unit,
            "description": item.description} for item in records]
        return ExpertTicketDetail(**cls.ticket_item(record).model_dump(),
            field_soil_type=field.soil_type if field else None, field_area=field.area if field else None,
            current_weather=weather, recent_farming_records=recent,
            ai_features=(record.review.image_features or []) if record.review else [])

    @classmethod
    async def claim_ticket(cls, db, ticket_id, expert_user):
        await DiseaseReviewService.claim(db, ticket_id, expert_user)
        detail = await cls.get_ticket_detail(db, ticket_id)
        if detail is None: raise HTTPException(404, "工单不存在")
        return ExpertTicketItem.model_validate(detail.model_dump())

    @classmethod
    async def resolve_ticket(cls, db, ticket_id, expert_user, resolve_in):
        return await DiseaseReviewService.resolve(db, ticket_id, expert_user, resolve_in)
