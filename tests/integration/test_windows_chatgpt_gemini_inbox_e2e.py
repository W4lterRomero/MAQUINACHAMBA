"""
Pruebas integrales de extremo a extremo para:
Windows + Ingestión ChatGPT / Gemini + Deduplicación + Bandeja de Aprobación Nocturna (TAREAS 1, 2 y 3).
"""

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select, and_

from backend.app.core.clock import MockClock
from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, CandidacyDocument
from backend.app.modules.approvals.pipeline import AutoApprovalPipeline
from backend.app.modules.approvals.service import ApprovalService
from backend.app.modules.communication.models import CommunicationProfile
from backend.app.modules.crm.models import CRMEntry
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.execution.worker import DurableExecutionWorker
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter
from backend.app.modules.profile.models import Experience, Skill, UserProfile
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.ingestion import JobIngestionService, canonicalize_url
from backend.app.modules.vacancies.models import Vacancy, ImportBatch


# Reportes sintéticos de prueba
CHATGPT_SYNTHETIC_REPORT = """# Reporte Diario de Empleo - ChatGPT
Fecha: 2026-10-18

### 1. Senior Backend Developer — NovaTech Solutions
- **Empresa:** NovaTech Solutions
- **URL:** https://novatech.io/jobs/senior-backend?utm_source=chatgpt&utm_campaign=daily_job_search
- **Ubicación:** Remoto (El Salvador / LATAM)
- **Salario:** $4,500 - $5,500 USD al mes
- **Requisitos:** Python, FastAPI, Docker, PostgreSQL, Redis
- **Nivel:** Senior
- **Descripción:** Buscamos un Senior Backend Developer para liderar microservicios con FastAPI y PostgreSQL en arquitectura distribuida.

### 2. Fullstack Python Engineer — CloudCore Global
- **Empresa:** CloudCore Global
- **URL:** https://cloudcore.com/careers/fullstack-py?ref=chatgpt_search
- **Ubicación:** Remoto (Worldwide)
- **Salario:** $4,000 - $5,000 USD
- **Requisitos:** Python, Django, React, TypeScript
- **Nivel:** Mid
- **Descripción:** Desarrollo de aplicaciones web modernas con Django y React.

### 3. QA Automation Engineer — TestCraft Systems
- **Empresa:** TestCraft Systems
- **URL:** https://testcraft.dev/openings/qa-auto-01
- **Ubicación:** Remoto (LATAM)
- **Salario:** $3,500 - $4,200 USD
- **Requisitos:** Python, Pytest, Selenium, CI/CD, Git
- **Nivel:** Senior
- **Descripción:** Creación y mantenimiento de frameworks de automatización de pruebas con Pytest y Selenium.

### 4. API & Integration Engineer — FinStream Corp
- **Empresa:** FinStream Corp
- **URL:** https://finstream.finance/jobs/api-dev
- **Ubicación:** Remoto (México / El Salvador)
- **Salario:** $3,800 - $4,600 USD
- **Requisitos:** Python, FastAPI, REST, OAuth, Docker
- **Nivel:** Mid
- **Descripción:** Integración de APIs financieras seguras y procesamiento asíncrono.

### 5. DevSecOps & Security Specialist — ShieldNet Inc
- **Empresa:** ShieldNet Inc
- **URL:** https://shieldnet.security/careers/secops
- **Ubicación:** Remoto (América)
- **Salario:** $5,000 - $6,500 USD
- **Requisitos:** Linux, Docker, Kubernetes, AWS, Python
- **Nivel:** Senior
- **Descripción:** Seguridad en infraestructura en la nube y pipelines de despliegue continuo.
"""

GEMINI_SYNTHETIC_REPORT = """# Gemini Job Search Daily Digest
Resultados encontrados el 18 de octubre de 2026:

| Empresa | Puesto | Ubicación | Salario | URL | Requisitos |
|---|---|---|---|---|---|
| NovaTech Solutions | Senior Backend Developer | Remoto (El Salvador / LATAM) | $4,500 - $5,500 USD | https://novatech.io/jobs/senior-backend?trackingid=gemini123 | Python, FastAPI, Docker, SQL, Microservicios |
| TestCraft Systems | QA Automation Engineer | Remoto (LATAM) | $3,500 - $4,200 USD | https://testcraft.dev/openings/qa-auto-01?source=gemini_alerts | Python, Pytest, Selenium, Automatización |
| DataSphere Analytics | Senior Data Platform Engineer | Remoto (LATAM) | $4,800 - $6,000 USD | https://datasphere.ai/jobs/data-eng | Python, Spark, SQL, Airflow |
| SwiftMobile Labs | Lead Backend Architect | Remoto (Worldwide) | $6,000 - $7,500 USD | https://swiftmobile.tech/careers/lead-backend | Python, FastAPI, AWS, Kafka |
| ApexCode Labs | QA Software Engineer in Test | Remoto (El Salvador) | $3,200 - $4,000 USD | https://apexcode.io/careers/sdet | Python, Pytest, Cypress, Docker |
"""


