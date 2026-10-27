"""
Pruebas de integración para LinkedIn Modo Asistido (REQ-024, D7, ADR-005).
Verifica:
1. Declaración estricta de capacidades: supports_direct_submit=False, mode='assisted_only'.
2. Validación anti-SSRF de la URL del empleo en LinkedIn.
3. El worker no ejecuta envíos directos headless; transiciona a 'requires_intervention' / 'result_uncertain'.
4. Endpoint GET /candidacies/{id}/linkedin-assisted provee checklist, snippets para copiar y enlace seguro.
5. Endpoint POST /candidacies/{id}/linkedin-confirm transiciona a 'sent_confirmed' y registra auditoría CRM.
6. Aislamiento estricto por tenant_id.
"""

from datetime import datetime, timezone
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select, and_

from backend.app.core.clock import MockClock
from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token, hash_password
from backend.app.modules.approvals.models import Approval, Candidacy, ScheduledTask, ExecutionLog
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.execution.worker import DurableExecutionWorker
from backend.app.modules.portals.adapters.linkedin_adapter import LinkedInAssistedAdapter
from backend.app.modules.portals.factory import get_portal_adapter
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.models import Vacancy


@pytest.mark.asyncio
async def test_linkedin_adapter_capabilities_and_anti_ssrf():
    """Verifica que el adaptador de LinkedIn declara capacidades asistidas y valida URLs."""
    adapter = LinkedInAssistedAdapter()
    assert adapter.CAPABILITIES["supports_direct_submit"] is False
    assert adapter.CAPABILITIES["mode"] == "assisted_only"
    assert adapter.CAPABILITIES["requires_account"] is True

    # URL no segura (anti-SSRF): Bloqueo inmediato
    res_bad = await adapter.submit_application(
        tenant_id="t1",
        target_url="http://127.0.0.1:8000/internal-api",
        applicant_data={},
    )
    assert res_bad.success is False
    assert res_bad.result_type == "failure"
    assert res_bad.status_code == 400

    # URL pública válida: paquete asistido con checklist
    res_good = await adapter.submit_application(
        tenant_id="t1",
        target_url="https://www.linkedin.com/jobs/view/1234567890",
        applicant_data={},
    )
    assert res_good.success is True
    assert res_good.result_type == "uncertain"
    assert "checklist" in res_good.evidence
    assert len(res_good.evidence["checklist"]) == 5


@pytest.mark.asyncio
async def test_portal_factory_resolves_linkedin_assisted():
    """Verifica que la fábrica de portales retorna LinkedInAssistedAdapter para linkedin."""
    adapter = get_portal_adapter("linkedin")
    assert isinstance(adapter, LinkedInAssistedAdapter)

    adapter2 = get_portal_adapter("linkedin_jobs")
    assert isinstance(adapter2, LinkedInAssistedAdapter)


@pytest.mark.asyncio
async def test_linkedin_assisted_worker_execution_pauses_for_intervention(setup_db, test_tenant_id: str):
    """
    Verifica que cuando el worker procesa una tarea programada para LinkedIn:
    - No realiza envíos directos salientes.
    - El resultado incierto transiciona la tarea y CRM a 'requires_intervention'.
    """
    async with async_session_factory() as session:
        # Perfil y CV
        p = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            full_name="Candidato LinkedIn",
            email="candidato.li@test.org",
            location_city="Guadalajara",
            location_country="México",
            availability="immediate",
        )
        session.add(p)
        await session.flush()
        cv = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            profile_id=p.id,
            name="CV Tech Lead",
            specialization="Tech Lead",
        )
        session.add(cv)

        # Vacante en LinkedIn
        v = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="LinkedIn Partner Corp",
            job_title="Senior Backend Engineer",
            source="linkedin",
            source_url="https://www.linkedin.com/jobs/view/99887766",
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(v)
        await session.flush()

        # Preparar candidatura dirigida a LinkedIn
        candidacy = await ApprovalService.prepare_candidacy(
            session=session,
            tenant_id=test_tenant_id,
            vacancy=v,
            cv=cv,
            profile=p,
            cover_letter="Estimado reclutador de LinkedIn Partner Corp, adjunto mi postulación...",
        )
        candidacy.target_portal = "linkedin"
        candidacy.target_url = "https://www.linkedin.com/jobs/view/99887766"
        await session.commit()

        # Aprobar candidatura y programar
        approval, task = await ApprovalService.approve_candidacy(
            session=session,
            tenant_id=test_tenant_id,
            candidacy_id=candidacy.id,
            user_timezone="UTC",
        )
        await session.commit()

        # Configurar instante válido dentro de ventana de ejecución con MockClock
        mock_now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        mock_clock = MockClock(mock_now)
        task.execution_instant_utc = mock_now
        task.scheduled_date = mock_now.date()
        await session.commit()

        worker = DurableExecutionWorker(worker_id="li-worker-1", clock=mock_clock)
        claimed = await worker.claim_task(session=session, task_id=task.id, tenant_id=test_tenant_id)
        assert claimed is not None

        success, msg = await worker.execute_task(session=session, task=claimed)
        # El resultado fue incierto/asistido, no un fallo fatal pero no despachado directo
        assert success is False
        assert "incierto" in msg.lower() or "intervención" in msg.lower()

        # Verificar que la tarea quedó como result_uncertain
        refreshed_task = (await session.execute(
            select(ScheduledTask).where(ScheduledTask.id == task.id)
        )).scalar_one()
        assert refreshed_task.status == "result_uncertain"

        # Verificar que el CRM quedó en requires_intervention
        crm = (await session.execute(
            select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id)
        )).scalar_one()
        assert crm.current_status == "requires_intervention"


