"""
Servicio / Demonio de Ejecución Programada Autónomo (REQ-031, Operación sin IDE).
Ejecuta de forma continua e independiente en segundo plano sin requerir un IDE
ni disparadores HTTP manuales.
"""

import asyncio
import logging
import signal
import sys
from datetime import time
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.clock import Clock, SystemClock, get_clock
from backend.app.core.config import get_settings
from backend.app.core.database import async_session_factory, init_db, close_db
from backend.app.modules.approvals.models import ScheduledTask
from backend.app.modules.execution.worker import DurableExecutionWorker
from backend.app.modules.vacancies.models import JobSearchPreferences
from backend.app.modules.vacancies.search_service import JobSearchService

logger = logging.getLogger("maquinachamba.daemon")


class DurableExecutionDaemon:
    """
    Demonio de segundo plano autónomo para la cola de ejecución durable.
    - Escanea periódicamente tareas programadas vencidas.
    - Gestiona leases, fencing tokens y predespacho atómico.
    - Recupera tareas interrumpidas ante caídas o reinicios.
    - Se detiene de forma limpia ante señales SIGINT / SIGTERM.
    """

    def __init__(
        self,
        worker_id: str = "daemon-worker-primary",
        poll_interval_seconds: float = 2.0,
        clock: Optional[Clock] = None,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        window_start: Optional[time] = None,
        window_end: Optional[time] = None,
    ):
        settings = get_settings()
        self.worker_id = worker_id
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock or get_clock()
        self.session_factory = session_factory or async_session_factory
        self.worker = DurableExecutionWorker(worker_id=self.worker_id, clock=self.clock)
        self._stop_event = asyncio.Event()
        self.is_running = False

        if window_start is not None:
            self.window_start = window_start
        else:
            try:
                self.window_start = time.fromisoformat(settings.execution_window_start)
            except Exception:
                self.window_start = time(9, 0)

        if window_end is not None:
            self.window_end = window_end
        else:
            try:
                self.window_end = time.fromisoformat(settings.execution_window_end)
            except Exception:
                self.window_end = time(18, 0)

    async def run_single_iteration(self) -> dict:
        """
        Ejecuta un ciclo completo de recuperación y procesamiento:
        1. Recupera tareas huérfanas o cuyo worker haya caído (recover_stale_tasks).
        2. Busca tareas pendientes cuyo instante UTC ya haya sido alcanzado.
        3. Reclama atómicamente y despacha con fencing token propio.
        Retorna estadísticas de la iteración.
        """
        async with self.session_factory() as session:
            # 1. Recuperar tareas huérfanas en todos los tenants
            recovered = await DurableExecutionWorker.recover_stale_tasks(
                session=session,
                stale_threshold_seconds=10,  # 10s para recuperación ágil en local
                clock=self.clock,
            )
            recovered_count = len(recovered)
            if recovered_count > 0:
                logger.warning(
                    f"[{self.worker_id}] Se recuperaron {recovered_count} tarea(s) interrumpidas de sesiones previas."
                )


            # 2. Consultar tareas pendientes listas para ejecución
            now = self.clock.now_utc()
            stmt = (
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "pending",
                    ScheduledTask.locked_by.is_(None),
                    ScheduledTask.execution_instant_utc <= now,
                )
                .order_by(ScheduledTask.execution_instant_utc.asc())
            )
            res = await session.execute(stmt)
            due_tasks = res.scalars().all()

            processed = []
            for t in due_tasks:
                task_id = t.id
                tenant_id = t.tenant_id

                # Reclamar atómicamente la tarea
                claimed = await self.worker.claim_task(
                    session=session,
                    task_id=task_id,
                    lease_seconds=30,
                    tenant_id=tenant_id,
                )

                if not claimed:
                    continue

                # Ejecutar con protecciones atómicas de predespacho y simulación
                try:
                    success, message = await self.worker.execute_task(
                        session=session,
                        task=claimed,
                        window_start=self.window_start,
                        window_end=self.window_end,
                    )
                    processed.append({
                        "task_id": task_id,
                        "success": success,
                        "message": message,
                    })
                    logger.info(f"[{self.worker_id}] Tarea {task_id} procesada: éxito={success}, msg={message}")
                except Exception as exc:
                    logger.error(f"[{self.worker_id}] Excepción no controlada en tarea {task_id}: {exc}")
                    processed.append({
                        "task_id": task_id,
                        "success": False,
                        "message": f"Error: {exc}",
                    })

            # 3. Búsqueda periódica autónoma de vacantes para tenants con periodicidad activa (REQ-007, REQ-031)
            search_processed = []
            try:
                search_prefs_stmt = (
                    select(JobSearchPreferences)
                    .where(
                        JobSearchPreferences.is_periodic_enabled.is_(True),
                        JobSearchPreferences.source_access_authorized.is_(True),
                        JobSearchPreferences.next_run_at.is_not(None),
                        JobSearchPreferences.next_run_at <= now,
                    )
                )
                search_prefs_res = await session.execute(search_prefs_stmt)
                due_searches = search_prefs_res.scalars().all()

                for sp in due_searches:
                    s_tenant_id = sp.tenant_id
                    try:
                        logger.info(f"[{self.worker_id}] Ejecutando búsqueda periódica autónoma para tenant '{s_tenant_id}'...")
                        res_search = await JobSearchService.execute_job_search(
                            session=session,
                            tenant_id=s_tenant_id,
                            force_dry_run=False,
                        )
                        cands_prepared = 0
                        if res_search.get("jobs_new", 0) > 0:
                            from backend.app.modules.approvals.pipeline import AutoApprovalPipeline
                            pipe_res = await AutoApprovalPipeline.run_pipeline_for_vacancies(
                                session=session,
                                tenant_id=s_tenant_id,
                            )
                            cands_prepared = pipe_res.get("candidacies_prepared", 0)
                            logger.info(
                                f"[{self.worker_id}] Pipeline automático ejecutado tras búsqueda: "
                                f"{cands_prepared} candidaturas preparadas para la bandeja de aprobación."
                            )

                        search_processed.append({
                            "tenant_id": s_tenant_id,
                            "success": True,
                            "jobs_found": res_search.get("jobs_found", 0),
                            "jobs_new": res_search.get("jobs_new", 0),
                            "candidacies_prepared": cands_prepared,
                        })
                    except Exception as s_exc:
                        logger.error(f"[{self.worker_id}] Error en búsqueda periódica de tenant '{s_tenant_id}': {s_exc}")
                        search_processed.append({
                            "tenant_id": s_tenant_id,
                            "success": False,
                            "error": str(s_exc),
                        })
            except Exception as outer_s_exc:
                logger.error(f"[{self.worker_id}] Error al consultar búsquedas periódicas: {outer_s_exc}")

            return {
                "recovered_count": recovered_count,
                "processed_count": len(processed),
                "tasks": processed,
                "periodic_searches": search_processed,
            }

    async def run_forever(self, max_iterations: Optional[int] = None):
        """Bucle principal de ejecución del demonio con límite opcional de iteraciones."""
        self.is_running = True
        self._stop_event.clear()
        iteration_count = 0
        logger.info(
            f"Demonio de ejecución autónomo iniciado [{self.worker_id}]. "
            f"Intervalo de escaneo: {self.poll_interval_seconds}s. Modo simulación activo."
        )

        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_single_iteration()
                except Exception as exc:
                    logger.error(f"Error en iteración del demonio: {exc}", exc_info=True)

                iteration_count += 1
                if max_iterations is not None and iteration_count >= max_iterations:
                    break

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self.is_running = False
            logger.info(f"Demonio de ejecución [{self.worker_id}] detenido correctamente.")

    def stop(self):
        """Detiene de forma limpia el bucle del demonio."""
        self._stop_event.set()


async def main(
    interval: float = 3.0,
    once: bool = False,
    max_iterations: Optional[int] = None,
    window_start: Optional[time] = None,
    window_end: Optional[time] = None,
):
    """Punto de entrada de línea de comandos para el launcher del demonio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    settings = get_settings()
    logger.info(f"Iniciando servicio de fondo Máquina de Chamba (simulación={settings.simulation_mode})...")

    await init_db()
    daemon = DurableExecutionDaemon(
        poll_interval_seconds=interval,
        window_start=window_start,
        window_end=window_end,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.stop)
        except NotImplementedError:
            pass  # En plataformas donde add_signal_handler no esté soportado

    try:
        if once:
            await daemon.run_single_iteration()
        else:
            await daemon.run_forever(max_iterations=max_iterations)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
