"""
Router para vacantes de empleo y búsqueda automatizada (REQ-007, REQ-008, REQ-009, REQ-030).
Deduplicación por hash, preferencias persistentes, conectores de lectura,
detección de vacantes cerradas o sospechosas y clasificación determinista contra el perfil.
"""

from datetime import date, datetime, timezone
import hashlib
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.security import validate_url
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.vacancies.connectors.remotive import RemotiveJobConnector
from backend.app.modules.vacancies.models import JobSearchPreferences, Vacancy
from backend.app.modules.vacancies.search_service import JobSearchService
from backend.app.modules.vacancies.service import VacancyIntegrityService

router = APIRouter(prefix="/vacancies", tags=["Vacancies"])


class VacancyCreate(BaseModel):
    company_name: str
    job_title: str
    source_url: str
    source: str = "manual"
    location: str = "Remoto"
    remote_type: str = "remote"  # remote, onsite, hybrid, unknown
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = "USD"
    requirements: List[str] = []
    description: str = ""
    closing_date: Optional[date] = None


class PreferencesUpdate(BaseModel):
    desired_job_titles: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    remote_modalities: Optional[List[str]] = None
    experience_level: Optional[str] = None
    salary_min: Optional[float] = None
    salary_currency: Optional[str] = None
    excluded_keywords: Optional[List[str]] = None
    excluded_companies: Optional[List[str]] = None
    interval_hours: Optional[int] = None


class AuthorizeSourcePayload(BaseModel):
    source_name: str = "remotive"
    confirm: bool = True


class SearchRunPayload(BaseModel):
    force_dry_run: bool = False
    limit: int = Field(default=20, ge=1, le=50)


