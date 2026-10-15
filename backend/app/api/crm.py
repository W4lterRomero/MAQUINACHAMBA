"""
Router para el CRM de seguimiento de candidaturas (REQ-026).
Soporta vistas de tabla, Kanban, desglose por estado y auditoría de eventos.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.approvals.models import Candidacy

router = APIRouter(prefix="/crm", tags=["CRM"])


@router.get("/entries")
async def list_crm_entries(
    status_filter: Optional[str] = Query(None, alias="status"),
    priority_filter: Optional[str] = Query(None, alias="priority"),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-026: Lista las entradas de seguimiento del CRM.
    Permite filtrar por estado (para columnas Kanban) y prioridad.
    """
    where_conds = [CRMEntry.tenant_id == tenant_id]
    if status_filter:
        where_conds.append(CRMEntry.current_status == status_filter)
    if priority_filter:
        where_conds.append(CRMEntry.priority == priority_filter)

    stmt = select(CRMEntry).where(*where_conds).order_by(CRMEntry.updated_at.desc())
    res = await session.execute(stmt)
    entries = res.scalars().all()

    result = []
    for e in entries:
        # Enriquecer con datos de vacante
        v_res = await session.execute(
            select(Vacancy).where(Vacancy.id == e.vacancy_id, Vacancy.tenant_id == tenant_id)
        )
        vacancy = v_res.scalar_one_or_none()

        result.append({
            "id": e.id,
            "candidacy_id": e.candidacy_id,
            "vacancy_id": e.vacancy_id,
            "company_name": vacancy.company_name if vacancy else "Desconocida",
            "job_title": vacancy.job_title if vacancy else "Desconocido",
            "current_status": e.current_status,
            "priority": e.priority,
            "next_action": e.next_action,
            "next_action_date": e.next_action_date.isoformat() if e.next_action_date else None,
            "notes": e.notes,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        })

    return result


@router.get("/entries/{entry_id}")
async def get_crm_entry_detail(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-026: Vista de detalle de una postulación con trazabilidad de eventos.
    """
    stmt = select(CRMEntry).where(CRMEntry.id == entry_id, CRMEntry.tenant_id == tenant_id)
    res = await session.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entrada de CRM no encontrada.")

    # Vacancy y Candidacy
    v_res = await session.execute(
        select(Vacancy).where(Vacancy.id == entry.vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = v_res.scalar_one_or_none()

    c_res = await session.execute(
        select(Candidacy).where(Candidacy.id == entry.candidacy_id, Candidacy.tenant_id == tenant_id)
    )
    candidacy = c_res.scalar_one_or_none()

    # Eventos históricos
    ev_stmt = (
        select(CRMEvent)
        .where(CRMEvent.crm_entry_id == entry.id, CRMEvent.tenant_id == tenant_id)
        .order_by(CRMEvent.created_at.asc())
    )
    ev_res = await session.execute(ev_stmt)
    events = ev_res.scalars().all()

    return {
        "entry": {
            "id": entry.id,
            "current_status": entry.current_status,
            "priority": entry.priority,
            "next_action": entry.next_action,
            "next_action_date": entry.next_action_date.isoformat() if entry.next_action_date else None,
            "notes": entry.notes,
        },
        "vacancy": {
            "id": vacancy.id if vacancy else None,
            "company_name": vacancy.company_name if vacancy else "",
            "job_title": vacancy.job_title if vacancy else "",
            "source": vacancy.source if vacancy else "",
            "source_url": vacancy.source_url if vacancy else "",
        },
        "candidacy": {
            "id": candidacy.id if candidacy else None,
            "status": candidacy.status if candidacy else None,
            "version": candidacy.version if candidacy else None,
        },
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "old_status": ev.old_status,
                "new_status": ev.new_status,
                "details": ev.details,
                "timestamp": ev.created_at.isoformat(),
            }
            for ev in events
        ],
    }


@router.get("/summary")
async def get_crm_summary(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Resumen del pipeline de candidaturas agrupado por estado para el dashboard.
    """
    stmt = (
        select(CRMEntry.current_status, func.count(CRMEntry.id))
        .where(CRMEntry.tenant_id == tenant_id)
        .group_by(CRMEntry.current_status)
    )
    res = await session.execute(stmt)
    status_counts = dict(res.all())

    return {
        "total": sum(status_counts.values()),
        "by_status": status_counts,
    }