@pytest.mark.asyncio
async def test_linkedin_assisted_api_endpoints_and_confirmation(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str
):
    """
    Verifica el flujo completo de la API asistida de LinkedIn:
    1. GET /api/v1/candidacies/{id}/linkedin-assisted: retorna checklist y snippets.
    2. POST /api/v1/candidacies/{id}/linkedin-confirm: confirma el envío manual y actualiza CRM.
    """
    async with async_session_factory() as session:
        # Perfil y CV
        p = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            full_name="Ingeniero Asistido",
            email="ing.asistido@ejemplo.com",
            phone="+52 33 1234 5678",
            location_city="Zapopan",
            location_country="México",
            availability="immediate",
        )
        session.add(p)
        await session.flush()
        cv = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            profile_id=p.id,
            name="CV Python Staff",
            specialization="Python Staff",
        )
        session.add(cv)

        v = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="Fintech Innovators",
            job_title="Lead Architect",
            source="linkedin",
            source_url="https://www.linkedin.com/jobs/view/44556677",
            salary_min=80000,
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(v)
        await session.flush()

        candidacy = await ApprovalService.prepare_candidacy(
            session=session,
            tenant_id=test_tenant_id,
            vacancy=v,
            cv=cv,
            profile=p,
            cover_letter="Carta específica para LinkedIn con snippets destacados...",
        )
        candidacy.target_portal = "linkedin"
        candidacy.target_url = "https://www.linkedin.com/jobs/view/44556677"
        await session.commit()

        # Aprobar para avanzar
        approval, task = await ApprovalService.approve_candidacy(
            session=session,
            tenant_id=test_tenant_id,
            candidacy_id=candidacy.id,
            user_timezone="UTC",
        )
        candidacy_id = candidacy.id
        await session.commit()

    # 1. Consultar paquete asistido
    res = await client.get(f"/api/v1/candidacies/{candidacy_id}/linkedin-assisted", headers=auth_headers)
    assert res.status_code == 200
    pkg = res.json()

    assert pkg["candidacy_id"] == candidacy_id
    assert pkg["company_name"] == "Fintech Innovators"
    assert pkg["is_safe_url"] is True
    assert "https://www.linkedin.com/jobs/view/44556677" in pkg["target_url"]
    assert len(pkg["checklist"]) >= 4
    assert pkg["is_confirmed"] is False
    assert "Carta específica para LinkedIn" in pkg["cover_letter"]

    # 2. Confirmar la postulación
    confirm_payload = {
        "confirmation_note": "Postulación completada en LinkedIn con éxito.",
        "external_reference": "LI-SUBMIT-20261017",
    }
    confirm_res = await client.post(
        f"/api/v1/candidacies/{candidacy_id}/linkedin-confirm",
        json=confirm_payload,
        headers=auth_headers,
    )
    assert confirm_res.status_code == 200
    c_data = confirm_res.json()
    assert c_data["status"] == "confirmed"
    assert c_data["new_status"] == "sent_confirmed"

    # 3. Verificar estado en base de datos
    async with async_session_factory() as session:
        c_db = (await session.execute(
            select(Candidacy).where(Candidacy.id == candidacy_id)
        )).scalar_one()
        assert c_db.status == "sent_confirmed"

        crm_db = (await session.execute(
            select(CRMEntry).where(CRMEntry.candidacy_id == candidacy_id)
        )).scalar_one()
        assert crm_db.current_status == "sent_confirmed"
        assert "LI-SUBMIT-20261017" in (crm_db.notes or "")

        # Verificar evento de CRM (último evento registrado) con semántica honesta
        event = (await session.execute(
            select(CRMEvent)
            .where(CRMEvent.crm_entry_id == crm_db.id)
            .order_by(CRMEvent.created_at.desc())
        )).scalars().first()
        assert event is not None
        assert event.event_type == "manual_external_submission_declared"
        assert event.details.get("has_automated_portal_evidence") is False
        assert event.details.get("declaration_type") == "user_attestation"

        # Verificar ExecutionLog registrado
        log = (await session.execute(
            select(ExecutionLog)
            .where(ExecutionLog.candidacy_id == candidacy_id)
            .order_by(ExecutionLog.created_at.desc())
        )).scalars().first()
        assert log is not None
        assert log.action == "linkedin_manual_user_declaration"
        assert log.result == "manual_attestation"
        assert log.evidence.get("has_automated_portal_evidence") is False


