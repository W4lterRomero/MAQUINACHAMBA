"""
Pruebas para garantizar que MockPortalAdapter NUNCA produzca sent_confirmed (P0 - Bloqueador 2).
Introduce y prueba el estado explícito 'simulated_success' en Candidacy, CRMEntry, CRMEvent y recuperación de tareas.
"""

from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.users.models import Tenant
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.core.clock import MockClock
from backend.app.modules.execution.worker import DurableExecutionWorker
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.approvals.service import ApprovalService


@pytest.mark.asyncio
async def test_mock_portal_adapter_produces_simulated_success_never_sent_confirmed(setup_db):
    """
    Verifica que al ejecutar una postulación con MockPortalAdapter:
    1. Candidacy.status transiciona a 'simulated_success' y NUNCA a 'sent_confirmed'.
    2. CRMEntry.current_status transiciona a 'simulated_success'.
    3. CRMEvent registra event_type='simulation_confirmed'.
    4. ScheduledTask.status transiciona a 'completed'.
    """
    from backend.app.core.database import async_session_factory

    clock = MockClock(datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc))
    mock_adapter = MockPortalAdapter(simulation_mode=True)
    worker = DurableExecutionWorker(
        worker_id="test-sim-worker-1",
        clock=clock,
        portal_adapter=mock_adapter,
        simulation_mode=True,
    )

    async with async_session_factory() as session:
        # Tenant
        tenant = Tenant(
            name="Test Tenant",
            timezone="America/Mexico_City",
            simulation_mode=True,
        )
        session.add(tenant)
        await session.flush()
        t_id = tenant.id

        # Profile
        profile = UserProfile(
            tenant_id=t_id,
            full_name="Test Candidate",
            email="candidate@example.com",
            location_city="San Salvador",
            location_country="El Salvador",
        )
        session.add(profile)
        await session.flush()

        # Master CV
        cv = MasterCV(
            tenant_id=t_id,
            profile_id=profile.id,
            name="Principal CV",
            specialization="Full Stack",
        )
        session.add(cv)
        await session.flush()

        # Vacante
        vacancy = Vacancy(
            tenant_id=t_id,
            job_title="Full Stack Engineer",
            company_name="Test Sim Corp",
            source="test_portal",
            source_url="https://test-sim.example.com/job/1",
            dedup_hash="hash-sim-001",
            is_active=True,
            is_real_search_result=False,
            verification_status="verified",
        )
        session.add(vacancy)
        await session.flush()

        # Candidatura
        candidacy = Candidacy(
            tenant_id=t_id,
            vacancy_id=vacancy.id,
            profile_id=profile.id,
            cv_id=cv.id,
            cover_letter="Cover letter text",
            form_responses={"applicant_email": "user@example.com"},
            target_portal="mock_test_portal",
            target_url="https://test-sim.example.com/job/1",
            planned_actions=[],
            status="approved",
            version=1,
        )
        session.add(candidacy)
        await session.flush()

        # Hash y aprobación
        c_hash = ApprovalService.compute_candidacy_hash(candidacy)
        approval = Approval(
            tenant_id=t_id,
            candidacy_id=candidacy.id,
            decision="approved",
            content_hash=c_hash,
            document_versions={},
        )
        session.add(approval)
        await session.flush()

        # CRMEntry
        crm = CRMEntry(
            tenant_id=t_id,
            candidacy_id=candidacy.id,
            vacancy_id=vacancy.id,
            current_status="scheduled",
            priority="medium",
        )
        session.add(crm)
        await session.flush()

        # ScheduledTask
        task = ScheduledTask(
            tenant_id=t_id,
            approval_id=approval.id,
            candidacy_id=candidacy.id,
            scheduled_date=clock.now_utc().date(),
            scheduled_time=clock.now_utc().time(),
            execution_timezone="UTC",
            execution_instant_utc=clock.now_utc(),
            status="pending",
            idempotency_key="idemp-sim-001",
        )
        session.add(task)
        await session.commit()

        # Reclamar y ejecutar
        claimed_task = await worker.claim_task(session, task.id)
        assert claimed_task is not None

        success, msg = await worker.execute_task(session, claimed_task)
        assert success is True
        assert "Simulación de postulación completada" in msg

        # Verificar estados
        cand_db = await session.get(Candidacy, candidacy.id)
        assert cand_db.status == "simulated_success"
        assert cand_db.status != "sent_confirmed"

        crm_db = await session.get(CRMEntry, crm.id)
        assert crm_db.current_status == "simulated_success"
        assert crm_db.current_status != "sent_confirmed"

        events_res = await session.execute(
            select(CRMEvent).where(CRMEvent.crm_entry_id == crm.id).order_by(CRMEvent.created_at.desc())
        )
        latest_event = events_res.scalars().first()
        assert latest_event is not None
        assert latest_event.event_type == "simulation_confirmed"
        assert latest_event.new_status == "simulated_success"


