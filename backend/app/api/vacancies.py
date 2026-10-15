"""
Router para vacantes de empleo (REQ-007, REQ-008).
Deduplicación por hash y trazabilidad de origen.
"""

import hashlib
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.security import validate_url
from backend.app.modules.vacancies.models import Vacancy

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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_import_vacancy(
    payload: VacancyCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Registra o importa una vacante de empleo.
    Aplica validación de URL (anti-SSRF formato) y deduplicación por hash determinista.
    """
    # 1. Validación de seguridad de URL (SEC-004)
    is_safe, reason = validate_url(payload.source_url)
    if not is_safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL rechazada por política de seguridad: {reason}",
        )

    # 2. Cálculo determinista de dedup_hash (REQ-008)
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
        is_active=True,
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
    }


@router.get("")
async def list_vacancies(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista las vacantes activas del tenant."""
    res = await session.execute(
        select(Vacancy)
        .where(Vacancy.tenant_id == tenant_id, Vacancy.is_active.is_(True))
        .order_by(Vacancy.created_at.desc())
    )
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
        }
        for v in vacancies
    ]


@router.get("/{vacancy_id}")
async def get_vacancy(
    vacancy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene los detalles de una vacante específica."""
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
        "dedup_hash": vacancy.dedup_hash,
    }
