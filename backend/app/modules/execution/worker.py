"""
Worker de ejecución duradera para tareas en cola.
Verificación de autorización en tiempo de ejecución, locking atómico,
prevención de ejecución anticipada, fencing tokens, leases/heartbeats,
persistencia previa al despacho y recuperación estricta sin duplicados.
"""

from datetime import datetime, time, timedelta, timezone
import logging
from typing import Any, Dict, Optional, Tuple
import uuid
from sqlalchemy import select, and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import Clock, get_clock
from backend.app.core.config import get_settings
from backend.app.core.security import AuthorizationPolicy
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.scheduling.service import SchedulingService
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter, PortalSubmissionResult

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
        self.active_fence_tokens: Dict[str, int] = {}

    async def claim_task(
        self,
        session: AsyncSession,
        task_id: str,
        lease_seconds: int = 300,
        tenant_id: Optional[str] = None,
    ) -> Optional[ScheduledTask]:
        """
        Intenta bloquear atómicamente una tarea pendiente debidamente vencida.
        Previene ejecuciones anticipadas y asigna lease temporal con fencing token incremental.
        Filtra obligatoriamente por tenant_id si es proporcionado para aislamiento.
        """
        now = self.clock.now_utc()
        lease_expires = now + timedelta(seconds=lease_seconds)
        where_conditions = [
            ScheduledTask.id == task_id,
            ScheduledTask.status == "pending",
            ScheduledTask.locked_by.is_(None),
            ScheduledTask.execution_instant_utc <= now,  # P0: Exige que el instante programado ya haya llegado
        ]
        if tenant_id:
            where_conditions.append(ScheduledTask.tenant_id == tenant_id)

        stmt = (
            update(ScheduledTask)
            .where(*where_conditions)
            .values(
                locked_by=self.worker_id,
                locked_at=now,
                lease_expires_at=lease_expires,
                fence_token=ScheduledTask.fence_token + 1,
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
        fetch_conds = [ScheduledTask.id == task_id]
        if tenant_id:
            fetch_conds.append(ScheduledTask.tenant_id == tenant_id)
        task_res = await session.execute(select(ScheduledTask).where(*fetch_conds))
        claimed = task_res.scalar_one_or_none()
        if claimed:
            self.active_fence_tokens[task_id] = claimed.fence_token
        return claimed

    async def heartbeat(
        self,
        session: AsyncSession,
        task_id: str,
        lease_seconds: int = 300,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """
        Renueva el lease de una tarea en ejecución asegurando que el worker sea el dueño
        y que el fencing token no haya cambiado.
        """
        fence_token = self.active_fence_tokens.get(task_id)
        if fence_token is None:
            return False

        now = self.clock.now_utc()
        new_expires = now + timedelta(seconds=lease_seconds)
        where_conds = [
            ScheduledTask.id == task_id,
            ScheduledTask.locked_by == self.worker_id,
            ScheduledTask.fence_token == fence_token,
            ScheduledTask.status == "executing",
            or_(
                ScheduledTask.lease_expires_at.is_(None),
                ScheduledTask.lease_expires_at > now,
            ),
        ]
        if tenant_id:
            where_conds.append(ScheduledTask.tenant_id == tenant_id)

        stmt = (
            update(ScheduledTask)
            .where(*where_conds)
            .values(
                locked_at=now,
                lease_expires_at=new_expires,
            )
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        await session.commit()
        return res.rowcount > 0

    async def execute_task(
        self,
        session: AsyncSession,
        task: ScheduledTask,
        window_start: time = time(9, 0),
        window_end: time = time(18, 0),
        expected_fence_token: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Ejecuta una tarea previamente reclamada siguiendo todos los controles de seguridad,
        persistencia de intención previa al despacho, manejo de excepciones y fencing.
        """
        start_dt = self.clock.now_utc()
        task_id = str(task.id)
        task_tenant_id = str(task.tenant_id)

        # 0. P0: Validar estado y propiedad persistidos (P0 - Reejecución sin propiedad y aislamiento por tenant)
        task_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.id == task_id,
                ScheduledTask.tenant_id == task_tenant_id,
            )
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

        # Token de claim propio y activo: falla cerrado si el worker no lo posee.
        # PROHIBIDO adoptar el token persistido en BD como sustituto de un claim propio.
        my_fence_token = expected_fence_token if expected_fence_token is not None else self.active_fence_tokens.get(persisted_task.id)
        if my_fence_token is None:
            return False, (
                f"Worker '{self.worker_id}' no posee un claim activo (fencing token) para la tarea '{persisted_task.id}'. "
                f"Prohibido adoptar el token de base de datos como sustituto."
            )

        # 1. P0: Defensa contra ejecución anticipada (comienzo de ventana e instante UTC)
        if SchedulingService.is_premature(persisted_task, execution_window_start=window_start, clock=self.clock):
            return False, "Ejecución anticipada rechazada: la tarea aún no alcanza su ventana/instante de ejecución."

        # 2. Comprobar ventana perdida (fin de ventana si el equipo estuvo apagado)
        if SchedulingService.is_missed_window(persisted_task, execution_window_end=window_end, clock=self.clock):
            persisted_task.status = "missed_window"
            persisted_task.locked_by = None
            persisted_task.lease_expires_at = None

            # Actualizar CRM
            crm_res = await session.execute(
                select(CRMEntry).where(
                    CRMEntry.candidacy_id == persisted_task.candidacy_id,
                    CRMEntry.tenant_id == persisted_task.tenant_id,
                )
            )
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

        # 3. Cargar Candidacy y Approval con verificación de tenant_id
        candidacy_res = await session.execute(
            select(Candidacy).where(
                Candidacy.id == persisted_task.candidacy_id,
                Candidacy.tenant_id == persisted_task.tenant_id,
            )
        )
        candidacy = candidacy_res.scalar_one_or_none()
        if not candidacy:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            persisted_task.lease_expires_at = None
            await session.commit()
            return False, "Candidatura no encontrada."

        approval_res = await session.execute(
            select(Approval).where(
                Approval.id == persisted_task.approval_id,
                Approval.tenant_id == persisted_task.tenant_id,
            )
        )
        approval = approval_res.scalar_one_or_none()
        if not approval:
            persisted_task.status = "failed"
            persisted_task.locked_by = None
            persisted_task.lease_expires_at = None
            await session.commit()
            return False, "Registro de aprobación no encontrado."

        # 4. Verificación de autorización en tiempo de ejecución (INDEPENDIENTE DEL MODELO)
        # 4.1 Verificación de integridad del paquete aprobado
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
            persisted_task.lease_expires_at = None
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

        # 4.1.b Verificación de vigencia e integridad de la vacante en tiempo de ejecución (P1, REQ-009)
        v_res = await session.execute(
            select(Vacancy).where(
                Vacancy.id == candidacy.vacancy_id,
                Vacancy.tenant_id == persisted_task.tenant_id,
            )
        )
        vacancy = v_res.scalar_one_or_none()
        if vacancy:
            current_date = self.clock.now_utc().date()
            if vacancy.closing_date and vacancy.closing_date < current_date:
                actions = candidacy.planned_actions or []
                has_consent = any(a.get("action") == "user_exception_consent" for a in actions)
                if not has_consent:
                    persisted_task.status = "failed"
                    persisted_task.locked_by = None
                    persisted_task.lease_expires_at = None
                    log = ExecutionLog(
                        tenant_id=persisted_task.tenant_id,
                        task_id=persisted_task.id,
                        candidacy_id=candidacy.id,
                        action="validate_vacancy_validity",
                        result="failure",
                        error_message=f"Bloqueo previo a ejecución: la vacante venció ({vacancy.closing_date} < {current_date}) sin excepción autorizada.",
                        started_at=start_dt,
                        completed_at=self.clock.now_utc(),
                        duration_ms=0,
                    )
                    session.add(log)
                    await session.commit()
                    return False, f"Ejecución bloqueada: la vacante ha expirado ({vacancy.closing_date})."

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
            persisted_task.lease_expires_at = None
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

        # =========================================================================
        # 5. P0: TRANSICIÓN ATÓMICA DE PREDESPACHO
        # UPDATE condicional con verificación atómica de:
        # - status == "executing"
        # - locked_by == self.worker_id
        # - fence_token == my_fence_token
        # - dispatch_in_flight == False
        # - lease vigente (lease_expires_at > now o lease_expires_at is None)
        # Exige exactamente una fila modificada antes de invocar al adaptador.
        # =========================================================================
        dispatch_attempt_id = f"disp-{uuid.uuid4().hex[:12]}"
        now_pre = self.clock.now_utc()

        predispatch_stmt = (
            update(ScheduledTask)
            .where(
                ScheduledTask.id == persisted_task.id,
                ScheduledTask.tenant_id == persisted_task.tenant_id,
                ScheduledTask.status == "executing",
                ScheduledTask.locked_by == self.worker_id,
                ScheduledTask.fence_token == my_fence_token,
                ScheduledTask.dispatch_in_flight.is_(False),
                or_(
                    ScheduledTask.lease_expires_at.is_(None),
                    ScheduledTask.lease_expires_at > now_pre,
                ),
            )
            .values(
                dispatch_in_flight=True,
                last_dispatch_attempt_id=dispatch_attempt_id,
            )
            .execution_options(synchronize_session=False)
        )
        predispatch_res = await session.execute(predispatch_stmt)
        if predispatch_res.rowcount != 1:
            await session.rollback()
            self.active_fence_tokens.pop(task_id, None)
            logger.warning(
                "Predespacho abortado: worker %s no pudo asegurar el bloqueo condicional atómico para tarea %s (token %s).",
                self.worker_id,
                task_id,
                my_fence_token,
            )
            return False, "Fallo al iniciar predespacho: la tarea ya no está vigente para este worker o existe un despacho en vuelo concurrente."

        # Sincronizar atributos en memoria para la sesión activa
        persisted_task.dispatch_in_flight = True
        persisted_task.last_dispatch_attempt_id = dispatch_attempt_id
        task.dispatch_in_flight = True
        task.last_dispatch_attempt_id = dispatch_attempt_id

        in_progress_log = ExecutionLog(
            tenant_id=persisted_task.tenant_id,
            task_id=persisted_task.id,
            candidacy_id=candidacy.id,
            action="submit_application",
            result="in_progress",
            evidence={
                "dispatch_attempt_id": dispatch_attempt_id,
                "idempotency_key": persisted_task.idempotency_key,
                "worker_id": self.worker_id,
                "fence_token": my_fence_token,
            },
            error_message=None,
            started_at=start_dt,
            completed_at=None,
            duration_ms=None,
        )
        session.add(in_progress_log)
        await session.commit()

        # =========================================================================
        # 6. DESPACHO CON MANEJO DE EXCEPCIONES DE RED/TRANSPORTE
        # Las excepciones de transporte se tratan conservadoramente como 'uncertain'
        # porque no es posible descartar que el portal haya recibido el paquete.
        # =========================================================================
        sub_result = None
        transport_error = None
        try:
            sub_result = await self.portal.submit_application(
                tenant_id=persisted_task.tenant_id,
                target_url=candidacy.target_url,
                applicant_data={"email": candidacy.form_responses.get("applicant_email", "")},
                cover_letter=candidacy.cover_letter,
                cv_payload=candidacy.adapted_cv_snapshot,
                actions=candidacy.planned_actions,
                idempotency_key=persisted_task.idempotency_key,
            )
        except Exception as exc:
            logger.exception("Excepción de transporte durante submit_application: %s", exc)
            transport_error = exc

        completed_dt = self.clock.now_utc()
        duration = int((completed_dt - start_dt).total_seconds() * 1000)

        # =========================================================================
        # 7. DETERMINACIÓN DE RESULTADO (success, failure, uncertain)
        # =========================================================================
        if transport_error is not None:
            exec_result = "uncertain"
            evidence_data = {
                "dispatch_attempt_id": dispatch_attempt_id,
                "idempotency_key": persisted_task.idempotency_key,
                "exception": type(transport_error).__name__,
                "details": str(transport_error),
                "uncertain": True,
            }
            error_msg = f"Excepción de transporte/red: {type(transport_error).__name__}: {str(transport_error)}"
            conf_number = ""
        else:
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

            evidence_data = sub_result.evidence or {}
            evidence_data["dispatch_attempt_id"] = dispatch_attempt_id
            error_msg = sub_result.error_message
            conf_number = sub_result.confirmation_number

        # =========================================================================
        # 8. TRANSICIÓN ATÓMICA DE FINALIZACIÓN
        # Comprobar atómicamente:
        # - id == persisted_task.id
        # - status == "executing"
        # - locked_by == self.worker_id
        # - fence_token == my_fence_token
        # - dispatch_in_flight == True
        # - last_dispatch_attempt_id == dispatch_attempt_id
        # =========================================================================
        if exec_result == "success":
            target_task_status = "completed"
        elif exec_result == "uncertain":
            target_task_status = "result_uncertain"
        else:
            target_task_status = "failed"

        final_task_stmt = (
            update(ScheduledTask)
            .where(
                ScheduledTask.id == persisted_task.id,
                ScheduledTask.tenant_id == persisted_task.tenant_id,
                ScheduledTask.status == "executing",
                ScheduledTask.locked_by == self.worker_id,
                ScheduledTask.fence_token == my_fence_token,
                ScheduledTask.dispatch_in_flight.is_(True),
                ScheduledTask.last_dispatch_attempt_id == dispatch_attempt_id,
            )
            .values(
                status=target_task_status,
                locked_by=None,
                lease_expires_at=None,
                dispatch_in_flight=False,
            )
            .execution_options(synchronize_session=False)
        )
        final_res = await session.execute(final_task_stmt)
        if final_res.rowcount != 1:
            await session.rollback()
            self.active_fence_tokens.pop(task_id, None)
            try:
                await session.refresh(task)
            except Exception:
                pass
            logger.warning(
                "Worker %s descartado por token de fencing u ownership obsoleto al finalizar tarea %s (token: %s)",
                self.worker_id,
                task_id,
                my_fence_token,
            )
            return False, (
                f"Worker {self.worker_id} descartado al finalizar: "
                f"token de fencing obsoleto (perdió lease o titularidad durante el despacho)."
            )

        # Sincronizar atributos en memoria para la sesión activa
        persisted_task.status = target_task_status
        persisted_task.locked_by = None
        persisted_task.lease_expires_at = None
        persisted_task.dispatch_in_flight = False
        task.status = target_task_status
        task.locked_by = None
        task.lease_expires_at = None
        task.dispatch_in_flight = False

        # =========================================================================
        # 9. SINCRONIZACIÓN (ExecutionLog, Candidacy, CRM y Eventos con tenant_id)
        # =========================================================================
        log_res = await session.execute(
            select(ExecutionLog)
            .where(
                ExecutionLog.task_id == persisted_task.id,
                ExecutionLog.tenant_id == persisted_task.tenant_id,
                ExecutionLog.action == "submit_application",
                ExecutionLog.started_at == start_dt,
            )
        )
        exec_log = log_res.scalar_one_or_none()
        if exec_log:
            exec_log.result = exec_result
            exec_log.completed_at = completed_dt
            exec_log.duration_ms = duration
            exec_log.evidence = evidence_data
            exec_log.error_message = error_msg
        else:
            session.add(ExecutionLog(
                tenant_id=persisted_task.tenant_id,
                task_id=persisted_task.id,
                candidacy_id=candidacy.id,
                action="submit_application",
                result=exec_result,
                evidence=evidence_data,
                error_message=error_msg,
                started_at=start_dt,
                completed_at=completed_dt,
                duration_ms=duration,
            ))

        cand_res = await session.execute(
            select(Candidacy).where(
                Candidacy.id == persisted_task.candidacy_id,
                Candidacy.tenant_id == persisted_task.tenant_id,
            )
        )
        cand_obj = cand_res.scalar_one_or_none()

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == persisted_task.candidacy_id,
                CRMEntry.tenant_id == persisted_task.tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()

        if exec_result == "success":
            if cand_obj:
                cand_obj.status = "sent_confirmed"
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
                        "confirmation": conf_number,
                        "portal": getattr(self.portal, "PORTAL_NAME", "mock_test_portal"),
                        "dispatch_attempt_id": dispatch_attempt_id,
                    },
                ))
            await session.commit()
            self.active_fence_tokens.pop(persisted_task.id, None)
            return True, f"Postulación enviada exitosamente. Recibo: {conf_number}"

        elif exec_result == "uncertain":
            # P0/P1: Resultado incierto -> requiere intervención, PROHIBIDO reenvío ciego
            if cand_obj:
                cand_obj.status = "result_uncertain"
            if crm:
                old_st = crm.current_status
                crm.current_status = "requires_intervention"
                crm.next_action = "Verificar estado en el portal manualmente. No reenviar a ciegas."
                crm.notes = f"Resultado incierto durante despacho ({dispatch_attempt_id}): {error_msg or 'Sin confirmación de recepción'}."
                session.add(CRMEvent(
                    tenant_id=persisted_task.tenant_id,
                    crm_entry_id=crm.id,
                    event_type="result_uncertain",
                    old_status=old_st,
                    new_status="requires_intervention",
                    details={
                        "error": error_msg,
                        "dispatch_attempt_id": dispatch_attempt_id,
                    },
                ))
            await session.commit()
            self.active_fence_tokens.pop(persisted_task.id, None)
            return False, f"Resultado incierto en portal ({dispatch_attempt_id}). Pasa a 'requires_intervention': {error_msg}"

        else:
            # Fallo definitivo
            if cand_obj:
                cand_obj.status = "failed"
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
                    details={"error": error_msg},
                ))
            await session.commit()
            self.active_fence_tokens.pop(persisted_task.id, None)
            return False, f"Fallo en portal: {error_msg}"

    @classmethod
    async def recover_stale_tasks(
        cls,
        session: AsyncSession,
        stale_threshold_seconds: int = 300,
        clock: Optional[Clock] = None,
        tenant_id: Optional[str] = None,
    ) -> list[dict]:
        """
        P0/P1 - Recuperación tras caída con prevención estricta de duplicados:
        - Solo recupera tareas cuyo lease haya expirado (lease_expires_at < now).
          Un worker activo dentro de su lease NO es interrumpido.
        - Para cada tarea recuperada, incrementa fence_token para descartar (fence out)
          inmediatamente a cualquier worker zombi o lento.
        - Si el despacho estuvo en vuelo (dispatch_in_flight=True) o hay un log in_progress/uncertain
          o la tarea ya tuvo intentos previos (attempts > 0 sin descarte seguro):
          RETENER como result_uncertain y CRM requires_intervention. NUNCA volver a pending.
        - Si existe un ExecutionLog confirmado con result == 'success':
          Reconciliar a 'completed' sin reenvío.
        - Si de forma verificable no hubo despacho previo (ej. attempts == 0 y sin logs):
          reset_to_pending.
        - Aislamiento multi-tenant: si se provee tenant_id, solo recupera tareas de ese tenant.
        """
        c = clock or get_clock()
        now = c.now_utc()
        threshold_dt = now - timedelta(seconds=stale_threshold_seconds)

        where_conds = [
            ScheduledTask.status == "executing",
            or_(
                and_(ScheduledTask.lease_expires_at.is_not(None), ScheduledTask.lease_expires_at < now),
                and_(ScheduledTask.lease_expires_at.is_(None), ScheduledTask.locked_at < threshold_dt),
            ),
        ]
        if tenant_id:
            where_conds.append(ScheduledTask.tenant_id == tenant_id)

        stmt = select(ScheduledTask).where(*where_conds)
        res = await session.execute(stmt)
        stale_tasks = res.scalars().all()
        recovered = []

        for task in stale_tasks:
            current_fence_token = task.fence_token
            new_fence_token = current_fence_token + 1

            # Inspeccionar el historial de logs con aislamiento por tenant_id
            log_res = await session.execute(
                select(ExecutionLog)
                .where(
                    ExecutionLog.task_id == task.id,
                    ExecutionLog.tenant_id == task.tenant_id,
                )
                .order_by(ExecutionLog.started_at.desc())
            )
            logs = log_res.scalars().all()
            last_log = logs[0] if logs else None

            # Determinar acción y estados de recuperación
            # Caso 1: Log previo confirma éxito
            if last_log and last_log.result == "success":
                target_status = "completed"
                cand_target_status = "sent_confirmed"
                crm_target_status = "sent_confirmed"
                rec_action = "reconciled_completed"
                rec_reason = "Efecto completado en portal antes de la caída; estado reconciliado sin reenvío."

            # Caso 2: Intento de despacho en vuelo, log in_progress/uncertain, o incertidumbre
            elif (
                task.dispatch_in_flight
                or (last_log and last_log.result in ("in_progress", "uncertain"))
                or (last_log is None and task.attempts > 0)
            ):
                target_status = "result_uncertain"
                cand_target_status = "result_uncertain"
                crm_target_status = "requires_intervention"
                rec_action = "flag_requires_intervention"
                rec_reason = "Despacho en vuelo interrumpido o sin confirmación concluyente; retenido para intervención manual."

            # Caso 3: Fallo explícito registrado
            elif last_log and last_log.result == "failure":
                target_status = "failed"
                cand_target_status = "failed"
                crm_target_status = "failed"
                rec_action = "marked_failed"
                rec_reason = "Fallo registrado previo a caída."

            # Caso 4: Jamás hubo intento de despacho ni logs y attempts == 0
            else:
                target_status = "pending"
                cand_target_status = None
                crm_target_status = None
                rec_action = "reset_to_pending"
                rec_reason = "Tarea bloqueada pero sin ningún intento de despacho iniciado; reintento seguro."

            # Transición ATÓMICA sobre ScheduledTask con tenant_id
            rec_stmt = (
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == task.id,
                    ScheduledTask.tenant_id == task.tenant_id,
                    ScheduledTask.fence_token == current_fence_token,
                    ScheduledTask.status == "executing",
                )
                .values(
                    status=target_status,
                    fence_token=new_fence_token,
                    locked_by=None,
                    locked_at=None if target_status == "pending" else task.locked_at,
                    lease_expires_at=None,
                    dispatch_in_flight=False,
                )
                .execution_options(synchronize_session=False)
            )
            rec_res = await session.execute(rec_stmt)
            if rec_res.rowcount != 1:
                # La tarea ya fue modificada por otro proceso o recuperación concurrente
                logger.info("Recuperación saltó tarea %s por colisión concurrente de token.", task.id)
                continue

            # Sincronizar atributos en memoria para la sesión activa
            task.status = target_status
            task.fence_token = new_fence_token
            task.locked_by = None
            task.locked_at = None if target_status == "pending" else task.locked_at
            task.lease_expires_at = None
            task.dispatch_in_flight = False

            # Sincronizar Candidacy y CRM si aplica
            if cand_target_status:
                cand_res = await session.execute(
                    select(Candidacy).where(
                        Candidacy.id == task.candidacy_id,
                        Candidacy.tenant_id == task.tenant_id,
                    )
                )
                candidacy = cand_res.scalar_one_or_none()
                if candidacy:
                    candidacy.status = cand_target_status

            if crm_target_status:
                crm_res = await session.execute(
                    select(CRMEntry).where(
                        CRMEntry.candidacy_id == task.candidacy_id,
                        CRMEntry.tenant_id == task.tenant_id,
                    )
                )
                crm = crm_res.scalar_one_or_none()
                if crm:
                    old_st = crm.current_status
                    crm.current_status = crm_target_status
                    if crm_target_status == "requires_intervention":
                        crm.next_action = "Verificar estado en el portal manualmente. No reenviar a ciegas tras interrupción durante despacho."
                        crm.notes = "Interrupción o expiración de lease durante despacho al portal. Requiere intervención manual."
                        session.add(CRMEvent(
                            tenant_id=task.tenant_id,
                            crm_entry_id=crm.id,
                            event_type="recovery_flagged_uncertain",
                            old_status=old_st,
                            new_status="requires_intervention",
                            details={"reason": "Lease expired or crash during dispatch. Blind resend forbidden."},
                        ))

            recovered.append({
                "task_id": task.id,
                "action": rec_action,
                "reason": rec_reason,
            })

        await session.commit()
        return recovered

