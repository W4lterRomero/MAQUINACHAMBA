"""
Pruebas de Operación sin IDE y Demonio Durable (REQ-019, REQ-020, REQ-021, REQ-031).
Demuestra que el demonio de ejecución programada:
1. Procesa tareas automáticamente a la hora autorizada sin disparadores manuales.
2. Conserva su estado íntegro y tareas persistidas tras reiniciarse.
3. Recupera tareas huérfanas dejadas por un proceso caído y las lleva a término de forma segura.
"""

from datetime import datetime, time, timedelta, timezone
import pytest
from sqlalchemy import select

from backend.app.core.clock import MockClock
from backend.app.core.database import async_session_factory
from backend.app.core.security import hash_content
from backend.app.modules.approvals.models import (
    Approval,
    Candidacy,
    ExecutionLog,
    ScheduledTask,
)
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.crm.models import CRMEntry

from backend.app.modules.cv.models import MasterCV
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.execution.daemon import DurableExecutionDaemon
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.vacancies.models import Vacancy


async def _create_test_candidacy_and_task(
    session,
    tenant_id: str,
    execution_instant_utc: datetime,
) -> tuple[ScheduledTask, Candidacy, CRMEntry]:
    """Crea una estructura completa de candidatura aprobada y programada en la base de datos."""
    prof = UserProfile(
        tenant_id=tenant_id,
        full_name="Candidato Durable",
        email="candidato@example.com",
        location_city="CDMX",
        location_country="México",
    )
    session.add(prof)
    await session.flush()

    cv = MasterCV(
        tenant_id=tenant_id,
        profile_id=prof.id,
        name="CV Principal",
        specialization="Backend",
    )
    session.add(cv)
    await session.flush()

    vac = Vacancy(
        tenant_id=tenant_id,
        company_name="Empresa Remota",
        job_title="Ingeniero Backend",
        source_url="https://empresa.com/jobs/1",
        requirements=["Python"],
        dedup_hash="hash-empresa-backend-001",
    )

    session.add(vac)
    await session.flush()

    ev = Evaluation(
        tenant_id=tenant_id,
        vacancy_id=vac.id,
        profile_id=prof.id,
        cv_id=cv.id,
        compatibility_score=0.9,
        justification="Compatibilidad alta en Python y backend.",
    )

    session.add(ev)
    await session.flush()

    cv_snapshot = {"name": prof.full_name, "title": cv.specialization, "sections": {}}
    cand = Candidacy(
        tenant_id=tenant_id,
        vacancy_id=vac.id,
        profile_id=prof.id,
        cv_id=cv.id,
        evaluation_id=ev.id,
        adapted_cv_snapshot=cv_snapshot,
        cover_letter="Estimado equipo, adjunto mi postulación.",
        form_responses={},
        target_portal="portal_mock",
        target_url="https://empresa.com/jobs/1",
        planned_actions=[{"action": "submit_candidacy", "method": "portal_mock"}],
        status="scheduled",
        version=1,
    )
    session.add(cand)
    await session.flush()

    c_hash = ApprovalService.compute_candidacy_hash(cand)
    approval = Approval(
        tenant_id=tenant_id,
        candidacy_id=cand.id,
        decision="approved",
        decided_by="user",
        decided_at=execution_instant_utc - timedelta(days=1),
        content_hash=c_hash,
        document_versions={"version": 1},
    )
    session.add(approval)
    await session.flush()


    task = ScheduledTask(
        tenant_id=tenant_id,
        approval_id=approval.id,
        candidacy_id=cand.id,
        scheduled_date=execution_instant_utc.date(),
        scheduled_time=execution_instant_utc.time(),
        execution_timezone="America/Mexico_City",
        execution_instant_utc=execution_instant_utc,
        status="pending",
        idempotency_key=f"idemp-{cand.id}-v1",
        fence_token=1,
    )
    session.add(task)

    crm = CRMEntry(
        tenant_id=tenant_id,
        candidacy_id=cand.id,
        vacancy_id=vac.id,
        current_status="scheduled",
        priority="medium",
    )
    session.add(crm)
    await session.commit()
    await session.refresh(task)
    await session.refresh(cand)
    await session.refresh(crm)
    return task, cand, crm


