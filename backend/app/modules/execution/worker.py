"""
Worker de ejecución duradera para tareas en cola.
Verificación de autorización en tiempo de ejecución, locking atómico y evidencia.
"""

from datetime import datetime, time, timezone
import logging
from typing import Optional, Tuple
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import Clock, get_clock
from backend.app.core.security import AuthorizationPolicy
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.scheduling.service import SchedulingService
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter

logger = logging.getLogger("maquinachamba.worker")


class DurableExecutionWorker:
    """Worker responsable de procesar la cola persistente de tareas autorizadas."""

    def __init__(
        self,
        worker_id: str = "worker-1",
        clock: Optional[Clock] = None,
        portal_adapter: Optional[MockPortalAdapter] = None,
    ):
        self.worker_id = worker_id
        self.clock = clock or get_clock()
        self.portal = portal_adapter or MockPortalAdapter()

    async def claim_task(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> Optional[ScheduledTask]:
        """
        Intenta bloquear atómicamente una tarea pendiente.
        Previene que dos workers ejecuten simultáneamente la misma postulación.
        """
        now = self.clock.now_utc()
        stmt = (
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.status == "pending",
                ScheduledTask.locked_by.is_(None),
            )
            .values(
                locked_by=self.worker_id,
                locked_at=now,
                status="executing",
                attempts=ScheduledTask.attempts + 1,
            )
            .execution_options(synchronize_session="fetch")
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            return None  # Tarea ya tomada por otro worker o no pendiente

        await session.flush()
        # Obtener tarea actualizada
        task_res = await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        return task_res.scalar_one_or_none()

    async def execute_task(
        self,
        session: AsyncSession,
        task: ScheduledTask,
        window_end: time = time(18, 0),
    ) -> Tuple[bool, str]:
        """
        Ejecuta una tarea reclamada siguiendo todos los controles de seguridad y autorización.
        """
        start_dt = self.clock.now_utc()
        now_local = self.clock.now(task.execution_timezone)

        # 1. Comprobar ventana perdida (si el equipo estuvo apagado y la hora pasó)
        if SchedulingService.is_missed_window(task, execution_window_end=window_end, clock=self.clock):
            task.status = "missed_window"
            task.locked_by = None

            # Actualizar CRM
            crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == task.candidacy_id))
            crm = crm_res.scalar_one_or_none()
            if crm:
                old_st = crm.current_status
                crm.current_status = "missed_window"
                crm.notes = "Ventana de ejecución perdida (equipo inactivo). No se envía a hora inesperada."
                session.add(CRMEvent(
                    tenant_id=task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="missed_window",
                    old_status=old_st,
                    new_status="missed_window",
                    details={"date": task.scheduled_date.isoformat()},
                ))
            await session.flush()
            return False, "Ventana de ejecución perdida; tarea retenida sin envío."

        # 2. Cargar Candidacy y Approval
        candidacy_res = await session.execute(
            select(Candidacy).where(Candidacy.id == task.candidacy_id)
        )
        candidacy = candidacy_res.scalar_one_or_none()
        if not candidacy:
            task.status = "failed"
            await session.flush()
            return False, "Candidatura no encontrada."

        approval_res = await session.execute(
            select(Approval).where(Approval.id == task.approval_id)
        )
        approval = approval_res.scalar_one_or_none()
        if not approval:
            task.status = "failed"
            await session.flush()
            return False, "Registro de aprobación no encontrado."

        # 3. Verificación de autorización en tiempo de ejecución (INDEPENDIENTE DEL MODELO)
        current_hash = ApprovalService.compute_candidacy_hash(candidacy)
        allowed, reason = AuthorizationPolicy.can_execute_candidacy(
            approval_decision=approval.decision,
            approval_content_hash=approval.content_hash,
            current_content_hash=current_hash,
            approval_invalidated=(approval.invalidated_at is not None),
            simulation_mode=False,  # Evaluamos si la aprobación en sí es válida
        )

        if not allowed:
            task.status = "failed"
            task.locked_by = None
            log = ExecutionLog(
                tenant_id=task.tenant_id,
                task_id=task.id,
                candidacy_id=candidacy.id,
                action="authorize_execution",
                result="failure",
                error_message=f"Bloqueo de seguridad: {reason}",
                started_at=start_dt,
                completed_at=self.clock.now_utc(),
                duration_ms=0,
            )
            session.add(log)
            await session.flush()
            return False, f"Ejecución bloqueada por política: {reason}"

        # 4. Despacho al portal (simulado en Fase 1)
        sub_result = await self.portal.submit_application(
            tenant_id=task.tenant_id,
            target_url=candidacy.target_url,
            applicant_data={"email": candidacy.form_responses.get("applicant_email", "")},
            cover_letter=candidacy.cover_letter,
            cv_payload=candidacy.adapted_cv_snapshot,
            actions=candidacy.planned_actions,
        )

        completed_dt = self.clock.now_utc()
        duration = int((completed_dt - start_dt).total_seconds() * 1000)

        # 5. Registro inmutable de evidencia
        log = ExecutionLog(
            tenant_id=task.tenant_id,
            task_id=task.id,
            candidacy_id=candidacy.id,
            action="submit_application",
            result="success" if sub_result.success else "failure",
            evidence=sub_result.evidence,
            error_message=sub_result.error_message,
            started_at=start_dt,
            completed_at=completed_dt,
            duration_ms=duration,
        )
        session.add(log)

        # 6. Actualización de estado final en cola y CRM
        if sub_result.success:
            task.status = "completed"
            candidacy.status = "sent_confirmed"

            crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id))
            crm = crm_res.scalar_one_or_none()
            if crm:
                old_st = crm.current_status
                crm.current_status = "sent_confirmed"
                crm.next_action = "Esperar respuesta de la empresa o programar seguimiento."
                session.add(CRMEvent(
                    tenant_id=task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="submission_confirmed",
                    old_status=old_st,
                    new_status="sent_confirmed",
                    details={
                        "confirmation": sub_result.confirmation_number,
                        "portal": self.portal.PORTAL_NAME,
                    },
                ))
            await session.flush()
            return True, f"Postulación enviada exitosamente. Recibo: {sub_result.confirmation_number}"
        else:
            task.status = "failed"
            await session.flush()
            return False, f"Fallo en portal: {sub_result.error_message}"
