"""
Worker de ejecución duradera para tareas en cola.
Verificación de autorización en tiempo de ejecución, locking atómico,
prevención de ejecución anticipada, recuperación tras caída y evidencia.
"""

from datetime import datetime, time, timedelta, timezone
import logging
from typing import Any, Optional, Tuple
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import Clock, get_clock
from backend.app.core.config import get_settings
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
        portal_adapter: Optional[Any] = None,
        simulation_mode: Optional[bool] = None,
    ):
        self.worker_id = worker_id
        self.clock = clock or get_clock()
        self.portal = portal_adapter or MockPortalAdapter()
        self.simulation_mode = (
            simulation_mode if simulation_mode is not None else get_settings().simulation_mode
        )

    async def claim_task(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> Optional[ScheduledTask]:
        """
        Intenta bloquear atómicamente una tarea pendiente debidamente vencida.
        Previene ejecuciones anticipadas y que dos workers tomen la misma tarea.
        """
        now = self.clock.now_utc()
        stmt = (
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.status == "pending",
                ScheduledTask.locked_by.is_(None),
                ScheduledTask.execution_instant_utc <= now,  # P0: Exige que el instante programado ya haya llegado
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
            return None  # Tarea no disponible (ya tomada, no pendiente o aún no vencida)

        await session.commit()
        # Obtener tarea actualizada desde BD
        task_res = await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        return task_res.scalar_one_or_none()

    async def execute_task(
        self,
        session: AsyncSession,
        task: ScheduledTask,
        window_start: time = time(9, 0),
        window_end: time = time(18, 0),
    ) -> Tuple[bool, str]:
        """
        Ejecuta una tarea previamente reclamada siguiendo todos los controles de seguridad y autorización.
        """
        start_dt = self.clock.now_utc()

        # 0. P0: Validar estado y propiedad persistidos (P0 - Reejecución sin propiedad)
        task_res = await session.execute(
            select(ScheduledTask).where(ScheduledTask.id == task.id)
        )
        persisted_task = task_res.scalar_one_or_none()
        if not persisted_task:
            return False, "Tarea no encontrada en base de datos."

        if persisted_task.status != "executing":
            return False, (
                f"Estado inválido para ejecución: '{persisted_task.status}'. "
                f"La tarea debe estar en estado 'executing' para despachar."
            )

        if persisted_task.locked_by != self.worker_id:
            return False, (
                f"Worker no autorizado: la tarea está bloqueada por '{persisted_task.locked_by}', "
                f"no por '{self.worker_id}'."
            )

        # 1. P0: Defensa contra ejecución anticipada (comienzo de ventana e instante UTC)
        if SchedulingService.is_premature(persisted_task, execution_window_start=window_start, clock=self.clock):
            return False, "Ejecución anticipada rechazada: la tarea aún no alcanza su ventana/instante de ejecución."

        # 2. Comprobar ventana perdida (fin de ventana si el equipo estuvo apagado)
        if SchedulingService.is_missed_window(persisted_task, execution_window_end=window_end, clock=self.clock):
            persisted_task.status = "missed_window"
            persisted_task.locked_by = None

            # Actualizar CRM
            crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == persisted_task.candidacy_id))
            crm = crm_res.scalar_one_or_none()
            if crm:
                old_st = crm.current_status
                crm.current_status = "missed_window"
                crm.notes = "Ventana de ejecución perdida (equipo inactivo). No se envía a hora inesperada."
                session.add(CRMEvent(
                    tenant_id=persisted_task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="missed_window",
                    old_status=old_st,
                    new_status="missed_window",
                    details={"date": persisted_task.scheduled_date.isoformat()},
                ))
            await session.commit()
            return False, "Ventana de ejecución perdida; tarea retenida sin envío."

        # 3. Cargar Candidacy y Approval
        candidacy_res = await session.execute(
            select(Candidacy).where(Candidacy.id == persisted_task.candidacy_id)
        )
        candidacy = candidacy_res.scalar_one_or_none()
        if not candidacy:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            await session.commit()
            return False, "Candidatura no encontrada."

        approval_res = await session.execute(
            select(Approval).where(Approval.id == persisted_task.approval_id)
        )
        approval = approval_res.scalar_one_or_none()
        if not approval:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            await session.commit()
            return False, "Registro de aprobación no encontrado."

        # 4. P1: Separación explícita de autorización de paquete y control de simulación
        # 4.1 Verificación de integridad del paquete aprobado (INDEPENDIENTE DEL MODELO)
        current_hash = ApprovalService.compute_candidacy_hash(candidacy)
        pkg_valid, pkg_reason = AuthorizationPolicy.validate_approval_package(
            approval_decision=approval.decision,
            approval_content_hash=approval.content_hash,
            current_content_hash=current_hash,
            approval_invalidated=(approval.invalidated_at is not None),
        )

        if not pkg_valid:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            log = ExecutionLog(
                tenant_id=persisted_task.tenant_id,
                task_id=persisted_task.id,
                candidacy_id=candidacy.id,
                action="authorize_execution",
                result="failure",
                error_message=f"Bloqueo de seguridad: {pkg_reason}",
                started_at=start_dt,
                completed_at=self.clock.now_utc(),
                duration_ms=0,
            )
            session.add(log)
            await session.commit()
            return False, f"Ejecución bloqueada por política: {pkg_reason}"

        # 4.2 Verificación de modo simulación vs capacidades del adaptador (P1 - Simulación)
        is_sim_adapter = getattr(self.portal, "simulation_mode", False) is True or getattr(self.portal, "CAPABILITIES", {}).get("mode") == "simulation_only"
        adapter_mode = getattr(self.portal, "CAPABILITIES", {}).get("mode")

        can_effect, reason_effect = AuthorizationPolicy.can_perform_live_effects(
            simulation_mode=self.simulation_mode,
            adapter_mode=adapter_mode,
            is_simulation_adapter=is_sim_adapter,
        )

        if not can_effect:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            log = ExecutionLog(
                tenant_id=persisted_task.tenant_id,
                task_id=persisted_task.id,
                candidacy_id=candidacy.id,
                action="check_simulation_safety",
                result="failure",
                error_message=f"Bloqueo de seguridad: {reason_effect}",
                started_at=start_dt,
                completed_at=self.clock.now_utc(),
                duration_ms=0,
            )
            session.add(log)
            await session.commit()
            return False, f"Bloqueo de seguridad: {reason_effect}"

        # 5. Despacho al portal (simulado en Fase 1)
        sub_result = await self.portal.submit_application(
            tenant_id=persisted_task.tenant_id,
            target_url=candidacy.target_url,
            applicant_data={"email": candidacy.form_responses.get("applicant_email", "")},
            cover_letter=candidacy.cover_letter,
            cv_payload=candidacy.adapted_cv_snapshot,
            actions=candidacy.planned_actions,
        )

        completed_dt = self.clock.now_utc()
        duration = int((completed_dt - start_dt).total_seconds() * 1000)

        # 6. Manejo de resultados: success, uncertain, failure
        # P1: Distinguir resultados inciertos y evitar reenvíos ciegos
        is_uncertain = (
            getattr(sub_result, "result_type", None) == "uncertain"
            or sub_result.status_code in (408, 504)
            or (sub_result.evidence and sub_result.evidence.get("uncertain") is True)
        )

        if is_uncertain:
            exec_result = "uncertain"
        elif sub_result.success:
            exec_result = "success"
        else:
            exec_result = "failure"

        # 7. Registro inmutable de evidencia
        log = ExecutionLog(
            tenant_id=persisted_task.tenant_id,
            task_id=persisted_task.id,
            candidacy_id=candidacy.id,
            action="submit_application",
            result=exec_result,
            evidence=sub_result.evidence,
            error_message=sub_result.error_message,
            started_at=start_dt,
            completed_at=completed_dt,
            duration_ms=duration,
        )
        session.add(log)

        # 8. Actualización de estado en cola y CRM
        crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id))
        crm = crm_res.scalar_one_or_none()

        if exec_result == "success":
            persisted_task.status = "completed"
            persisted_task.locked_by = None
            candidacy.status = "sent_confirmed"
            if crm:
                old_st = crm.current_status
                crm.current_status = "sent_confirmed"
                crm.next_action = "Esperar respuesta de la empresa o programar seguimiento."
                session.add(CRMEvent(
                    tenant_id=persisted_task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="submission_confirmed",
                    old_status=old_st,
                    new_status="sent_confirmed",
                    details={
                        "confirmation": sub_result.confirmation_number,
                        "portal": getattr(self.portal, "PORTAL_NAME", "mock_test_portal"),
                    },
                ))
            await session.commit()
            return True, f"Postulación enviada exitosamente. Recibo: {sub_result.confirmation_number}"

        elif exec_result == "uncertain":
            # P1: Resultado incierto -> requiere intervención, PROHIBIDO reenvío ciego
            persisted_task.status = "result_uncertain"
            persisted_task.locked_by = None
            candidacy.status = "result_uncertain"
            if crm:
                old_st = crm.current_status
                crm.current_status = "requires_intervention"
                crm.next_action = "Verificar estado en el portal manualmente. No reenviar a ciegas."
                crm.notes = f"Resultado incierto: {sub_result.error_message or 'Sin confirmación de recepción'}."
                session.add(CRMEvent(
                    tenant_id=persisted_task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="result_uncertain",
                    old_status=old_st,
                    new_status="requires_intervention",
                    details={
                        "status_code": sub_result.status_code,
                        "error": sub_result.error_message,
                    },
                ))
            await session.commit()
            return False, f"Resultado incierto en portal. Pasa a 'requires_intervention': {sub_result.error_message}"

        else:
            # Fallo definitivo
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            candidacy.status = "failed"
            if crm:
                old_st = crm.current_status
                crm.current_status = "failed"
                crm.next_action = "Revisar motivo de fallo y reprogramar si corresponde."
                session.add(CRMEvent(
                    tenant_id=persisted_task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="submission_failed",
                    old_status=old_st,
                    new_status="failed",
                    details={"error": sub_result.error_message},
                ))
            await session.commit()
            return False, f"Fallo en portal: {sub_result.error_message}"

    @classmethod
    async def recover_stale_tasks(
        cls,
        session: AsyncSession,
        stale_threshold_seconds: int = 300,
        clock: Optional[Clock] = None,
    ) -> list[dict]:
        """
        P1 - Recuperación tras caída:
        Inspecciona tareas que quedaron en estado 'executing' debido a caída o reinicio del worker.
        Distingue si la caída fue antes o después del efecto externo:
        - Si NO existe ExecutionLog (caída antes del efecto): reinicia la tarea a 'pending' para reintento seguro.
        - Si existe ExecutionLog con 'success': reconcilia el estado a 'completed' sin reenviar.
        - Si existe ExecutionLog con 'uncertain': marca 'result_uncertain' y CRM 'requires_intervention' (sin reenvío ciego).
        """
        c = clock or get_clock()
        now = c.now_utc()
        threshold_dt = now - timedelta(seconds=stale_threshold_seconds)

        stmt = select(ScheduledTask).where(
            ScheduledTask.status == "executing",
            ScheduledTask.locked_at < threshold_dt,
        )
        res = await session.execute(stmt)
        stale_tasks = res.scalars().all()
        recovered = []

        for task in stale_tasks:
            log_res = await session.execute(
                select(ExecutionLog)
                .where(ExecutionLog.task_id == task.id)
                .order_by(ExecutionLog.completed_at.desc())
            )
            last_log = log_res.scalars().first()

            if last_log is None:
                # Caída antes del efecto en portal: seguro reintentar
                task.status = "pending"
                task.locked_by = None
                task.locked_at = None
                recovered.append({
                    "task_id": task.id,
                    "action": "reset_to_pending",
                    "reason": "Crash detectado antes de despachar al portal; reintento seguro habilitado.",
                })
            elif last_log.result == "success":
                # Caída después del efecto: reconciliar sin duplicar envío
                task.status = "completed"
                task.locked_by = None
                recovered.append({
                    "task_id": task.id,
                    "action": "reconciled_completed",
                    "reason": "Efecto completado en portal antes de la caída; estado reconciliado sin reenvío.",
                })
            elif last_log.result == "uncertain":
                # Resultado incierto: requiere intervención manual, PROHIBIDO reenvío ciego
                task.status = "result_uncertain"
                task.locked_by = None
                recovered.append({
                    "task_id": task.id,
                    "action": "flag_requires_intervention",
                    "reason": "Resultado incierto registrado previo a caída; retenido para verificación manual.",
                })
            else:
                task.status = "failed"
                task.locked_by = None
                recovered.append({
                    "task_id": task.id,
                    "action": "marked_failed",
                    "reason": "Fallo registrado previo a caída.",
                })

        await session.commit()
        return recovered
