"""
Pruebas de regresión para el worker de ejecución duradera de Fase 1.
Cubre los 4 requerimientos obligatorios del dictamen de Work sobre 54b844f:
1. P0: Ejecución anticipada (antes de fecha, antes de hora, instante exacto, después de ventana).
2. P0: Reejecución sin propiedad y estados terminales (contador de llamadas a cero).
3. P1: Durabilidad y concurrencia con dos sesiones en SQLite temporal en disco, caídas antes/después del efecto y manejo de resultados inciertos sin reenvío ciego.
4. P1: Control estricto de simulación (bloqueo fail-closed de adaptadores no simulados).
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import pytest
import pytest_asyncio
from sqlalchemy import select, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.app.core.clock import MockClock
from backend.app.core.database import Base
from backend.app.core.security import AuthorizationPolicy, hash_content
from backend.app.modules.users.models import Tenant
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.scheduling.service import SchedulingService
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter, PortalSubmissionResult
from backend.app.modules.execution.worker import DurableExecutionWorker


class CountingMockAdapter(MockPortalAdapter):
    """Doble de prueba con contador de llamadas y soporte para respuestas inciertas."""
    def __init__(self, simulation_mode: bool = True, result_override: PortalSubmissionResult = None):
        super().__init__(simulation_mode=simulation_mode, result_override=result_override)
        self.call_count = 0

    async def submit_application(self, *args, **kwargs) -> PortalSubmissionResult:
        self.call_count += 1
        return await super().submit_application(*args, **kwargs)


class LiveUnsimulatedAdapter:
    """Adaptador que simula un conector de producción sin modo simulación."""
    PORTAL_NAME = "live_production_portal"
    CAPABILITIES = {
        "supports_direct_submit": True,
        "mode": "live_real_network",
    }

    def __init__(self):
        self.simulation_mode = False
        self.call_count = 0

    async def submit_application(self, *args, **kwargs) -> PortalSubmissionResult:
        self.call_count += 1
        return PortalSubmissionResult(
            success=True,
            confirmation_number="LIVE-REAL-CONF-999",
            status_code=200,
            evidence={"portal": "live_real_network"},
        )


@pytest_asyncio.fixture
async def regression_db(tmp_path):
    """
    Crea una base de datos SQLite real en disco temporal con soporte WAL
    y dos fábricas de sesiones independientes para pruebas de concurrencia y durabilidad.
    """
    db_file = tmp_path / "regression_durability.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker_a = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session_maker_b = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    yield session_maker_a, session_maker_b

    await engine.dispose()
    if os.path.exists(db_file):
        os.remove(db_file)


async def _create_test_candidacy_and_task(session: AsyncSession, clock: MockClock):
    """Helper para crear una postulación aprobada y programada en base de datos."""
    tenant = Tenant(
        name="Tenant Regresión",
        timezone="America/Mexico_City",
        simulation_mode=True,
        execution_window_start=time(9, 0),
        execution_window_end=time(18, 0),
    )
    session.add(tenant)
    await session.flush()

    profile = UserProfile(
        tenant_id=tenant.id,
        full_name="Carlos Prueba",
        email="carlos.prueba@example.com",
        location_city="Ciudad de México",
        location_country="México",
        work_countries=["México", "Remoto"],
        verification_status="verified",
    )
    session.add(profile)
    await session.flush()

    cv = MasterCV(
        tenant_id=tenant.id,
        profile_id=profile.id,
        name="CV Python",
        specialization="Backend",
        summary="CV verificado de prueba",
        sections={"skills": ["Python", "FastAPI"]},
        is_active=True,
    )
    session.add(cv)
    await session.flush()

    vacancy = Vacancy(
        tenant_id=tenant.id,
        external_id="V-REG-01",
        source="mock_test_portal",
        source_url="https://jobs.example.com/v01",
        company_name="Empresa Test",
        job_title="Desarrollador Python",
        requirements=["Python"],
        dedup_hash=hash_content("V-REG-01", "Empresa Test"),
    )
    session.add(vacancy)
    await session.flush()

    candidacy = await ApprovalService.prepare_candidacy(
        session=session,
        tenant_id=tenant.id,
        profile=profile,
        cv=cv,
        vacancy=vacancy,
    )

    approval, task = await ApprovalService.approve_candidacy(
        session=session,
        tenant_id=tenant.id,
        candidacy_id=candidacy.id,
        clock=clock,
    )

    await session.commit()
    return tenant, candidacy, approval, task


# =============================================================================
# 1. P0 — Pruebas de Ejecución Anticipada
# =============================================================================

class TestPrematureExecutionDefense:
    """Verifica que ninguna tarea pueda reclamarse ni ejecutarse antes de tiempo."""

    @pytest.mark.asyncio
    async def test_claim_and_execute_before_scheduled_day_blocked(self, regression_db):
        """No permite reclamo ni ejecución en el mismo día de la aprobación (hoy)."""
        session_maker, _ = regression_db
        # Aprobada hoy a las 10:00 AM (16:00 UTC)
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            # 1. Intentar reclamar hoy: DEBE ser None
            claimed = await worker.claim_task(session, task.id)
            assert claimed is None, "claim_task no debe reclamar una tarea antes del instante programado."

            # 2. Si se fuerza la llamada a execute_task directamente hoy: DEBE rechazar y NO llamar al portal
            success, msg = await worker.execute_task(session, task)
            assert success is False
            assert "anticipada" in msg.lower() or "estado inválido" in msg.lower()
            assert adapter.call_count == 0, "No debe haber ningún efecto sobre el adaptador si aún no vence."

    @pytest.mark.asyncio
    async def test_claim_and_execute_before_window_hour_on_scheduled_day_blocked(self, regression_db):
        """En el día programado pero antes de las 09:00 local (ej. 08:30 = 14:30 UTC): bloqueado."""
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()

            # Avanzar al día siguiente pero a las 08:30 hora México (14:30 UTC)
            clock.set_time(datetime(2026, 10, 16, 14, 30, 0, tzinfo=timezone.utc))
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            # 1. claim_task debe fallar
            claimed = await worker.claim_task(session, task.id)
            assert claimed is None

            # 2. execute_task debe rechazar sin llamar al portal
            task_persisted = await session.get(ScheduledTask, task.id)
            success, msg = await worker.execute_task(session, task_persisted)
            assert success is False
            assert adapter.call_count == 0

    @pytest.mark.asyncio
    async def test_claim_and_execute_at_exact_instant_allowed(self, regression_db):
        """En el instante exacto programado (09:00 México = 15:00 UTC): éxito."""
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()

            # En punto de las 09:00 hora México (15:00 UTC)
            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None
            assert claimed.status == "executing"

            success, msg = await worker.execute_task(session, claimed)
            assert success is True
            assert adapter.call_count == 1
            assert claimed.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_after_window_end_marks_missed_window(self, regression_db):
        """Después del fin de ventana (ej. 19:00 México = 01:00 UTC día +2): missed_window sin efectos."""
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()

            # 19:00 México del día 16 = 01:00 UTC del día 17
            clock.set_time(datetime(2026, 10, 17, 1, 0, 0, tzinfo=timezone.utc))
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None

            success, msg = await worker.execute_task(session, claimed)
            assert success is False
            assert "perdida" in msg.lower()
            assert adapter.call_count == 0  # Sin llamadas al portal

            fresh = await session.get(ScheduledTask, task.id)
            assert fresh.status == "missed_window"


# =============================================================================
# 2. P0 — Pruebas de Reejecución sin Propiedad y Worker Incorrecto
# =============================================================================

class TestOwnershipAndDuplicateExecutionDefense:
    """Verifica que execute_task valide estado/propiedad persistidos y no re-despache."""

    @pytest.mark.asyncio
    async def test_execute_task_fails_if_status_not_executing(self, regression_db):
        """execute_task sobre una tarea en estado 'completed' falla y no llama al adaptador."""
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()

            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None

            # Primera ejecución exitosa
            success, _ = await worker.execute_task(session, claimed)
            assert success is True
            assert adapter.call_count == 1

            # Segunda invocación repetida sobre la misma tarea ya completada
            success_2, msg_2 = await worker.execute_task(session, claimed)
            assert success_2 is False
            assert "estado inválido" in msg_2.lower()
            # El adaptador NO debe haberse llamado por segunda vez
            assert adapter.call_count == 1

    @pytest.mark.asyncio
    async def test_execute_task_fails_if_locked_by_different_worker(self, regression_db):
        """Worker B no puede ejecutar una tarea bloqueada por Worker A."""
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            adapter = CountingMockAdapter()

            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))
            worker_a = DurableExecutionWorker(worker_id="worker-A", clock=clock, portal_adapter=adapter)
            worker_b = DurableExecutionWorker(worker_id="worker-B", clock=clock, portal_adapter=adapter)

            # Worker A reclama la tarea
            claimed_a = await worker_a.claim_task(session, task.id)
            assert claimed_a is not None
            assert claimed_a.locked_by == "worker-A"

            # Worker B intenta ejecutar la tarea reclamada por Worker A
            success_b, msg_b = await worker_b.execute_task(session, claimed_a)
            assert success_b is False
            assert "worker no autorizado" in msg_b.lower()
            assert adapter.call_count == 0


# =============================================================================
# 3. P1 — Durabilidad, Concurrencia y Recuperación tras Caída
# =============================================================================

class TestDurabilityConcurrencyAndCrashRecovery:
    """Pruebas con sesiones/conexiones independientes sobre SQLite en disco."""

    @pytest.mark.asyncio
    async def test_concurrent_claim_with_two_independent_sessions(self, regression_db):
        """
        Dos sesiones/conexiones independientes sobre base de datos en disco:
        Worker A hace commit de su reclamo en Sesión A; Sesión B ve el bloqueo de inmediato.
        """
        session_maker_a, session_maker_b = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        # Crear registros iniciales en Sesión A
        async with session_maker_a() as session_a:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session_a, clock)
            task_id = task.id

        clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))

        # Abrir Sesión A y Sesión B simultáneamente
        async with session_maker_a() as session_a, session_maker_b() as session_b:
            worker_a = DurableExecutionWorker(worker_id="worker-A", clock=clock)
            worker_b = DurableExecutionWorker(worker_id="worker-B", clock=clock)

            # Worker A reclama en Sesión A (realiza commit duradero)
            claimed_a = await worker_a.claim_task(session_a, task_id)
            assert claimed_a is not None
            assert claimed_a.locked_by == "worker-A"
            assert claimed_a.status == "executing"

            # Worker B intenta reclamar en Sesión B: debe retornar None
            claimed_b = await worker_b.claim_task(session_b, task_id)
            assert claimed_b is None

    @pytest.mark.asyncio
    async def test_crash_before_adapter_effect_recovery(self, regression_db):
        """
        Caída ANTES del efecto: la tarea quedó 'executing' pero no hay ExecutionLog.
        La recuperación debe regresarla a 'pending' para reintento seguro.
        """
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))

            worker = DurableExecutionWorker(worker_id="worker-crash-1", clock=clock)
            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None
            # Simular caída abrupta: el worker muere aquí, no se llama execute_task

        # Simular reinicio del sistema 10 minutos después
        clock.advance_seconds(600)
        async with session_maker() as session:
            recovered = await DurableExecutionWorker.recover_stale_tasks(
                session=session,
                stale_threshold_seconds=300,
                clock=clock,
            )
            assert len(recovered) == 1
            assert recovered[0]["action"] == "reset_to_pending"

            fresh_task = await session.get(ScheduledTask, task.id)
            assert fresh_task.status == "pending"
            assert fresh_task.locked_by is None

    @pytest.mark.asyncio
    async def test_crash_after_adapter_effect_recovery_without_duplicate_send(self, regression_db):
        """
        Caída DESPUÉS del efecto: existe ExecutionLog exitoso pero la tarea no completó actualización.
        La recuperación reconcilia a 'completed' SIN llamar de nuevo al portal.
        """
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))

            worker = DurableExecutionWorker(worker_id="worker-crash-2", clock=clock)
            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None

            # Simular que el portal ejecutó y se escribió el ExecutionLog, pero se cayó antes del commit final de status
            log = ExecutionLog(
                tenant_id=tenant.id,
                task_id=claimed.id,
                candidacy_id=candidacy.id,
                action="submit_application",
                result="success",
                evidence={"confirmation_code": "MOCK-CONF-CRASH"},
                started_at=clock.now_utc(),
                completed_at=clock.now_utc(),
                duration_ms=150,
            )
            session.add(log)
            # Tarea quedó en executing
            await session.commit()

        # Simular reinicio tras caída
        clock.advance_seconds(600)
        async with session_maker() as session:
            recovered = await DurableExecutionWorker.recover_stale_tasks(
                session=session,
                stale_threshold_seconds=300,
                clock=clock,
            )
            assert len(recovered) == 1
            assert recovered[0]["action"] == "reconciled_completed"

            fresh_task = await session.get(ScheduledTask, task.id)
            assert fresh_task.status == "completed"

    @pytest.mark.asyncio
    async def test_uncertain_result_blocks_blind_resends(self, regression_db):
        """
        Resultado incierto (ej. timeout de red en portal):
        Pasa a 'result_uncertain' y CRM a 'requires_intervention'.
        NO se reenvía a ciegas.
        """
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))

            # Adaptador que retorna resultado incierto (código 504 Gateway Timeout)
            uncertain_result = PortalSubmissionResult(
                success=False,
                confirmation_number="",
                status_code=504,
                evidence={"uncertain": True, "details": "Timeout awaiting HTTP response"},
                error_message="Gateway Timeout al contactar el portal. Estado de recepción desconocido.",
                result_type="uncertain",
            )
            adapter = CountingMockAdapter(result_override=uncertain_result)
            worker = DurableExecutionWorker(worker_id="worker-reg", clock=clock, portal_adapter=adapter)

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None

            success, msg = await worker.execute_task(session, claimed)
            assert success is False
            assert "requires_intervention" in msg.lower() or "incierto" in msg.lower()

            # Verificar estados persistidos
            fresh_task = await session.get(ScheduledTask, task.id)
            assert fresh_task.status == "result_uncertain"

            fresh_candidacy = await session.get(Candidacy, candidacy.id)
            assert fresh_candidacy.status == "result_uncertain"

            crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id))
            crm = crm_res.scalar_one()
            assert crm.current_status == "requires_intervention"
            assert "no reenviar a ciegas" in crm.next_action.lower()


# =============================================================================
# 4. P1 — Control Estricto de Simulación
# =============================================================================

class TestSimulationModeFailClosedProtection:
    """Verifica que adaptadores no simulados queden terminantemente bloqueados."""

    @pytest.mark.asyncio
    async def test_unsimulated_adapter_blocked_when_simulation_mode_active(self, regression_db):
        """
        Un adaptador sin simulación (que intentaría red real) inyectado en un worker
        con simulation_mode=True es bloqueado de inmediato con cero efectos externos.
        """
        session_maker, _ = regression_db
        clock = MockClock(datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc))

        async with session_maker() as session:
            tenant, candidacy, approval, task = await _create_test_candidacy_and_task(session, clock)
            clock.set_time(datetime(2026, 10, 16, 15, 0, 0, tzinfo=timezone.utc))

            # Adaptador de red real sin simulación
            live_adapter = LiveUnsimulatedAdapter()
            # Worker configurado con simulation_mode=True
            worker = DurableExecutionWorker(
                worker_id="worker-sim-safety",
                clock=clock,
                portal_adapter=live_adapter,
                simulation_mode=True,
            )

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None

            # Debe ser bloqueado por la verificación de simulación
            success, msg = await worker.execute_task(session, claimed)
            assert success is False
            assert "modo simulación activo" in msg.lower()
            assert "no es de simulación" in msg.lower()

            # EFECTO CERO: El adaptador real no fue invocado
            assert live_adapter.call_count == 0

            # La tarea fue marcada como fallo y se registró el ExecutionLog
            fresh_task = await session.get(ScheduledTask, task.id)
            assert fresh_task.status == "failed"

            log_res = await session.execute(select(ExecutionLog).where(ExecutionLog.task_id == task.id))
            log = log_res.scalar_one()
            assert log.result == "failure"
            assert "bloqueo de seguridad" in log.error_message.lower()

    def test_authorization_policy_separates_package_from_live_permission(self):
        """Verifica la separación explícita de validate_approval_package y can_perform_live_effects."""
        # 1. Paquete válido
        valid, msg = AuthorizationPolicy.validate_approval_package(
            approval_decision="approved",
            approval_content_hash="hash-123",
            current_content_hash="hash-123",
            approval_invalidated=False,
        )
        assert valid is True

        # 2. Permiso de efectos en simulación con adaptador simulado: PERMITIDO
        sim_ok, _ = AuthorizationPolicy.can_perform_live_effects(
            simulation_mode=True,
            is_simulation_adapter=True,
        )
        assert sim_ok is True

        # 3. Permiso de efectos en simulación con adaptador real: BLOQUEADO
        sim_blocked, msg_blocked = AuthorizationPolicy.can_perform_live_effects(
            simulation_mode=True,
            is_simulation_adapter=False,
            adapter_mode="live_network",
        )
        assert sim_blocked is False
        assert "efectos externos reales no autorizados" in msg_blocked