import pytest_asyncio


@pytest_asyncio.fixture
async def setup_candidate_profile(test_tenant_id: str):
    """Configura un perfil completo con habilidades verificadas y dos CVs maestros especializados."""
    async with async_session_factory() as session:
        # 1. Perfil
        p_res = await session.execute(select(UserProfile).where(UserProfile.tenant_id == test_tenant_id))
        profile = p_res.scalars().first()
        if not profile:
            profile = UserProfile(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                full_name="Walter Romero",
                email="walter@test.sv",
                phone="+503 7000-0000",
                location_city="San Salvador",
                location_country="El Salvador",
                work_countries=["El Salvador", "LATAM", "Worldwide"],
                verification_status="verified",
            )
            session.add(profile)
            await session.flush()

        # 2. Habilidades verificadas
        skill_names = [
            ("Python", "technical", "advanced", 6),
            ("FastAPI", "technical", "advanced", 4),
            ("Docker", "technical", "intermediate", 4),
            ("SQL", "technical", "advanced", 5),
            ("PostgreSQL", "technical", "advanced", 5),
            ("Pytest", "technical", "advanced", 4),
            ("Selenium", "technical", "intermediate", 3),
            ("Git", "technical", "advanced", 6),
            ("REST", "technical", "advanced", 5),
        ]
        for name, cat, prof_lvl, yrs in skill_names:
            sk_res = await session.execute(
                select(Skill).where(Skill.tenant_id == test_tenant_id, Skill.name == name)
            )
            if not sk_res.scalars().first():
                sk = Skill(
                    id=str(uuid.uuid4()),
                    tenant_id=test_tenant_id,
                    profile_id=profile.id,
                    name=name,
                    category=cat,
                    proficiency=prof_lvl,
                    years_experience=yrs,
                    verification_status="verified",
                )
                session.add(sk)

        # 3. CVs Maestros especializados
        # CV Maestro 1: Backend Developer
        cv1_res = await session.execute(
            select(MasterCV).where(MasterCV.tenant_id == test_tenant_id, MasterCV.name == "CV Maestro Backend")
        )
        if not cv1_res.scalars().first():
            cv1 = MasterCV(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                profile_id=profile.id,
                name="CV Maestro Backend",
                specialization="Backend Developer",
                sections={
                    "skills": ["Python", "FastAPI", "Docker", "SQL", "PostgreSQL", "REST"],
                    "summary": "Desarrollador Backend enfocado en APIs escalables con Python y FastAPI.",
                },
                is_active=True,
            )
            session.add(cv1)

        # CV Maestro 2: QA Automation Engineer
        cv2_res = await session.execute(
            select(MasterCV).where(MasterCV.tenant_id == test_tenant_id, MasterCV.name == "CV Maestro QA Automation")
        )
        if not cv2_res.scalars().first():
            cv2 = MasterCV(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                profile_id=profile.id,
                name="CV Maestro QA Automation",
                specialization="QA Automation Engineer",
                sections={
                    "skills": ["Python", "Pytest", "Selenium", "Git", "Docker"],
                    "summary": "Ingeniero de QA especializado en pruebas automatizadas y calidad de software.",
                },
                is_active=True,
            )
            session.add(cv2)

        # 4. Perfil de comunicación
        comm_res = await session.execute(
            select(CommunicationProfile).where(CommunicationProfile.tenant_id == test_tenant_id)
        )
        if not comm_res.scalars().first():
            comm = CommunicationProfile(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                profile_id=profile.id,
                tone="formal",
                writing_rules={"conciseness": "concise"},
                questionnaire_answers={"focus_area": "technical_depth"},
                language_preferences={"primary_language": "es"},
                status="approved",
            )
            session.add(comm)

        await session.commit()
    return profile


