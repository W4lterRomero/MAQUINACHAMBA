"""
Router para revisión, aprobación y ciclo de control de candidaturas (REQ-014, REQ-015, REQ-017, REQ-018).
Aprobación humana obligatoria, inmutabilidad y programación para el día siguiente.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask
from backend.app.modules.approvals.service import ApprovalService

router = APIRouter(prefix="/candidacies", tags=["Candidacies & Approvals"])


class PrepareCandidacyRequest(BaseModel):
    vacancy_id: str
    cv_id: Optional[str] = None
    evaluation_id: Optional[str] = None
    cover_letter: Optional[str] = None
    force_proceed: Optional[bool] = False


class ApproveCandidacyRequest(BaseModel):
    user_timezone: str = "America/Mexico_City"
    notes: Optional[str] = None


class RejectCandidacyRequest(BaseModel):
    reason: Optional[str] = None


class ModifyCandidacyRequest(BaseModel):
    new_cover_letter: Optional[str] = None
    new_cv_snapshot: Optional[Dict[str, Any]] = None


@router.post("/prepare", status_code=status.HTTP_201_CREATED)
async def prepare_candidacy(
    payload: PrepareCandidacyRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-016: Prepara el paquete de candidatura para revisión humana.
    ESTRICTAMENTE NINGÚN ENVÍO durante esta fase.
    REQ-009: Bloquea candidaturas sobre vacantes cerradas o con fraude sospechoso
    salvo confirmación explícita (force_proceed=True).
    """
    p_res = await session.execute(
        select(UserProfile)
        .where(UserProfile.tenant_id == tenant_id)
        .order_by(UserProfile.created_at.desc())
    )
    profile = p_res.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado.")

    v_res = await session.execute(
        select(Vacancy).where(Vacancy.id == payload.vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = v_res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    # Reanálisis dinámico en servidor antes de preparar (P1, REQ-009)
    # Recalcula fecha de cierre contra fecha actual y actualiza la vacante
    analysis = VacancyIntegrityService.analyze_vacancy(
        title=vacancy.job_title,
        description=vacancy.description,
        source_url=vacancy.source_url or "",
        salary_min=float(vacancy.salary_min) if vacancy.salary_min else None,
        salary_max=float(vacancy.salary_max) if vacancy.salary_max else None,
        salary_currency=vacancy.salary_currency or "USD",
        closing_date=vacancy.closing_date,
    )
    was_inactive = not vacancy.is_active
    is_closed = was_inactive or analysis["is_closed"]
    is_fraud_suspect = analysis["is_fraud_suspect"] or any(s.get("severity") == "high" for s in (vacancy.fraud_signals or []))

    # Sincronizar estado en vacante
    vacancy.is_active = not is_closed
    # Combinar señales de fraude detectadas
    existing_signals = {s.get("id"): s for s in (vacancy.fraud_signals or [])}
    for s in analysis["fraud_signals"]:
        existing_signals[s["id"]] = s
    vacancy.fraud_signals = list(existing_signals.values())

    if analysis["closure_reason"]:
        vacancy.closure_reason = analysis["closure_reason"]
    elif was_inactive and not vacancy.closure_reason:
        vacancy.closure_reason = "Vacante inactiva en el sistema."
    vacancy.last_verified_at = datetime.now(timezone.utc)
    session.add(vacancy)
    await session.flush()

    # Protección contra fraude y vacantes expiradas (REQ-009)
    if (is_fraud_suspect or is_closed) and not payload.force_proceed:
        reason_str = vacancy.closure_reason or "Señales de fraude de alta gravedad detectadas."
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Postulación bloqueada por seguridad: la vacante está cerrada o presenta indicios de fraude ({reason_str}). Para proceder bajo su propio criterio, active force_proceed=true.",
        )

    exception_consent = None
    if (is_fraud_suspect or is_closed) and payload.force_proceed:
        exception_consent = {
            "action": "user_exception_consent",
            "forced_at": datetime.now(timezone.utc).isoformat(),
            "reason": analysis["closure_reason"] or "Riesgo de fraude confirmado por usuario",
            "state_version": vacancy.updated_at.isoformat() if vacancy.updated_at else datetime.now(timezone.utc).isoformat(),
            "fraud_signals": analysis["fraud_signals"],
            "is_closed": is_closed,
        }

    if payload.cv_id:
        c_res = await session.execute(
            select(MasterCV).where(MasterCV.id == payload.cv_id, MasterCV.tenant_id == tenant_id)
        )
        cv = c_res.scalar_one_or_none()
    else:
        c_res = await session.execute(
            select(MasterCV)
            .where(MasterCV.profile_id == profile.id, MasterCV.tenant_id == tenant_id)
            .order_by(MasterCV.created_at.desc())
        )
        cv = c_res.scalars().first()

    if not cv:
        raise HTTPException(status_code=400, detail="No hay CV disponible.")

    evaluation = None
    if payload.evaluation_id:
        ev_res = await session.execute(
            select(Evaluation).where(
                Evaluation.id == payload.evaluation_id,
                Evaluation.tenant_id == tenant_id,
            )
        )
        evaluation = ev_res.scalar_one_or_none()

    candidacy = await ApprovalService.prepare_candidacy(
        session=session,
        tenant_id=tenant_id,
        profile=profile,
        cv=cv,
        vacancy=vacancy,
        evaluation=evaluation,
        cover_letter=payload.cover_letter,
        exception_consent=exception_consent,
    )
    await session.commit()
    await session.refresh(candidacy)

    content_hash = ApprovalService.compute_candidacy_hash(candidacy)

    return {
        "id": candidacy.id,
        "status": candidacy.status,
        "content_hash": content_hash,
        "version": candidacy.version,
        "target_portal": candidacy.target_portal,
        "target_url": candidacy.target_url,
        "vacancy": {
            "id": vacancy.id,
            "title": vacancy.job_title,
            "company": vacancy.company_name,
        },
        "cover_letter_preview": candidacy.cover_letter[:150] + "...",
    }


@router.get("/{candidacy_id}")
async def get_candidacy_for_review(
    candidacy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-014: Inspección completa del paquete antes de aprobar.
    Muestra la vacante, el CV adaptado, la carta de presentación, el formulario y las acciones.
    """
    c_res = await session.execute(
        select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
    )
    candidacy = c_res.scalar_one_or_none()
    if not candidacy:
        raise HTTPException(status_code=404, detail="Candidatura no encontrada.")

    v_res = await session.execute(
        select(Vacancy).where(Vacancy.id == candidacy.vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = v_res.scalar_one_or_none()

    content_hash = ApprovalService.compute_candidacy_hash(candidacy)

    # Comprobar si ya tiene aprobación activa
    app_res = await session.execute(
        select(Approval).where(
            Approval.candidacy_id == candidacy.id,
            Approval.tenant_id == tenant_id,
            Approval.decision == "approved",
            Approval.invalidated_at.is_(None),
        )
    )
    active_approval = app_res.scalar_one_or_none()

    # Comprobar tarea programada
    task_res = await session.execute(
        select(ScheduledTask).where(
            ScheduledTask.candidacy_id == candidacy.id,
            ScheduledTask.tenant_id == tenant_id,
        )
    )
    task = task_res.scalars().first()

    return {
        "candidacy": {
            "id": candidacy.id,
            "status": candidacy.status,
            "version": candidacy.version,
            "content_hash": content_hash,
            "target_portal": candidacy.target_portal,
            "target_url": candidacy.target_url,
            "adapted_cv_snapshot": candidacy.adapted_cv_snapshot,
            "cover_letter": candidacy.cover_letter,
            "form_responses": candidacy.form_responses,
            "planned_actions": candidacy.planned_actions,
        },
        "vacancy": {
            "id": vacancy.id if vacancy else None,
            "company_name": vacancy.company_name if vacancy else "",
            "job_title": vacancy.job_title if vacancy else "",
            "location": vacancy.location if vacancy else "",
        },
        "approval": {
            "id": active_approval.id if active_approval else None,
            "decision": active_approval.decision if active_approval else None,
            "decided_at": active_approval.decided_at.isoformat() if active_approval else None,
        } if active_approval else None,
        "scheduled_task": {
            "id": task.id,
            "scheduled_date": task.scheduled_date.isoformat(),
            "execution_instant_utc": task.execution_instant_utc.isoformat(),
            "status": task.status,
        } if task else None,
    }


@router.post("/{candidacy_id}/approve", status_code=status.HTTP_200_OK)
async def approve_candidacy(
    candidacy_id: str,
    payload: ApproveCandidacyRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-015: Aprobación humana explícita.
    Genera registro inmutable y programa la ejecución para el día siguiente.
    """
    try:
        approval, task = await ApprovalService.approve_candidacy(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
            user_timezone=payload.user_timezone,
            notes=payload.notes,
        )
        await session.commit()
        return {
            "status": "approved_and_scheduled",
            "approval_id": approval.id,
            "content_hash": approval.content_hash,
            "scheduled_task_id": task.id,
            "scheduled_date": task.scheduled_date.isoformat(),
            "execution_instant_utc": task.execution_instant_utc.isoformat(),
            "idempotency_key": task.idempotency_key,
        }
    except ValueError as exc:
        if "no encontrada" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{candidacy_id}/reject", status_code=status.HTTP_200_OK)
async def reject_candidacy(
    candidacy_id: str,
    payload: RejectCandidacyRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-018: Rechazo explícito por el usuario.
    Bloquea permanentemente el envío y cancela cualquier tarea pendiente.
    """
    try:
        approval = await ApprovalService.reject_candidacy(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
            reason=payload.reason,
        )
        await session.commit()
        return {
            "status": "rejected_by_user",
            "approval_id": approval.id,
            "decision": approval.decision,
            "notes": approval.notes,
        }
    except ValueError as exc:
        if "no encontrada" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{candidacy_id}/modify", status_code=status.HTTP_200_OK)
async def modify_candidacy(
    candidacy_id: str,
    payload: ModifyCandidacyRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-017: Modificación de contenido.
    Invalida inmediatamente cualquier aprobación previa y cancela tareas en cola.
    """
    try:
        candidacy = await ApprovalService.modify_candidacy(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
            new_cover_letter=payload.new_cover_letter,
            new_cv_snapshot=payload.new_cv_snapshot,
        )
        await session.commit()
        return {
            "status": "modified_and_invalidated",
            "candidacy_id": candidacy.id,
            "new_version": candidacy.version,
            "current_status": candidacy.status,
            "message": "Aprobación previa invalidada. La candidatura requiere nueva revisión humana.",
        }
    except ValueError as exc:
        if "no encontrada" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{candidacy_id}/export/{format_type}")
async def export_candidacy_cv(
    candidacy_id: str,
    format_type: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Exporta el documento curricular adaptado vinculado a la candidatura (REQ-013).
    Garantiza inmutabilidad y previene sustituciones silenciosas de paquetes aprobados.
    """
    from backend.app.modules.cv.service import (
        CVService,
        CVServiceError,
        CVServiceNotFoundError,
        CVServiceImmutableError,
    )
    from fastapi import Response

    try:
        content_bytes, filename, media_type, file_hash = await CVService.export_candidacy_cv(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
            format_type=format_type,
        )
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Document-Hash": file_hash,
            },
        )
    except CVServiceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CVServiceImmutableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except CVServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


class LinkedInConfirmRequest(BaseModel):
    confirmation_note: Optional[str] = None
    external_reference: Optional[str] = None


@router.get("/{candidacy_id}/linkedin-assisted", status_code=status.HTTP_200_OK)
async def get_linkedin_assisted_package(
    candidacy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-024: Obtiene el paquete de postulación guiada y checklist para LinkedIn en Modo Asistido.
    """
    from backend.app.modules.portals.linkedin_service import (
        LinkedInAssistedService,
        LinkedInAssistedError,
    )
    try:
        return await LinkedInAssistedService.get_assisted_package(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
        )
    except LinkedInAssistedError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{candidacy_id}/linkedin-confirm", status_code=status.HTTP_200_OK)
async def confirm_linkedin_submission(
    candidacy_id: str,
    payload: Optional[LinkedInConfirmRequest] = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-024: Registra la confirmación manual de postulación enviada por el usuario en LinkedIn.
    Transiciona el estado en CRM a 'sent_confirmed' y actualiza la tarea durable.
    """
    from backend.app.modules.portals.linkedin_service import (
        LinkedInAssistedService,
        LinkedInAssistedError,
    )
    note = payload.confirmation_note if payload else None
    ref = payload.external_reference if payload else None
    try:
        return await LinkedInAssistedService.confirm_submission(
            session=session,
            tenant_id=tenant_id,
            candidacy_id=candidacy_id,
            confirmation_note=note,
            external_reference=ref,
        )
    except LinkedInAssistedError as e:
        raise HTTPException(status_code=400, detail=str(e))