@pytest.mark.asyncio
async def test_daemon_executes_task_at_authorized_time(setup_db):
    """
    REQ-031 y REQ-015:
    Demuestra que el demonio autónomo respeta el tiempo:
    - NO procesa la tarea antes del instante programado.
    - La procesa en cuanto se alcanza el instante autorizado, sincronizando CRM y candidatura.
    """
    import uuid
    tenant_id = f"tenant-daemon-{uuid.uuid4().hex[:8]}"
    scheduled_instant = datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(datetime(2026, 10, 16, 14, 50, 0, tzinfo=timezone.utc))  # 10 min antes

    async with async_session_factory() as session:
        task, cand, crm = await _create_test_candidacy_and_task(
            session=session,
            tenant_id=tenant_id,
            execution_instant_utc=scheduled_instant,
        )

    daemon = DurableExecutionDaemon(
        worker_id="daemon-scheduled-test",
        clock=clock,
        session_factory=async_session_factory,
    )

    # 1. Ejecutar iteración antes de la hora -> tarea NO vence, procesa 0 de este tenant
    res_before = await daemon.run_single_iteration()
    assert not any(t["task_id"] == task.id for t in res_before["tasks"])

    async with async_session_factory() as session:
        t_check = await session.get(ScheduledTask, task.id)
        assert t_check.status == "pending"

    # 2. Avanzar el reloj a la hora programada + 1 segundo
    clock.advance(timedelta(minutes=10, seconds=1))

    # 3. Ejecutar iteración al alcanzar la hora autorizada -> procesa y completa la tarea
    res_after = await daemon.run_single_iteration()
    matching_results = [t for t in res_after["tasks"] if t["task_id"] == task.id]
    assert len(matching_results) == 1
    assert matching_results[0]["success"] is True

    # 4. Verificar sincronización en SQLite: tarea completada, candidatura enviada, CRM actualizado
    async with async_session_factory() as session:
        t_final = await session.get(ScheduledTask, task.id)
        assert t_final.status == "completed"

        c_final = await session.get(Candidacy, cand.id)
        assert c_final.status == "simulated_success"

        crm_res = await session.execute(
            select(CRMEntry).where(CRMEntry.candidacy_id == cand.id)
        )
        crm_final = crm_res.scalar_one()
        assert crm_final.current_status == "simulated_success"


@pytest.mark.asyncio
async def test_daemon_restart_preserves_persisted_state_and_recovers_task(setup_db):
    """
    REQ-019, REQ-020, REQ-021:
    Demuestra resiliencia ante caídas y reinicio del servicio de segundo plano:
    1. Un worker/demonio A toma una tarea y el proceso cae abruptamente dejando la tarea en ejecución.
    2. La tarea permanece persistida de forma segura en SQLite.
    3. Un nuevo demonio B arranca (simulando reinicio del sistema o ejecución sin IDE).
    4. El nuevo demonio recupera la tarea huérfana, incrementa el fencing token y la lleva a término
       sin duplicaciones ni inconsistencias de estado.
    """
    import uuid
    tenant_id = f"tenant-restart-{uuid.uuid4().hex[:8]}"
    scheduled_instant = datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(datetime(2026, 10, 16, 15, 1, 0, tzinfo=timezone.utc))

    async with async_session_factory() as session:
        task, cand, crm = await _create_test_candidacy_and_task(
            session=session,
            tenant_id=tenant_id,
            execution_instant_utc=scheduled_instant,
        )

    # 1. Daemon A arranca y reclama la tarea
    daemon_a = DurableExecutionDaemon(
        worker_id="daemon-instance-A",
        clock=clock,
        session_factory=async_session_factory,
    )
    async with async_session_factory() as session:
        claimed_task = await daemon_a.worker.claim_task(
            session=session,
            task_id=task.id,
            lease_seconds=5,
            tenant_id=tenant_id,
        )


        assert claimed_task is not None
        assert claimed_task.locked_by == "daemon-instance-A"
        assert claimed_task.status == "executing"

    # 2. Simulamos caída abrupta de Daemon A (muere sin completar)
    del daemon_a

    # Comprobamos que el estado permanece persistido en SQLite
    async with async_session_factory() as session:
        t_persisted = await session.get(ScheduledTask, task.id)
        assert t_persisted.status == "executing"
        assert t_persisted.locked_by == "daemon-instance-A"

    # 3. El tiempo avanza más allá del lease del worker caído
    clock.advance(timedelta(seconds=15))

    # 4. Un nuevo demonio independiente (Daemon B) inicia en un nuevo ciclo/arranque
    daemon_b = DurableExecutionDaemon(
        worker_id="daemon-instance-B-after-reboot",
        clock=clock,
        session_factory=async_session_factory,
    )

    # 5. Daemon B ejecuta su iteración: detecta la tarea huérfana de A y la recupera de forma segura
    iteration_result = await daemon_b.run_single_iteration()
    assert iteration_result["recovered_count"] >= 1

    # 6. Comprobamos estado final persistido en la base de datos:
    # Por seguridad estricta anti-duplicación (Work dictamen a99b9f2), ante caída sin log confirmado,
    # la tarea se preserva como result_uncertain / requires_intervention (NUNCA reenvío ciego).
    async with async_session_factory() as session:
        t_recovered = await session.get(ScheduledTask, task.id)
        assert t_recovered.status == "result_uncertain"
        # El fencing token se incrementó asegurando que la instancia A no pueda escribir
        assert t_recovered.fence_token >= 2

        c_recovered = await session.get(Candidacy, cand.id)
        assert c_recovered.status == "result_uncertain"

        crm_res = await session.execute(
            select(CRMEntry).where(CRMEntry.candidacy_id == cand.id)
        )
        crm_recovered = crm_res.scalar_one()
        assert crm_recovered.current_status == "requires_intervention"