class TogglePeriodicPayload(BaseModel):
    enabled: bool
    interval_hours: Optional[int] = Field(default=None, ge=1, le=168)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_import_vacancy(
    payload: VacancyCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Registra o importa una vacante de empleo manual o desde CSV.
    Aplica validación de URL (anti-SSRF formato), deduplicación por hash determinista
    y análisis de integridad contra vacantes cerradas y señales de fraude (REQ-009).
    """
    # 1. Validación de seguridad de URL (SEC-004)
    is_safe, reason = validate_url(payload.source_url)
    if not is_safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL rechazada por política de seguridad: {reason}",
        )

    # 2. Análisis de integridad y detección de fraude / cierre (REQ-009)
    integrity = VacancyIntegrityService.analyze_vacancy(
        title=payload.job_title,
        description=payload.description,
        source_url=payload.source_url,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_currency=payload.salary_currency,
        closing_date=payload.closing_date,
    )

    # 3. Cálculo determinista de dedup_hash (REQ-008)
    dedup_input = f"{payload.source_url.strip().lower()}|{payload.company_name.strip().lower()}|{payload.job_title.strip().lower()}"
    dedup_hash = hashlib.sha256(dedup_input.encode("utf-8")).hexdigest()

    # Comprobar duplicado existente en el tenant
    existing_res = await session.execute(
        select(Vacancy).where(
            Vacancy.tenant_id == tenant_id,
            Vacancy.dedup_hash == dedup_hash,
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing:
        return {
            "id": existing.id,
            "status": "already_exists",
            "dedup_hash": existing.dedup_hash,
            "company_name": existing.company_name,
            "job_title": existing.job_title,
            "is_active": existing.is_active,
            "is_fraud_suspect": any(s.get("severity") == "high" for s in (existing.fraud_signals or [])),
            "fraud_signals": existing.fraud_signals or [],
            "closure_reason": existing.closure_reason,
        }

    now = datetime.now(timezone.utc)
    vacancy = Vacancy(
        tenant_id=tenant_id,
        company_name=payload.company_name,
        job_title=payload.job_title,
        source=payload.source,
        source_url=payload.source_url,
        location=payload.location,
        remote_type=payload.remote_type,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_currency=payload.salary_currency,
        requirements=payload.requirements,
        description=payload.description,
        closing_date=payload.closing_date,
        is_active=integrity["is_active"],
        fraud_signals=integrity["fraud_signals"],
        closure_reason=integrity["closure_reason"],
        dedup_hash=dedup_hash,
        fetched_at=now,
        is_real_search_result=False,
    )
    session.add(vacancy)
    await session.commit()
    await session.refresh(vacancy)

    return {
        "id": vacancy.id,
        "status": "created",
        "dedup_hash": vacancy.dedup_hash,
        "company_name": vacancy.company_name,
        "job_title": vacancy.job_title,
        "location": vacancy.location,
        "remote_type": vacancy.remote_type,
        "is_active": vacancy.is_active,
        "is_fraud_suspect": integrity["is_fraud_suspect"],
        "fraud_signals": vacancy.fraud_signals,
        "is_closed": integrity["is_closed"],
        "closure_reason": vacancy.closure_reason,
    }


@router.get("")
async def list_vacancies(
    include_inactive: bool = Query(True, description="Incluir vacantes cerradas o sospechosas"),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Lista las vacantes del tenant con sus indicadores de fraude, vigencia,
    fecha de consulta y puntuación de compatibilidad calculada.
    """
    query = select(Vacancy).where(Vacancy.tenant_id == tenant_id)
    if not include_inactive:
        query = query.where(Vacancy.is_active.is_(True))

    res = await session.execute(query.order_by(Vacancy.created_at.desc()))
    vacancies = res.scalars().all()

    # Obtener evaluaciones deterministas existentes para este tenant
    evals_res = await session.execute(
        select(Evaluation).where(Evaluation.tenant_id == tenant_id)
    )
    eval_by_vac: Dict[str, Evaluation] = {}
    for ev in evals_res.scalars().all():
        if ev.vacancy_id not in eval_by_vac:
            eval_by_vac[ev.vacancy_id] = ev

    result_list = []
    for v in vacancies:
        eval_item = eval_by_vac.get(v.id)
        result_list.append({
            "id": v.id,
            "company_name": v.company_name,
            "job_title": v.job_title,
            "source": v.source,
            "source_url": v.source_url,
            "location": v.location,
            "remote_type": v.remote_type,
            "salary_min": float(v.salary_min) if v.salary_min else None,
            "salary_max": float(v.salary_max) if v.salary_max else None,
            "salary_currency": v.salary_currency,
            "requirements": v.requirements,
            "posted_date": v.posted_date.isoformat() if v.posted_date else None,
            "closing_date": v.closing_date.isoformat() if v.closing_date else None,
            "fetched_at": v.fetched_at.isoformat() if hasattr(v, "fetched_at") and v.fetched_at else None,
            "is_real_search_result": getattr(v, "is_real_search_result", False),
            "is_active": v.is_active,
            "is_fraud_suspect": any(s.get("severity") == "high" for s in (v.fraud_signals or [])),
            "fraud_signals": v.fraud_signals or [],
            "closure_reason": v.closure_reason,
            "last_verified_at": v.last_verified_at.isoformat() if v.last_verified_at else None,
            "compatibility_score": eval_item.compatibility_score if eval_item else None,
            "recommendation": eval_item.recommendation if eval_item else None,
            "matched_skills": eval_item.matched_skills if eval_item else [],
            "missing_skills": eval_item.missing_skills if eval_item else [],
        })

    return result_list


# === ENDPOINTS DE PREFERENCIAS Y BÚSQUEDA AUTOMATIZADA ===


@router.get("/preferences")
async def get_search_preferences(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Retorna las preferencias de búsqueda configuradas y el estado de autorización de fuentes."""
    pref = await JobSearchService.get_or_create_preferences(session, tenant_id)
    connector = RemotiveJobConnector()

    return {
        "desired_job_titles": pref.desired_job_titles or [],
        "skills": pref.skills or [],
        "locations": pref.locations or [],
        "remote_modalities": pref.remote_modalities or ["remote"],
        "experience_level": pref.experience_level,
        "salary_min": float(pref.salary_min) if pref.salary_min else None,
        "salary_currency": pref.salary_currency,
        "excluded_keywords": pref.excluded_keywords or [],
        "excluded_companies": pref.excluded_companies or [],
        "is_periodic_enabled": pref.is_periodic_enabled,
        "interval_hours": pref.interval_hours,
        "last_run_at": pref.last_run_at.isoformat() if pref.last_run_at else None,
        "next_run_at": pref.next_run_at.isoformat() if pref.next_run_at else None,
        "last_status": pref.last_status,
        "last_error": pref.last_error,
        "last_results_count": pref.last_results_count,
        "last_new_jobs_count": pref.last_new_jobs_count,
        "source_access_authorized": pref.source_access_authorized,
        "authorized_sources": pref.authorized_sources or ["remotive"],
        "source_metadata": {
            "name": connector.source_name,
            "base_domain": connector.base_domain,
            "data_sent": connector.data_sent_description,
            "requires_credentials": False,
        },
    }


@router.put("/preferences")
async def update_search_preferences(
    payload: PreferencesUpdate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Actualiza los criterios y exclusiones de búsqueda de empleo."""
    updated = await JobSearchService.update_preferences(
        session=session,
        tenant_id=tenant_id,
        data=payload.model_dump(exclude_unset=True),
    )
    return {
        "status": "success",
        "message": "Preferencias de búsqueda actualizadas correctamente.",
        "desired_job_titles": updated.desired_job_titles or [],
        "skills": updated.skills or [],
        "locations": updated.locations or [],
        "remote_modalities": updated.remote_modalities or ["remote"],
        "experience_level": updated.experience_level,
        "salary_min": float(updated.salary_min) if updated.salary_min else None,
        "salary_currency": updated.salary_currency,
        "excluded_keywords": updated.excluded_keywords or [],
        "excluded_companies": updated.excluded_companies or [],
        "is_periodic_enabled": updated.is_periodic_enabled,
        "interval_hours": updated.interval_hours,
        "last_run_at": updated.last_run_at.isoformat() if updated.last_run_at else None,
        "next_run_at": updated.next_run_at.isoformat() if updated.next_run_at else None,
        "last_status": updated.last_status,
        "last_error": updated.last_error,
        "last_results_count": updated.last_results_count,
        "last_new_jobs_count": updated.last_new_jobs_count,
        "source_access_authorized": updated.source_access_authorized,
        "authorized_sources": updated.authorized_sources or ["remotive"],
        "preferences": {
            "desired_job_titles": updated.desired_job_titles,
            "skills": updated.skills,
            "locations": updated.locations,
            "remote_modalities": updated.remote_modalities,
            "experience_level": updated.experience_level,
            "salary_min": float(updated.salary_min) if updated.salary_min else None,
            "salary_currency": updated.salary_currency,
            "excluded_keywords": updated.excluded_keywords,
            "excluded_companies": updated.excluded_companies,
            "interval_hours": updated.interval_hours,
        },
    }


@router.post("/preferences/authorize-source")
async def authorize_source_access(
    payload: AuthorizeSourcePayload,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Otorga autorización explícita para que la aplicación realice consultas HTTP
    de solo lectura a la fuente externa pública configurada (Remotive).
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe confirmar explícitamente la autorización de acceso a red.",
        )

    updated = await JobSearchService.authorize_source_access(
        session=session,
        tenant_id=tenant_id,
        source_name=payload.source_name,
    )

    return {
        "status": "authorized",
        "message": f"Acceso de lectura a la fuente '{payload.source_name}' autorizado por el usuario.",
        "source_access_authorized": updated.source_access_authorized,
        "authorized_sources": updated.authorized_sources,
    }


@router.post("/preferences/revoke-source")
async def revoke_source_access(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Revoca la autorización de consultas de red externas, regresando a modo local seguro."""
    updated = await JobSearchService.revoke_source_access(session, tenant_id)
    return {
        "status": "revoked",
        "message": "Acceso a fuentes de red externas revocado. El sistema operará en modo local sin salida a red.",
        "source_access_authorized": updated.source_access_authorized,
    }


@router.post("/search/toggle-periodic")
async def toggle_periodic_search(
    payload: TogglePeriodicPayload,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Activa o pausa la búsqueda periódica automática en segundo plano."""
    updated = await JobSearchService.toggle_periodic_search(
        session=session,
        tenant_id=tenant_id,
        enabled=payload.enabled,
        interval_hours=payload.interval_hours,
    )
    return {
        "status": "success",
        "is_periodic_enabled": updated.is_periodic_enabled,
        "interval_hours": updated.interval_hours,
        "next_run_at": updated.next_run_at.isoformat() if updated.next_run_at else None,
    }


@router.post("/search/run")
async def run_job_search(
    payload: SearchRunPayload = SearchRunPayload(),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Ejecuta un ciclo de búsqueda inmediata de vacantes:
    - Consulta la fuente Remotive (en vivo si está autorizada; con muestra verificable si no).
    - Aplica deduplicación, filtros de exclusión y análisis de integridad.
    - Clasifica de manera determinista respecto al perfil verificado del usuario.
    """
    try:
        result = await JobSearchService.execute_job_search(
            session=session,
            tenant_id=tenant_id,
            force_dry_run=payload.force_dry_run,
            limit=payload.limit,
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo durante la ejecución de búsqueda: {e}",
        )


@router.get("/search/status")
async def get_search_status(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Retorna el estado operativo actual del servicio de búsqueda y última ejecución."""
    pref = await JobSearchService.get_or_create_preferences(session, tenant_id)
    return {
        "last_status": pref.last_status,
        "last_run_at": pref.last_run_at.isoformat() if pref.last_run_at else None,
        "next_run_at": pref.next_run_at.isoformat() if pref.next_run_at else None,
        "last_error": pref.last_error,
        "is_periodic_enabled": pref.is_periodic_enabled,
        "interval_hours": pref.interval_hours,
        "source_access_authorized": pref.source_access_authorized,
        "last_results_count": pref.last_results_count,
        "last_new_jobs_count": pref.last_new_jobs_count,
    }


@router.get("/{vacancy_id}")
async def get_vacancy(
    vacancy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene los detalles de una vacante específica incluyendo auditoría de integridad."""
    res = await session.execute(
        select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    return {
        "id": vacancy.id,
        "company_name": vacancy.company_name,
        "job_title": vacancy.job_title,
        "source": vacancy.source,
        "source_url": vacancy.source_url,
        "location": vacancy.location,
        "remote_type": vacancy.remote_type,
        "salary_min": float(vacancy.salary_min) if vacancy.salary_min else None,
        "salary_max": float(vacancy.salary_max) if vacancy.salary_max else None,
        "salary_currency": vacancy.salary_currency,
        "requirements": vacancy.requirements,
        "description": vacancy.description,
        "posted_date": vacancy.posted_date.isoformat() if vacancy.posted_date else None,
        "closing_date": vacancy.closing_date.isoformat() if vacancy.closing_date else None,
        "fetched_at": vacancy.fetched_at.isoformat() if hasattr(vacancy, "fetched_at") and vacancy.fetched_at else None,
        "is_real_search_result": getattr(vacancy, "is_real_search_result", False),
        "is_active": vacancy.is_active,
        "is_fraud_suspect": any(s.get("severity") == "high" for s in (vacancy.fraud_signals or [])),
        "fraud_signals": vacancy.fraud_signals or [],
        "closure_reason": vacancy.closure_reason,
        "dedup_hash": vacancy.dedup_hash,
        "last_verified_at": vacancy.last_verified_at.isoformat() if vacancy.last_verified_at else None,
    }


@router.post("/{vacancy_id}/verify-status")
async def verify_vacancy_status(
    vacancy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Re-analiza y actualiza deterministamente el estado de vigencia y fraude de la vacante (REQ-009).
    """
    res = await session.execute(
        select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    integrity = VacancyIntegrityService.analyze_vacancy(
        title=vacancy.job_title,
        description=vacancy.description,
        source_url=vacancy.source_url,
        salary_min=float(vacancy.salary_min) if vacancy.salary_min else None,
        salary_max=float(vacancy.salary_max) if vacancy.salary_max else None,
        salary_currency=vacancy.salary_currency,
        closing_date=vacancy.closing_date,
    )

    vacancy.is_active = integrity["is_active"]
    vacancy.fraud_signals = integrity["fraud_signals"]
    vacancy.closure_reason = integrity["closure_reason"]
    vacancy.last_verified_at = datetime.now(timezone.utc)
    session.add(vacancy)
    await session.commit()
    await session.refresh(vacancy)

    return {
        "id": vacancy.id,
        "is_active": vacancy.is_active,
        "is_fraud_suspect": integrity["is_fraud_suspect"],
        "fraud_signals": vacancy.fraud_signals,
        "is_closed": integrity["is_closed"],
        "closure_reason": vacancy.closure_reason,
        "last_verified_at": vacancy.last_verified_at.isoformat(),
    }