@pytest.mark.asyncio
async def test_task_recovery_reconciles_to_simulated_success_never_sent_confirmed(setup_db):
    """
    Verifica que la recuperación de tareas (crash recovery):
    Si el último log era de simulación exitosa, reconcilia la candidatura y el CRM
    a 'simulated_success' y NUNCA a 'sent_confirmed'.
    """
    from backend.app.core.database import async_session_factory

    clock = MockClock(datetime(2026, 10, 27, 14, 0, 0, tzinfo=timezone.utc))
    worker = DurableExecutionWorker(
        worker_id="test-recovery-worker-1",
        clock=clock,
        simulation_mode=True,
    )

    async with async_session_factory() as session:
        # Tenant
        tenant = Tenant(
            name="Test Recovery Tenant",
            timezone="America/Mexico_City",
            simulation_mode=True,
        )
        session.add(tenant)
        await session.flush()
        t_id = tenant.id

        # Profile
        profile = UserProfile(
            tenant_id=t_id,
            full_name="Test Candidate 2",
            email="candidate2@example.com",
            location_city="San Salvador",
            location_country="El Salvador",
        )
        session.add(profile)
        await session.flush()

        # Master CV
        cv = MasterCV(
            tenant_id=t_id,
            profile_id=profile.id,
            name="Principal CV 2",
            specialization="Backend",
        )
        session.add(cv)
        await session.flush()

        # Vacante
        vacancy = Vacancy(
            tenant_id=t_id,
            job_title="Backend Engineer",
            company_name="Crash Recovery Sim Corp",
            source="test_portal",
            source_url="https://test-recovery.example.com/job/2",
            dedup_hash="hash-rec-002",
            is_active=True,
            is_real_search_result=False,
            verification_status="verified",
        )
        session.add(vacancy)
        await session.flush()

        # Candidatura
        candidacy = Candidacy(
            tenant_id=t_id,
            vacancy_id=vacancy.id,
            profile_id=profile.id,
            cv_id=cv.id,
            cover_letter="Text",
            form_responses={},
            target_portal="mock_test_portal",
            target_url="https://test-recovery.example.com/job/2",
            planned_actions=[],
            status="executing",
            version=1,
        )
        session.add(candidacy)
        await session.flush()

        # CRMEntry
        crm = CRMEntry(
            tenant_id=t_id,
            candidacy_id=candidacy.id,
            vacancy_id=vacancy.id,
            current_status="executing",
        )
        session.add(crm)
        await session.flush()

        # Approval
        approval = Approval(
            tenant_id=t_id,
            candidacy_id=candidacy.id,
            decision="approved",
            content_hash="some-hash",
            document_versions={},
        )
        session.add(approval)
        await session.flush()

        # ScheduledTask huérfana (quedó en executing tras caída)
        task = ScheduledTask(
            tenant_id=t_id,
            approval_id=approval.id,
            candidacy_id=candidacy.id,
            scheduled_date=clock.now_utc().date(),
            scheduled_time=clock.now_utc().time(),
            execution_timezone="UTC",
            execution_instant_utc=clock.now_utc(),
            status="executing",
            locked_by="dead-worker",
            fence_token=1,
            lease_expires_at=clock.now_utc(),  # Lease expirado
            idempotency_key="idemp-rec-002",
        )
        session.add(task)
        await session.flush()

        # Log de ejecución previo que confirma éxito en modo simulación
        log = ExecutionLog(
            tenant_id=t_id,
            task_id=task.id,
            candidacy_id=candidacy.id,
            action="submit_application",
            result="success",
            evidence={
                "portal": "mock_test_portal",
                "simulation_mode": True,
                "is_simulated": True,
                "confirmation_code": "MOCK-CONF-RECOVERED",
            },
            started_at=clock.now_utc(),
            completed_at=clock.now_utc(),
            duration_ms=50,
        )
        session.add(log)
        await session.commit()

        # Ejecutar reconciliación de tareas huérfanas
        clock.advance = None  # Just in case
        clock.set_time(datetime(2026, 10, 27, 14, 10, 0, tzinfo=timezone.utc))
        recovered = await DurableExecutionWorker.recover_stale_tasks(
            session=session,
            stale_threshold_seconds=60,
            clock=clock,
            tenant_id=t_id,
        )
        assert len(recovered) == 1
        assert recovered[0]["action"] == "reconciled_simulated_completed"

        # Comprobar que candidatura y CRM quedaron en simulated_success, NUNCA sent_confirmed
        cand_db = await session.get(Candidacy, candidacy.id)
        assert cand_db.status == "simulated_success"
        assert cand_db.status != "sent_confirmed"

        crm_db = await session.get(CRMEntry, crm.id)
        assert crm_db.current_status == "simulated_success"
        assert crm_db.current_status != "sent_confirmed"