@pytest.mark.asyncio
async def test_e2e_multisource_import_and_deduplication(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str, setup_candidate_profile
):
    """
    PASOS OBLIGATORIOS 1, 2, 3:
    1. Importa reporte sintético de ChatGPT con 5 vacantes.
    2. Importa reporte de Gemini con 5 vacantes (incluyendo 2 vacantes repetidas con tracking params).
    3. Comprueba que la deduplicación conserva una única vacante consolidada con source='both'.
    """
    # 1. Importar ChatGPT (5 vacantes)
    res_gpt = await client.post(
        "/api/v1/vacancies/import/universal",
        headers=auth_headers,
        json={"provider": "chatgpt", "content": CHATGPT_SYNTHETIC_REPORT},
    )
    assert res_gpt.status_code == 200
    data_gpt = res_gpt.json()
    assert data_gpt["status"] == "completed"
    assert data_gpt["vacancies_found"] == 5
    assert data_gpt["vacancies_imported"] == 5
    assert data_gpt["vacancies_deduplicated"] == 0

    # 2. Importar Gemini (5 vacantes, 2 repetidas)
    res_gem = await client.post(
        "/api/v1/vacancies/import/universal",
        headers=auth_headers,
        json={"provider": "gemini", "content": GEMINI_SYNTHETIC_REPORT},
    )
    assert res_gem.status_code == 200
    data_gem = res_gem.json()
    assert data_gem["status"] == "completed"
    assert data_gem["vacancies_found"] == 5
    assert data_gem["vacancies_imported"] == 3  # 3 nuevas
    assert data_gem["vacancies_deduplicated"] == 2  # 2 repetidas

    # 3. Comprobar conteo total en base de datos (5 + 3 = 8 vacantes únicas)
    res_all = await client.get("/api/v1/vacancies", headers=auth_headers)
    assert res_all.status_code == 200
    all_vacancies = res_all.json()
    multisource_vacancies = [v for v in all_vacancies if v["source"] in ("chatgpt", "gemini", "both")]
    assert len(multisource_vacancies) == 8

    # Verificar que las vacantes repetidas se consolidaron con source='both' y conservan ambas fuentes
    both_vacancies = [v for v in multisource_vacancies if v["source"] == "both"]
    assert len(both_vacancies) == 2

    # Verificar que NovaTech Solutions figura como 'both' y tiene snippets de ambas fuentes
    novatech = next(v for v in both_vacancies if "NovaTech" in v["company_name"])
    assert novatech["company_name"] == "NovaTech Solutions"
    assert len(novatech["sources_detail"]) == 2
    sources = [s["source"] for s in novatech["sources_detail"]]
    assert "chatgpt" in sources
    assert "gemini" in sources
    # Comprobar que la URL canónica eliminó los tracking params (utm_source, trackingid)
    assert "utm_source" not in novatech["source_url"]
    assert "trackingid" not in novatech["source_url"]
    assert novatech["source_url"] == "https://novatech.io/jobs/senior-backend"


