"""
Punto de agregación de routers de la API v1.
"""

from fastapi import APIRouter

from backend.app.api.auth import router as auth_router
from backend.app.api.profile import router as profile_router
from backend.app.api.vacancies import router as vacancies_router
from backend.app.api.evaluations import router as evaluations_router
from backend.app.api.approvals import router as approvals_router
from backend.app.api.crm import router as crm_router
from backend.app.api.execution import router as execution_router
from backend.app.api.communication import router as communication_router
from backend.app.api.ai_config import router as ai_router
from backend.app.api.reports import router as reports_router
from backend.app.api.backups import router as backups_router
from backend.app.api.portals import router as portals_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(profile_router)
api_router.include_router(vacancies_router)
api_router.include_router(evaluations_router)
api_router.include_router(approvals_router)
api_router.include_router(crm_router)
api_router.include_router(execution_router)
api_router.include_router(communication_router)
api_router.include_router(ai_router)
api_router.include_router(reports_router)
api_router.include_router(backups_router)
api_router.include_router(portals_router)

