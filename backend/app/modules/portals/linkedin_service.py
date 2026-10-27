"""
Servicio para gestión de postulaciones asistidas en LinkedIn (REQ-024, D7).
Proporciona:
1. Paquete asistido con checklist estructurado, respuestas de filtrado, carta de presentación y enlace seguro.
2. Confirmación humana de postulación efectuada (sent_confirmed) con registro inmutable en CRM y logs.
3. Validación anti-SSRF de la URL de destino.
4. Aislamiento estricto por tenant_id.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import validate_url
from backend.app.modules.approvals.models import (
    Approval,
    Candidacy,
    ScheduledTask,
    ExecutionLog,
    CandidacyDocument,
)
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.vacancies.models import Vacancy

logger = logging.getLogger("maquinachamba.portals.linkedin_service")


class LinkedInAssistedError(Exception):
    """Error al procesar la postulación asistida de LinkedIn."""
    pass


class LinkedInAssistedService:
    """Servicio para orquestar el flujo asistido de postulaciones en LinkedIn."""

    @classmethod
    async def get_assisted_package(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
    ) -> Dict[str, Any]:
        """
        Obtiene el paquete asistido completo para que el usuario complete su postulación en LinkedIn.
        """
        stmt = (
            select(Candidacy)
            .where(and_(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id))
        )
        candidacy = (await session.execute(stmt)).scalar_one_or_none()
        if not candidacy:
            raise LinkedInAssistedError(f"Candidatura '{candidacy_id}' no encontrada en el tenant.")

        # Obtener vacante
        v_stmt = select(Vacancy).where(and_(Vacancy.id == candidacy.vacancy_id, Vacancy.tenant_id == tenant_id))
        vacancy = (await session.execute(v_stmt)).scalar_one_or_none()

        # Obtener perfil
        p_stmt = select(UserProfile).where(and_(UserProfile.id == candidacy.profile_id, UserProfile.tenant_id == tenant_id))
        profile = (await session.execute(p_stmt)).scalar_one_or_none()

        # Obtener documentos vinculados (PDF y DOCX)
        doc_stmt = (
            select(CandidacyDocument)
            .where(CandidacyDocument.candidacy_id == candidacy_id)
            .order_by(CandidacyDocument.created_at.desc())
        )
        docs = (await session.execute(doc_stmt)).scalars().all()
        doc_info = [
            {
                "document_type": d.document_type,
                "file_hash": d.file_hash,
                "download_url": f"/api/v1/candidacies/{candidacy.id}/export/{d.document_type.replace('cv_', '')}",
            }
            for d in docs
        ]

        # Validar URL contra SSRF
        target_url = candidacy.target_url or (vacancy.source_url if vacancy else "")
        is_safe, url_reason = validate_url(target_url) if target_url else (False, "URL no definida")

        # Preparar respuestas típicas de screening (expectativas salariales, disponibilidad, etc.)
        form_resp = candidacy.form_responses or {}
        salary_exp = form_resp.get("salary_expectation") or (vacancy.salary_min if vacancy and vacancy.salary_min else "A convenir")
        avail_str = profile.availability if profile and profile.availability else "Inmediata"

        # Formatear checklist de pasos asistidos
        checklist = [
            {
                "id": "step_open_url",
                "step": 1,
                "title": "Abrir publicación en LinkedIn",
                "description": "Abre la vacante en una nueva pestaña del navegador.",
                "url": target_url if is_safe else None,
                "is_safe_url": is_safe,
            },
            {
                "id": "step_download_cv",
                "step": 2,
                "title": "Descargar y adjuntar CV especializado",
                "description": "Descarga tu CV adaptado generado con verificación de integridad.",
                "documents": doc_info,
            },
            {
                "id": "step_copy_letter",
                "step": 3,
                "title": "Copiar carta de presentación / nota personalizada",
                "description": "Copia el texto personalizado generado para este puesto.",
                "content": candidacy.cover_letter or "Estimado equipo de selección...",
            },
            {
                "id": "step_answer_questions",
                "step": 4,
                "title": "Respuestas de filtrado (Screening)",
                "description": "Usa las respuestas verificadas preparadas.",
                "answers": {
                    "Expectativa salarial": str(salary_exp),
                    "Disponibilidad": str(avail_str),
                    "Teléfono": profile.phone if profile else "No especificado",
                    "Email": profile.email if profile else "No especificado",
                    "Ubicación": f"{profile.location_city}, {profile.location_country}" if profile else "No especificada",
                },
            },
            {
                "id": "step_confirm_done",
                "step": 5,
                "title": "Confirmar envío de la postulación",
                "description": "Tras enviar la solicitud en LinkedIn, confirma el envío para actualizar el CRM.",
            },
        ]

        return {
            "candidacy_id": candidacy.id,
            "vacancy_id": vacancy.id if vacancy else None,
            "job_title": vacancy.job_title if vacancy else "Vacante",
            "company_name": vacancy.company_name if vacancy else "Empresa",
            "target_url": target_url,
            "is_safe_url": is_safe,
            "url_reason": url_reason if not is_safe else "OK",
            "candidacy_status": candidacy.status,
            "is_confirmed": candidacy.status == "sent_confirmed",
            "cover_letter": candidacy.cover_letter,
            "documents": doc_info,
            "checklist": checklist,
        }

    @classmethod
    async def confirm_submission(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        confirmation_note: Optional[str] = None,
        external_reference: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Confirma que el usuario envió manualmente la postulación en LinkedIn (REQ-024).
        Requiere aprobación previa obligatoria registrada.
        Registra la declaración manual del usuario distinguiéndola de evidencias verificadas del portal.
        """
        stmt = (
            select(Candidacy)
            .where(and_(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id))
        )
        candidacy = (await session.execute(stmt)).scalar_one_or_none()
        if not candidacy:
            raise LinkedInAssistedError(f"Candidatura '{candidacy_id}' no encontrada en el tenant.")

        # Aprobación previa obligatoria (ADR-003, REQ-014, Regla de desarrollo #2)
        appr_stmt = select(Approval).where(
            and_(
                Approval.candidacy_id == candidacy_id,
                Approval.tenant_id == tenant_id,
                Approval.decision == "approved",
                Approval.invalidated_at.is_(None),
            )
        )
        approval = (await session.execute(appr_stmt)).scalars().first()
        if not approval:
            raise LinkedInAssistedError(
                f"Aprobación obligatoria: no se puede confirmar la postulación de la candidatura '{candidacy_id}' "
                f"sin un registro de aprobación previo, explícito y no invalidado."
            )

        old_status = candidacy.status
        if old_status not in ("approved", "scheduled", "executing", "requires_intervention"):
            raise LinkedInAssistedError(
                f"No se puede confirmar postulación en estado actual: '{old_status}'. "
                f"Debe haber sido previamente aprobada y encontrarse en estado 'approved', 'scheduled', 'executing' o 'requires_intervention'."
            )

        now_utc = datetime.now(timezone.utc)
        candidacy.status = "sent_confirmed"

        # 1. Actualizar ScheduledTask si existe alguna asociada
        task_stmt = select(ScheduledTask).where(
            and_(
                ScheduledTask.candidacy_id == candidacy_id,
                ScheduledTask.tenant_id == tenant_id,
            )
        )
        task = (await session.execute(task_stmt)).scalars().first()
        if task:
            task.status = "completed"
            task.locked_by = None
            task.lease_expires_at = None

        # 2. Actualizar CRM distinguiendo declaración manual de evidencia del portal
        crm_stmt = select(CRMEntry).where(
            and_(
                CRMEntry.candidacy_id == candidacy_id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = (await session.execute(crm_stmt)).scalar_one_or_none()
        if crm:
            crm.current_status = "sent_confirmed"
            ref_text = f" (Ref: {external_reference})" if external_reference else ""
            crm.notes = (
                f"{crm.notes or ''}\n[Declaración de Envío Manual en LinkedIn - Sin Evidencia Automatizada del Portal]{ref_text}: "
                f"{confirmation_note or 'Envío manual declarado por el usuario'}."
            )
            event = CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="manual_external_submission_declared",
                old_status=old_status,
                new_status="sent_confirmed",
                details={
                    "portal": "linkedin",
                    "mode": "assisted_manual_declaration",
                    "declaration_type": "user_attestation",
                    "has_automated_portal_evidence": False,
                    "external_reference": external_reference or "manual_confirm",
                    "note": confirmation_note or "Declaración manual de postulación asistida.",
                    "approval_id": approval.id,
                    "confirmed_at_utc": now_utc.isoformat(),
                },
            )
            session.add(event)

        # 3. Registrar log de ejecución con resultado explícito de atestación humana
        log = ExecutionLog(
            tenant_id=tenant_id,
            task_id=task.id if task else None,
            candidacy_id=candidacy.id,
            action="linkedin_manual_user_declaration",
            result="manual_attestation",
            evidence={
                "portal": "linkedin",
                "mode": "assisted_manual_declaration",
                "declaration_type": "user_attestation",
                "has_automated_portal_evidence": False,
                "external_reference": external_reference,
                "note": confirmation_note,
                "previous_status": old_status,
                "approval_id": approval.id,
            },
            started_at=now_utc,
            completed_at=now_utc,
            duration_ms=0,
        )
        session.add(log)
        await session.commit()
        await session.refresh(candidacy)

        return {
            "status": "confirmed",
            "candidacy_id": candidacy.id,
            "new_status": candidacy.status,
            "confirmed_at": now_utc.isoformat(),
            "external_reference": external_reference,
            "declaration_type": "user_attestation",
            "has_automated_portal_evidence": False,
            "message": "Declaración de postulación manual en LinkedIn registrada exitosamente en CRM (sin evidencia automatizada del portal).",
        }
