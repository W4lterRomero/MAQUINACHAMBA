"""
Servicio del ciclo obligatorio de preparación, aprobación y control de cambios.
Aprobación inmutable, versionado y reversión ante modificaciones no autorizadas.
"""

from datetime import datetime, time, timezone
import json
from typing import Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import Clock, get_clock
from backend.app.core.security import hash_content
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.scheduling.service import SchedulingService


class ApprovalService:
    """Gestiona el ciclo de vida de candidaturas, aprobaciones y tareas programadas."""

    @classmethod
    def compute_candidacy_hash(cls, candidacy: Candidacy) -> str:
        """
        Calcula el hash determinista del paquete completo de postulación.
        Cualquier variación en el contenido genera un hash diferente.
        """
        return hash_content(
            candidacy.id,
            candidacy.vacancy_id,
            json.dumps(candidacy.adapted_cv_snapshot, sort_keys=True, ensure_ascii=False),
            candidacy.cover_letter,
            json.dumps(candidacy.form_responses, sort_keys=True, ensure_ascii=False),
            candidacy.target_portal,
            candidacy.target_url,
            json.dumps(candidacy.planned_actions, sort_keys=True, ensure_ascii=False),
        )

    @classmethod
    async def prepare_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile: UserProfile,
        cv: MasterCV,
        vacancy: Vacancy,
        evaluation: Optional[Evaluation] = None,
        cover_letter: Optional[str] = None,
    ) -> Candidacy:
        """
        Fase de preparación: compila los documentos y paquete de postulación.
        ESTRICTAMENTE NINGÚN ENVÍO durante esta fase.
        """
        # Adaptación sintética del CV basada en las secciones del CV maestro
        adapted_cv = {
            "name": profile.full_name,
            "email": profile.email,
            "title": cv.specialization or vacancy.job_title,
            "summary": cv.summary or f"Profesional especializado en {cv.specialization}",
            "sections": cv.sections or {},
            "version": cv.version,
        }

        letter = cover_letter or (
            f"Estimado equipo de {vacancy.company_name},\n\n"
            f"Me dirijo a ustedes con gran interés por la posición de {vacancy.job_title}. "
            f"Cuento con experiencia alineada a sus requerimientos técnicos.\n\n"
            f"Atentamente,\n{profile.full_name}"
        )

        planned_actions = [
            {"action": "authenticate_session", "target": vacancy.source},
            {"action": "fill_application_form", "fields": ["name", "email", "cv", "letter"]},
            {"action": "submit_candidacy", "method": "portal_mock"},
        ]

        candidacy = Candidacy(
            tenant_id=tenant_id,
            vacancy_id=vacancy.id,
            profile_id=profile.id,
            cv_id=cv.id,
            evaluation_id=evaluation.id if evaluation else None,
            adapted_cv_snapshot=adapted_cv,
            cover_letter=letter,
            form_responses={"applicant_email": profile.email, "salary_expectation": vacancy.salary_min or 0},
            target_portal=vacancy.source,
            target_url=vacancy.source_url,
            planned_actions=planned_actions,
            status="pending_approval",
            version=1,
        )
        session.add(candidacy)
        await session.flush()

        # Registro inicial en CRM
        crm_entry = CRMEntry(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            vacancy_id=vacancy.id,
            current_status="pending_approval",
            priority="medium",
            notes="Candidatura preparada, pendiente de revisión y aprobación humana.",
        )
        session.add(crm_entry)
        await session.flush()

        event = CRMEvent(
            tenant_id=tenant_id,
            crm_entry_id=crm_entry.id,
            event_type="prepared",
            old_status="draft",
            new_status="pending_approval",
            details={"vacancy": vacancy.job_title, "company": vacancy.company_name},
        )
        session.add(event)
        await session.flush()

        return candidacy

    @classmethod
    async def approve_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        user_timezone: str = "America/Mexico_City",
        window_start: time = time(9, 0),
        notes: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> Tuple[Approval, ScheduledTask]:
        """
        Aprobación explícita por el usuario.
        Genera registro inmutable de aprobación y programa la tarea para el día siguiente.
        """
        c = clock or get_clock()

        # 1. Obtener candidatura
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        if candidacy.status not in ("pending_approval", "postponed"):
            raise ValueError(f"No se puede aprobar una candidatura en estado: {candidacy.status}")

        # 2. Calcular hash inmutable del paquete
        content_hash = cls.compute_candidacy_hash(candidacy)

        # 3. Crear registro inmutable de aprobación
        approval = Approval(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            decision="approved",
            decided_by="user",
            decided_at=c.now_utc(),
            content_hash=content_hash,
            document_versions={
                "candidacy_version": candidacy.version,
                "cv_version": candidacy.adapted_cv_snapshot.get("version", 1),
            },
            notes=notes,
        )
        session.add(approval)
        await session.flush()

        # 4. Programar para el ciclo del día siguiente
        scheduled_date, scheduled_time, exec_utc = SchedulingService.calculate_next_day_execution(
            user_timezone=user_timezone,
            execution_window_start=window_start,
            clock=c,
        )
        idempotency_key = SchedulingService.generate_idempotency_key(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            scheduled_date=scheduled_date,
            version=candidacy.version,
        )

        task = ScheduledTask(
            tenant_id=tenant_id,
            approval_id=approval.id,
            candidacy_id=candidacy.id,
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
            execution_timezone=user_timezone,
            execution_instant_utc=exec_utc,
            status="pending",
            idempotency_key=idempotency_key,
            attempts=0,
            max_attempts=1,
        )
        session.add(task)

        # 5. Actualizar candidatura y CRM
        candidacy.status = "scheduled"

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "scheduled"
            crm.next_action = f"Ejecución automática programada para {scheduled_date.isoformat()} {scheduled_time.isoformat()}"
            crm.next_action_date = scheduled_date

            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="approved_and_scheduled",
                old_status=old_st,
                new_status="scheduled",
                details={
                    "scheduled_date": scheduled_date.isoformat(),
                    "instant_utc": exec_utc.isoformat(),
                    "content_hash": content_hash,
                },
            ))

        await session.flush()
        return approval, task

    @classmethod
    async def reject_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        reason: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> Approval:
        """Rechazo explícito por el usuario. Bloquea permanentemente el envío."""
        c = clock or get_clock()
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        content_hash = cls.compute_candidacy_hash(candidacy)
        approval = Approval(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            decision="rejected",
            decided_by="user",
            decided_at=c.now_utc(),
            content_hash=content_hash,
            notes=reason,
        )
        session.add(approval)

        candidacy.status = "rejected_by_user"

        # Cancelar cualquier tarea programada
        tasks_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "pending",
            )
        )
        for t in tasks_res.scalars():
            t.status = "cancelled"

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "rejected_by_user"
            crm.notes = f"Rechazada por el usuario: {reason or 'Sin nota'}"
            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="rejected_by_user",
                old_status=old_st,
                new_status="rejected_by_user",
                details={"reason": reason},
            ))

        await session.flush()
        return approval

    @classmethod
    async def modify_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        new_cover_letter: Optional[str] = None,
        new_cv_snapshot: Optional[dict] = None,
        clock: Optional[Clock] = None,
    ) -> Candidacy:
        """
        Regla crítica: Cualquier modificación en el contenido que se enviará
        INVALIDA inmediatamente una aprobación previa y cancela su tarea.
        """
        c = clock or get_clock()
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        # Invalidar aprobaciones previas activas
        approvals_res = await session.execute(
            select(Approval).where(
                Approval.candidacy_id == candidacy.id,
                Approval.tenant_id == tenant_id,
                Approval.decision == "approved",
                Approval.invalidated_at.is_(None),
            )
        )
        for app in approvals_res.scalars():
            app.invalidated_at = c.now_utc()
            app.invalidation_reason = "Contenido modificado; requiere nueva revisión y aprobación."

        # Cancelar tareas programadas activas
        tasks_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "pending",
            )
        )
        for t in tasks_res.scalars():
            t.status = "cancelled"

        # Aplicar cambios y devolver a revisión
        if new_cover_letter is not None:
            candidacy.cover_letter = new_cover_letter
        if new_cv_snapshot is not None:
            candidacy.adapted_cv_snapshot = new_cv_snapshot

        candidacy.version += 1
        candidacy.status = "pending_approval"

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "pending_approval"
            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="approval_invalidated_by_edit",
                old_status=old_st,
                new_status="pending_approval",
                details={"version": candidacy.version, "reason": "Edit invalidated approval"},
            ))

        await session.flush()
        return candidacy
