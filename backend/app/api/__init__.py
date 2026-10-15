# API Routes Package
from app.api.auth import router as auth_router
from app.api.knowledge import router as knowledge_router
from app.api.statistics import router as statistics_router
from app.api.performance import router as performance_router
from app.api.logs import router as logs_router
from app.api.settings import router as settings_router
from app.api.backup import router as backup_router
from app.api.farms import router as farms_router
from app.api.farming_records import router as farming_records_router
from app.api.agri_agent import router as agri_agent_router
from app.api.agri_history import router as agri_history_router
from app.api.expert_workbench import router as expert_workbench_router
from app.api.agri_plan import router as agri_plan_router

__all__ = [
    "auth_router",
    "knowledge_router",
    "statistics_router",
    "performance_router",
    "logs_router",
    "settings_router",
    "backup_router",
    "farms_router",
    "farming_records_router",
    "agri_agent_router",
    "agri_history_router",
    "expert_workbench_router",
    "agri_plan_router",
]



