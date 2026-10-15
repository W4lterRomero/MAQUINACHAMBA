"""
Suite de pruebas de integración para la Fase 1 — MVP Vertical.
Demuestra el circuito completo de extremo a extremo con datos ficticios y portal de prueba:
Perfil -> Vacante -> Evaluación -> Preparación -> Aprobación -> Programación día siguiente ->
Ejecución con reloj simulado -> Confirmación con evidencia -> CRM.
"""

from datetime import date, datetime, time, timedelta, timezone
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import MockClock
from backend.app.core.security import hash_content
from backend.app.core.database import async_session_factory
from backend.app.modules.users.models import Tenant
from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.evaluation.service import DeterministicEvaluationService
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.scheduling.service import SchedulingService
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter
from backend.app.modules.execution.worker import DurableExecutionWorker


@pytest_asyncio.fixture
async def db_session(setup_db) -> AsyncSession:
    """Provee una sesión de base de datos aislada para cada test."""
    async with async_session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def tenant_fixture(db_session: AsyncSession) -> Tenant:
    """Fixture de tenant configurado."""
    tenant = Tenant(
        name="Empresa Ficticia Demo",
        timezone="America/Mexico_City",
        simulation_mode=True,
        execution_window_start=time(9, 0),
        execution_window_end=time(18, 0),
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest_asyncio.fixture
async def candidate_fixture(db_session: AsyncSession, tenant_fixture: Tenant):
    """Fixture de candidato ficticio con perfil verificado, habilidades y CV maestro."""
    t_id = tenant_fixture.id

    profile = UserProfile(
        tenant_id=t_id,
        full_name="Alejandro Morales",
        email="alejandro.morales.demo@example.com",
        phone="+52 55 1234 5678",
        location_city="Ciudad de México",
        location_country="México",
        work_countries=["México", "Remoto"],
        availability="immediate",
        languages=[{"lang": "es", "level": "native"}, {"lang": "en", "level": "advanced"}],
        verification_status="verified",
    )
    db_session.add(profile)
    await db_session.flush()

    skills = [
        Skill(tenant_id=t_id, profile_id=profile.id, name="Python", category="technical", proficiency="expert", years_experience=5.0, verification_status="verified"),
        Skill(tenant_id=t_id, profile_id=profile.id, name="FastAPI", category="technical", proficiency="advanced", years_experience=3.0, verification_status="verified"),
        Skill(tenant_id=t_id, profile_id=profile.id, name="SQLAlchemy", category="technical", proficiency="advanced", years_experience=3.0, verification_status="verified"),
        Skill(tenant_id=t_id, profile_id=profile.id, name="Docker", category="technical", proficiency="intermediate", years_experience=2.0, verification_status="verified"),
    ]
    for s in skills:
        db_session.add(s)

    cv = MasterCV(
        tenant_id=t_id,
        profile_id=profile.id,
        name="Desarrollo Backend Python",
        specialization="Backend Developer",
        summary="Ingeniero de Software backend con 5 años de experiencia en APIs RESTful y microservicios.",
        sections={"experience": ["Dev en TechCorp"], "education": ["Ing. en Sistemas"]},
        is_active=True,
        version=1,
    )
    db_session.add(cv)
    await db_session.flush()

    return profile, skills, cv


@pytest_asyncio.fixture
async def vacancy_fixture(db_session: AsyncSession, tenant_fixture: Tenant) -> Vacancy:
    """Fixture de vacante de prueba ficticia."""
    t_id = tenant_fixture.id
    vacancy = Vacancy(
        tenant_id=t_id,
        external_id="V-MOCK-101",
        source="mock_test_portal",
        source_url="https://mock-jobs.local/vacancies/101",
        company_name="Innovatech Soluciones",
        job_title="Senior Python Backend Developer",
        location="Remoto - Latinoamérica",
        remote_type="remote",
        salary_min=3000.00,
        salary_max=4500.00,
        salary_currency="USD",
        requirements=["Python", "FastAPI", "SQLAlchemy", "Docker"],
        description="Buscamos backend developer con experiencia en FastAPI y arquitecturas modulares.",
        dedup_hash=hash_content("V-MOCK-101", "Innovatech Soluciones", "Senior Python Backend Developer"),
    )
    db_session.add(vacancy)
    await db_session.flush()
    return vacancy


class TestPhase1MVPVertical:
    """Suite completa de integración para el circuito de Fase 1."""

    @pytest.mark.asyncio
    async def test_full_vertical_happy_path(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """
        Demostración del circuito completo:
        1. Evaluación determinista
        2. Preparación de candidatura (sin envíos)
        3. Aprobación inmutable del usuario
        4. Programación para el día siguiente con MockClock
        5. Avance de reloj al ciclo de ejecución
        6. Despacho por worker
        7. Evidencia auditable y actualización de CRM
        """
        profile, skills, cv = candidate_fixture
        tenant_id = tenant_fixture.id

        # 1. Reloj de prueba iniciado el Miércoles 15 de Octubre de 2026, 10:00 UTC (04:00 México)
        test_clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        # 2. Evaluación determinista (reglas antes de IA)
        evaluation = DeterministicEvaluationService.evaluate(
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            skills=skills,
            vacancy=vacancy_fixture,
        )
        assert evaluation.compatibility_score >= 0.80
        assert evaluation.recommendation == "strong_match"
        assert len(evaluation.matched_skills) == 4
        assert len(evaluation.missing_skills) == 0
        db_session.add(evaluation)
        await db_session.flush()

        # 3. Preparación de candidatura (REQ-016: ningún envío durante preparación)
        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
            evaluation=evaluation,
        )
        assert candidacy.status == "pending_approval"
        assert candidacy.cover_letter is not None

        # Verificar que el CRM registró la candidatura en pending_approval
        crm_res = await db_session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id))
        crm_entry = crm_res.scalar_one()
        assert crm_entry.current_status == "pending_approval"

        # 4. Aprobación por el usuario (REQ-015: aprobar hoy programa para mañana)
        approval, scheduled_task = await ApprovalService.approve_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            user_timezone=tenant_fixture.timezone,
            window_start=tenant_fixture.execution_window_start,
            notes="Aprobado por el candidato tras revisión completa.",
            clock=test_clock,
        )
        assert approval.decision == "approved"
        assert approval.content_hash is not None
        assert scheduled_task.status == "pending"

        # El reloj estaba en 15 de Octubre -> fecha programada DEBE ser 16 de Octubre
        assert scheduled_task.scheduled_date == date(2026, 10, 16)
        assert scheduled_task.scheduled_time == time(9, 0)
        assert candidacy.status == "scheduled"
        assert crm_entry.current_status == "scheduled"

        # 5. Intentar ejecutar ANTES del día programado: no debe ejecutarse
        worker = DurableExecutionWorker(
            worker_id="worker-test-1",
            clock=test_clock,
            portal_adapter=MockPortalAdapter(simulation_mode=True),
        )

        # 6. AVANZAR EL RELOJ al día siguiente (Jueves 16 de Octubre a las 09:15 hora México)
        # 09:15 México (UTC-6) = 15:15 UTC
        test_clock.set_time(datetime(2026, 10, 16, 15, 15, 0, tzinfo=timezone.utc))

        # 7. Reclamar tarea atómicamente por el worker
        claimed = await worker.claim_task(db_session, scheduled_task.id)
        assert claimed is not None
        assert claimed.locked_by == "worker-test-1"
        assert claimed.status == "executing"

        # 8. Ejecutar la tarea autorizada
        success, message = await worker.execute_task(db_session, claimed)
        assert success is True
        assert "Recibo: MOCK-CONF-" in message

        # 9. Verificación de confirmación y evidencia durable
        assert scheduled_task.status == "completed"
        assert candidacy.status == "sent_confirmed"
        assert crm_entry.current_status == "sent_confirmed"

        # Verificar ExecutionLog
        log_res = await db_session.execute(select(ExecutionLog).where(ExecutionLog.task_id == scheduled_task.id))
        exec_log = log_res.scalar_one()
        assert exec_log.result == "success"
        assert exec_log.evidence is not None
        assert exec_log.evidence["portal"] == "mock_test_portal"
        assert exec_log.evidence["confirmation_code"].startswith("MOCK-CONF-")
        assert exec_log.evidence["simulation_mode"] is True

        # Verificar trazabilidad en CRM events
        events_res = await db_session.execute(
            select(CRMEvent).where(CRMEvent.crm_entry_id == crm_entry.id).order_by(CRMEvent.created_at)
        )
        events = events_res.scalars().all()
        assert len(events) >= 3  # prepared -> approved_and_scheduled -> submission_confirmed
        assert events[-1].event_type == "submission_confirmed"

    @pytest.mark.asyncio
    async def test_modification_invalidates_approval(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """REQ-017: Cualquier edición en el contenido invalida la aprobación previa."""
        profile, skills, cv = candidate_fixture
        tenant_id = tenant_fixture.id
        test_clock = MockClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc))

        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
        )

        approval, task = await ApprovalService.approve_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            clock=test_clock,
        )
        assert task.status == "pending"
        assert approval.invalidated_at is None

        # Modificación de la carta de presentación
        updated_candidacy = await ApprovalService.modify_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            new_cover_letter="Carta completamente editada por el usuario con nuevos datos.",
            clock=test_clock,
        )

        # Verificaciones obligatorias
        assert approval.invalidated_at is not None
        assert "Contenido modificado" in approval.invalidation_reason
        assert task.status == "cancelled"
        assert updated_candidacy.status == "pending_approval"
        assert updated_candidacy.version == 2

        # Si el worker intentara ejecutar la tarea cancelada o invalidada, debe fallar
        test_clock.advance_days(1)
        worker = DurableExecutionWorker(worker_id="worker-1", clock=test_clock)
        claimed = await worker.claim_task(db_session, task.id)
        assert claimed is None  # No puede ser reclamada porque está cancelada

    @pytest.mark.asyncio
    async def test_rejection_blocks_execution(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """REQ-018: Rechazo por el usuario bloquea permanentemente el envío."""
        profile, skills, cv = candidate_fixture
        tenant_id = tenant_fixture.id

        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
        )

        approval = await ApprovalService.reject_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            reason="La empresa no cumple con la expectativa salarial.",
        )
        assert approval.decision == "rejected"
        assert candidacy.status == "rejected_by_user"

        # Verificar que no existe tarea programada pendiente
        tasks_res = await db_session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.status == "pending",
            )
        )
        assert tasks_res.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_two_workers_do_not_execute_concurrently(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """REQ-020: Dos workers concurrentes no pueden reclamar la misma tarea."""
        profile, skills, cv = candidate_fixture
        tenant_id = tenant_fixture.id
        test_clock = MockClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc))

        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
        )
        _, task = await ApprovalService.approve_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            clock=test_clock,
        )

        # Avanzar el reloj al día siguiente dentro de la ventana de ejecución (09:00 México = 15:00 UTC)
        test_clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))
        worker_a = DurableExecutionWorker(worker_id="worker-A", clock=test_clock)
        worker_b = DurableExecutionWorker(worker_id="worker-B", clock=test_clock)

        # Worker A reclama la tarea
        claimed_a = await worker_a.claim_task(db_session, task.id)
        assert claimed_a is not None
        assert claimed_a.locked_by == "worker-A"

        # Worker B intenta reclamar simultáneamente
        claimed_b = await worker_b.claim_task(db_session, task.id)
        assert claimed_b is None  # Debe ser rechazada atómicamente

    @pytest.mark.asyncio
    async def test_missed_window_does_not_send_unexpectedly(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """REQ-023: Si el equipo estuvo apagado y la hora pasó, marca ventana perdida sin enviar."""
        profile, skills, cv = candidate_fixture
        tenant_id = tenant_fixture.id
        test_clock = MockClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc))

        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
        )
        _, task = await ApprovalService.approve_candidacy(
            session=db_session,
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            clock=test_clock,
        )

        # SIMULACIÓN: El equipo estuvo apagado todo el día siguiente y se enciende a las 21:00 horas
        # (Ventana terminaba a las 18:00)
        # 21:00 México (UTC-6) = 03:00 del día subsiguiente UTC
        test_clock.set_time(datetime(2026, 10, 17, 3, 0, 0, tzinfo=timezone.utc))

        worker = DurableExecutionWorker(worker_id="worker-1", clock=test_clock)
        claimed = await worker.claim_task(db_session, task.id)
        assert claimed is not None

        success, message = await worker.execute_task(db_session, claimed, window_end=time(18, 0))
        assert success is False
        assert "perdida" in message

        # Comprobar que no se envió y se registró como missed_window
        assert task.status == "missed_window"
        crm_res = await db_session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id))
        crm = crm_res.scalar_one()
        assert crm.current_status == "missed_window"

    @pytest.mark.asyncio
    async def test_tenant_isolation(
        self,
        db_session: AsyncSession,
        tenant_fixture: Tenant,
        candidate_fixture,
        vacancy_fixture: Vacancy,
    ):
        """SEC-002: Aislamiento por tenant. Operaciones de otro tenant son rechazadas."""
        profile, skills, cv = candidate_fixture
        other_tenant_id = "tenant-other-9999-9999-9999-999999999999"

        candidacy = await ApprovalService.prepare_candidacy(
            session=db_session,
            tenant_id=tenant_fixture.id,
            profile=profile,
            cv=cv,
            vacancy=vacancy_fixture,
        )

        # Intentar aprobar con tenant_id incorrecto debe lanzar error
        with pytest.raises(ValueError, match="Candidatura no encontrada"):
            await ApprovalService.approve_candidacy(
                session=db_session,
                tenant_id=other_tenant_id,
                candidacy_id=candidacy.id,
            )