@pytest.mark.asyncio
async def test_linkedin_confirm_rejects_unapproved_candidacy(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """
    Verifica que la confirmación de LinkedIn rechaza candidaturas que no han sido
    previamente aprobadas (REQ-014, REQ-024, Regla de desarrollo #2).
    """
    async with async_session_factory() as session:
        p = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            full_name="Usuario Sin Aprobar",
            email="unapproved@example.com",
            location_city="Puebla",
            location_country="México",
        )
        session.add(p)
        await session.flush()

        v = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="Pending Corp",
            job_title="Software Engineer",
            source_url="https://www.linkedin.com/jobs/view/99887766",
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(v)
        cv = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            profile_id=p.id,
            name="CV Unapproved",
            specialization="Backend",
        )
        session.add(cv)
        await session.flush()

        candidacy = await ApprovalService.prepare_candidacy(
            session=session,
            tenant_id=test_tenant_id,
            vacancy=v,
            cv=cv,
            profile=p,
        )
        candidacy.target_portal = "linkedin"
        candidacy_id = candidacy.id
        await session.commit()

    # Intentar confirmar directamente mientras está en pending_approval (sin registro de aprobación)
    confirm_res = await client.post(
        f"/api/v1/candidacies/{candidacy_id}/linkedin-confirm",
        json={"confirmation_note": "Intento de envío sin aprobación previa"},
        headers=auth_headers,
    )
    assert confirm_res.status_code == 400
    detail = confirm_res.json()["detail"]
    assert "Aprobación obligatoria" in detail

    # Verificar que el estado no cambió en la base de datos
    async with async_session_factory() as session:
        c_db = (await session.execute(
            select(Candidacy).where(Candidacy.id == candidacy_id)
        )).scalar_one()
        assert c_db.status == "pending_approval"


@pytest.mark.asyncio
async def test_linkedin_assisted_tenant_isolation(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """Verifica que un tenant no puede acceder ni confirmar candidaturas asistidas de otro tenant."""
    other_tenant = f"other-li-tenant-{uuid.uuid4().hex[:8]}"
    other_user_id = str(uuid.uuid4())

    async with async_session_factory() as session:
        t2 = Tenant(id=other_tenant, tenant_id=other_tenant, name="Tenant Dos LinkedIn")
        session.add(t2)
        u2 = User(
            id=other_user_id,
            tenant_id=other_tenant,
            email=f"li_user_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pw123456"),
            full_name="User LinkedIn Dos",
            is_active=True,
        )
        session.add(u2)
        p2 = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            full_name="User LinkedIn Dos",
            email="li_user_dos@example.com",
            location_city="Monterrey",
            location_country="México",
        )
        session.add(p2)
        await session.flush()
        v2 = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            company_name="Private LinkedIn Corp",
            job_title="Private Lead",
            source_url="https://www.linkedin.com/jobs/view/11223344",
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(v2)
        cv2 = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            profile_id=p2.id,
            name="CV Privado",
            specialization="Privado",
        )
        session.add(cv2)
        await session.flush()

        c2 = await ApprovalService.prepare_candidacy(
            session=session,
            tenant_id=other_tenant,
            vacancy=v2,
            cv=cv2,
            profile=p2,
        )
        c2.target_portal = "linkedin"
        c2_id = c2.id
        await session.commit()

    # Tenant 1 intenta acceder a la candidatura de Tenant 2: Debe recibir 404
    res = await client.get(f"/api/v1/candidacies/{c2_id}/linkedin-assisted", headers=auth_headers)
    assert res.status_code == 404

    # Tenant 1 intenta confirmar candidatura de Tenant 2: Debe recibir 400 o 404
    confirm_res = await client.post(
        f"/api/v1/candidacies/{c2_id}/linkedin-confirm",
        json={"confirmation_note": "Ataque cross-tenant"},
        headers=auth_headers,
    )
    assert confirm_res.status_code in (400, 404)


def test_ui_contains_linkedin_assisted_controls():
    """Verifica que index.html contiene el modal y controles para modo asistido de LinkedIn."""
    with open("backend/app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    assert 'id="modalLinkedInAssisted"' in html
    assert 'openLinkedInAssistedModal' in html
    assert 'closeLinkedInAssistedModal' in html
    assert 'submitLinkedInConfirmation' in html
    assert 'linkedinAssistedContent' in html

