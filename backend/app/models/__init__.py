# SQLAlchemy Models Package
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeItem
from app.models.product import Product
from app.models.settings import SystemSettings
from app.models.statistics import StatisticsDaily, StatisticsMeta
# 农业智能系统新增实体
from app.models.farm import Farm
from app.models.field import Field
from app.models.field_environment import FieldEnvironmentObservation
from app.models.crop import CropPlanting
from app.models.crop_status_event import CropStatusEvent
from app.models.farming_record import FarmingRecord
from app.models.disease_record import DiseaseRecord
from app.models.weather import WeatherSnapshot
from app.models.expert_case import ExpertCase
from app.models.plan_execution import PlanExecution
from app.models.agri_plan_snapshot import AgriPlanSnapshot
from app.models.agri_conversation import AgriConversation, AgriTurn, AgriConsultation
from app.models.consultation_review import ConsultationReview
from app.models.disease_review import DiseaseReview
from app.models.diagnosis_media import DiagnosisMedia, DiagnosisRequest

__all__ = [
    "User",
    "Conversation",
    "Message",
    "KnowledgeItem",
    "Product",
    "SystemSettings",
    "StatisticsDaily",
    "StatisticsMeta",
    # 农业模型
    "Farm",
    "Field",
    "FieldEnvironmentObservation",
    "CropPlanting",
    "CropStatusEvent",
    "FarmingRecord",
    "DiseaseRecord",
    "WeatherSnapshot",
    "ExpertCase",
    "PlanExecution",
    "AgriPlanSnapshot",
    "AgriConversation",
    "AgriTurn",
    "AgriConsultation",
    "ConsultationReview",
    "DiseaseReview",
    "DiagnosisMedia",
    "DiagnosisRequest",
]