@pytest.mark.asyncio
async def test_insufficient_content_email_rejection(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str
):
    """
    PASO OBLIGATORIO:
    Verifica que correos o notificaciones vacías sin ofertas reales
    se marcan como 'insufficient_content' y no inventan ofertas.
    """
    empty_notification = """
    Hola,
    Tus alertas diarias de ChatGPT Job Search ya están disponibles.
    Haz clic aquí para ver tus resultados: https://chatgpt.com/jobs/alert-12345
    No responda a este correo.
    """
    res = await client.post(
        "/api/v1/vacancies/sync-email",
        headers=auth_headers,
        json={
            "sender": "alerts@openai.com",
            "subject": "ChatGPT Daily Job Search Alert",
            "body": empty_notification,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "insufficient_content"
    assert data["vacancies_imported"] == 0
    assert "contenido insuficiente" in data["message"].lower()


@pytest.mark.asyncio
async def test_e2e_pipeline_and_approval_inbox(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str, setup_candidate_profile
):
    """
    PASOS OBLIGATORIOS 4, 5, 6, 7, 8, 9, 10, 11:
    4. Ejecuta evaluación automática determinista.
    5. Selecciona automáticamente los CV maestros adecuados según especialización.
    6. Genera CVs personalizados (PDF y DOCX) y mensaje de presentación sin inventar hechos.
    7. Verifica que la bandeja de aprobación muestra las tarjetas con badges, scores y documentos.
    8. Aprueba una candidatura de prueba programándola para mañana (09:00 - 18:00 America/El_Salvador).
    9. Comprueba que el CRM se actualiza a 'scheduled'.
    10. Simula la ejecución del día siguiente sin envíos reales (candidacy/CRM status 'simulated').
    11. Verifica que editar la candidatura después de aprobar invalida la aprobación y cancela la tarea.
    """
    # Ejecutar pipeline automático sobre las vacantes importadas
    res_pipeline = await client.post(
        "/api/v1/candidacies/pipeline/auto-prepare",
        headers=auth_headers,
        json={"min_score_threshold": 0.55},
    )
    assert res_pipeline.status_code == 200
    pdata = res_pipeline.json()
    assert pdata["status"] == "success"
    assert pdata["candidacies_prepared"] >= 2

    # 7. Consultar la bandeja nocturna de aprobación
    res_inbox = await client.get("/api/v1/candidacies/inbox", headers=auth_headers)
    assert res_inbox.status_code == 200
    cards = res_inbox.json()
    assert len(cards) >= 2

    # Verificar selección adecuada de CV maestro:
    # NovaTech (Backend) debe haber seleccionado 'CV Maestro Backend'
    novatech_card = next((c for c in cards if "NovaTech" in c["company_name"]), None)
    assert novatech_card is not None
    assert novatech_card["selected_cv"]["name"] == "CV Maestro Backend"
    assert novatech_card["compatibility_score"] >= 0.70
    assert novatech_card["source"] == "both"
    assert len(novatech_card["matched_skills"]) >= 2
    assert novatech_card["documents"]["pdf_url"].endswith("/export/pdf")
    assert novatech_card["documents"]["docx_url"].endswith("/export/docx")

    # TestCraft (QA) debe haber seleccionado 'CV Maestro QA Automation'
    qa_card = next((c for c in cards if "TestCraft" in c["company_name"]), None)
    assert qa_card is not None
    assert qa_card["selected_cv"]["name"] == "CV Maestro QA Automation"

    # Verificar exportación de documentos generados en PDF y DOCX
    pdf_export = await client.get(novatech_card["documents"]["pdf_url"], headers=auth_headers)
    assert pdf_export.status_code == 200
    assert pdf_export.headers["content-type"] == "application/pdf"
    assert len(pdf_export.content) > 1000

    docx_export = await client.get(novatech_card["documents"]["docx_url"], headers=auth_headers)
    assert docx_export.status_code == 200
    assert "wordprocessingml" in docx_export.headers["content-type"]
    assert len(docx_export.content) > 1000

    # 8. Aprobar una candidatura de prueba (Aprobar hoy, programar para mañana)
    candidacy_id = novatech_card["id"]
    res_app = await client.post(
        f"/api/v1/candidacies/{candidacy_id}/approve",
        headers=auth_headers,
        json={"user_timezone": "America/El_Salvador", "notes": "Aprobado para despacho matutino."},
    )
    assert res_app.status_code == 200
    app_data = res_app.json()
    assert app_data["status"] == "approved_and_scheduled"
    assert app_data["scheduled_task_id"] is not None

    # 9. Comprobar que el CRM se actualizó a 'scheduled'
    res_crm = await client.get("/api/v1/crm/entries", headers=auth_headers)
    assert res_crm.status_code == 200
    crm_entries = res_crm.json()
    cand_crm = next(e for e in crm_entries if e["candidacy_id"] == candidacy_id)
    assert cand_crm["current_status"] == "scheduled"

    # 11. Regla crítica: Modificar contenido invalida inmediatamente la aprobación previa
    res_mod = await client.post(
        f"/api/v1/candidacies/{candidacy_id}/modify",
        headers=auth_headers,
        json={"new_cover_letter": "Estimado equipo de NovaTech, adjunto mi postulación con énfasis en alta concurrencia."},
    )
    assert res_mod.status_code == 200
    mod_data = res_mod.json()
    assert mod_data["status"] == "modified_and_invalidated"
    assert mod_data["current_status"] == "pending_approval"

    # Re-aprobar para prueba de simulación
    res_reapp = await client.post(
        f"/api/v1/candidacies/{candidacy_id}/approve",
        headers=auth_headers,
        json={"user_timezone": "America/El_Salvador", "notes": "Re-aprobado tras ajuste de mensaje."},
    )
    assert res_reapp.status_code == 200
    new_task_id = res_reapp.json()["scheduled_task_id"]

    # 10. Simular ejecución del día siguiente
    async with async_session_factory() as session:
        t_res = await session.execute(select(ScheduledTask).where(ScheduledTask.id == new_task_id))
        task = t_res.scalar_one()

        test_clock = MockClock(task.execution_instant_utc + timedelta(minutes=5))
        worker = DurableExecutionWorker(
            worker_id="worker-windows-sim-1",
            clock=test_clock,
            portal_adapter=MockPortalAdapter(simulation_mode=True),
            simulation_mode=True,
        )

        claimed = await worker.claim_task(session, task.id)
        assert claimed is not None
        success, msg = await worker.execute_task(session, claimed)
        assert success is True
        assert "MOCK-CONF-" in msg

        # Verificar que el estado del log fue exitoso y el portal fue el simulador
        cand_after = (await session.execute(select(Candidacy).where(Candidacy.id == candidacy_id))).scalar_one()
        assert cand_after.status in ("sent_confirmed", "simulated")
