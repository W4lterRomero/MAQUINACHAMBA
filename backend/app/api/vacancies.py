"""
Router para vacantes de empleo (REQ-007, REQ-008, REQ-009).
Deduplicación por hash, trazabilidad de origen y detección de vacantes cerradas o sospechosas de fraude.
"""

from datetime import date, datetime, timezone
import hashlib
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.security import validate_url
from backend.app.modules.vacancies.models import Vacancy
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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_import_vacancy(
    payload: VacancyCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Registra o importa una vacante de empleo.
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
    """Lista las vacantes del tenant con sus indicadores de fraude y vigencia."""
    query = select(Vacancy).where(Vacancy.tenant_id == tenant_id)
    if not include_inactive:
        query = query.where(Vacancy.is_active.is_(True))

    res = await session.execute(query.order_by(Vacancy.created_at.desc()))
    vacancies = res.scalars().all()
    return [
        {
            "id": v.id,
            "company_name": v.company_name,
            "job_title": v.job_title,
            "source": v.source,
            "source_url": v.source_url,
            "location": v.location,
            "remote_type": v.remote_type,
            "requirements": v.requirements,
            "posted_date": v.posted_date.isoformat() if v.posted_date else None,
            "closing_date": v.closing_date.isoformat() if v.closing_date else None,
            "is_active": v.is_active,
            "is_fraud_suspect": any(s.get("severity") == "high" for s in (v.fraud_signals or [])),
            "fraud_signals": v.fraud_signals or [],
            "closure_reason": v.closure_reason,
            "last_verified_at": v.last_verified_at.isoformat() if v.last_verified_at else None,
        }
        for v in vacancies
    ]


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
