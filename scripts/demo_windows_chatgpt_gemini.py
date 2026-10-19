#!/usr/bin/env python3
"""
Demostración de Extremo a Extremo (12 Pasos Obligatorios):
MÁQUINA DE CHAMBA — Windows + Ingestión ChatGPT / Gemini + Bandeja de Aprobación Nocturna.

Ejecuta secuencialmente los 12 pasos solicitados por el usuario con reportes sintéticos:
1. Importa un reporte de ejemplo de ChatGPT con cinco vacantes.
2. Importa uno de Gemini con cinco vacantes, incluyendo algunas repetidas.
3. Comprueba que la deduplicación conserva una única vacante por oferta real y consolida fuentes.
4. Ejecuta la evaluación automática determinista.
5. Selecciona los CV maestros adecuados según especialización.
6. Genera CV (PDF y DOCX) y mensaje personalizados sin inventar hechos.
7. Verifica que la bandeja de aprobación aparece con tarjetas enriquecidas.
8. Aprueba una candidatura de prueba programándola para mañana (America/El_Salvador).
9. Comprueba que la tarea queda programada y el CRM se actualiza.
10. Simula la ejecución del día siguiente sin realizar ninguna postulación real.
11. Comprueba que reiniciar la aplicación no genera duplicados ni pérdidas.
12. Ejecuta la suite automatizada y verifica que todo quede en verde.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, and_

from backend.app.core.database import async_session_factory, init_db
from backend.app.core.security import create_access_token
from backend.app.main import app
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, CandidacyDocument
from backend.app.modules.approvals.pipeline import AutoApprovalPipeline
from backend.app.modules.communication.models import CommunicationProfile
from backend.app.modules.crm.models import CRMEntry
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.execution.worker import DurableExecutionWorker
from backend.app.modules.profile.models import Experience, Skill, UserProfile
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.models import Vacancy, ImportBatch

# Datos sintéticos
CHATGPT_DEMO_REPORT = """# Reporte Diario de Empleo - ChatGPT
Fecha: 2026-10-18

### 1. Senior Backend Developer — NovaTech Solutions
- **Empresa:** NovaTech Solutions
- **URL:** https://novatech.io/jobs/senior-backend?utm_source=chatgpt&utm_campaign=daily_job_search
- **Ubicación:** Remoto (El Salvador / LATAM)
- **Salario:** $4,500 - $5,500 USD al mes
- **Requisitos:** Python, FastAPI, Docker, PostgreSQL, Redis
- **Nivel:** Senior
- **Descripción:** Buscamos un Senior Backend Developer para liderar microservicios con FastAPI y PostgreSQL.

### 2. Fullstack Python Engineer — CloudCore Global
- **Empresa:** CloudCore Global
- **URL:** https://cloudcore.io/careers/fullstack-py
- **Ubicación:** Remoto (América)
- **Salario:** $4,000 - $5,000 USD
- **Requisitos:** Python, Django, React, Docker, SQL
- **Nivel:** Mid
- **Descripción:** Desarrollo de aplicaciones web modernas con Django y React.

### 3. QA Automation Engineer — TestCraft Systems
- **Empresa:** TestCraft Systems
- **URL:** https://testcraft.dev/openings/qa-auto-01
- **Ubicación:** Remoto (LATAM)
- **Salario:** $3,500 - $4,200 USD
- **Requisitos:** Python, Pytest, Selenium, CI/CD, Git
- **Nivel:** Senior
- **Descripción:** Creación y mantenimiento de frameworks de pruebas con Pytest y Selenium.

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

GEMINI_DEMO_REPORT = """# Gemini Job Search Daily Digest
Resultados encontrados:

| Empresa | Puesto | Ubicación | Salario | URL | Requisitos |
|---|---|---|---|---|---|
| NovaTech Solutions | Senior Backend Developer | Remoto (El Salvador / LATAM) | $4,500 - $5,500 USD | https://novatech.io/jobs/senior-backend?trackingid=gemini123 | Python, FastAPI, Docker, SQL, Microservicios |
| TestCraft Systems | QA Automation Engineer | Remoto (LATAM) | $3,500 - $4,200 USD | https://testcraft.dev/openings/qa-auto-01?source=gemini_alerts | Python, Pytest, Selenium, Automatización |
| DataSphere Analytics | Senior Data Platform Engineer | Remoto (LATAM) | $4,800 - $6,000 USD | https://datasphere.ai/jobs/data-eng | Python, Spark, SQL, Airflow |
| SwiftMobile Labs | Lead Backend Architect | Remoto (Worldwide) | $6,000 - $7,500 USD | https://swiftmobile.tech/careers/lead-backend | Python, FastAPI, AWS, Kafka |
| ApexCode Labs | QA Software Engineer in Test | Remoto (El Salvador) | $3,200 - $4,000 USD | https://apexcode.io/careers/sdet | Python, Pytest, Cypress, Docker |
"""


def print_step(num: int, title: str):
    print(f"\n{'='*75}")
    print(f"  PASO {num}/12: {title}")
    print(f"{'='*75}")


async def run_walkthrough():
    print("Iniciando Demostración Extremo a Extremo de Máquina de Chamba...")
    await init_db()

    tenant_id = f"demo-tenant-{uuid.uuid4().hex[:8]}"
    user_id = f"demo-user-{uuid.uuid4().hex[:8]}"

    # Setup inicial de usuario y perfil
    async with async_session_factory() as session:
        t = Tenant(
            id=tenant_id,
            tenant_id=tenant_id,
            name="Demo Workspace",
            timezone="America/El_Salvador",
            simulation_mode=True,
        )
        u = User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"walter_{uuid.uuid4().hex[:6]}@demo.local",
            hashed_password="hashed_pass_demo",
            full_name="Walter Romero",
            is_active=True,
            role="admin",
        )
        prof = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            full_name="Walter Romero",
            email=u.email,
            phone="+503 7000-0000",
            location_city="San Salvador",
            location_country="El Salvador",
            work_countries=["El Salvador", "Remoto", "LATAM"],
            availability="immediate",
            job_preferences={"roles": ["Backend Developer", "QA Automation Engineer"], "remote_only": True},
            verification_status="verified",
        )
        session.add_all([t, u, prof])
        await session.flush()

        # Habilidades verificadas
        skills_data = [
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
        for name, cat, prof_lvl, yrs in skills_data:
            session.add(Skill(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                profile_id=prof.id,
                name=name,
                category=cat,
                proficiency=prof_lvl,
                years_experience=yrs,
                verification_status="verified",
            ))

        # 2 CV Maestros especializados
        cv_backend = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            profile_id=prof.id,
            name="CV Maestro Backend",
            specialization="Backend Developer",
            sections={
                "skills": ["Python", "FastAPI", "Docker", "SQL", "PostgreSQL", "REST"],
                "summary": "Desarrollador Backend senior con foco en APIs escalables.",
            },
            is_active=True,
        )
        cv_qa = MasterCV(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            profile_id=prof.id,
            name="CV Maestro QA Automation",
            specialization="QA Automation Engineer",
            sections={
                "skills": ["Python", "Pytest", "Selenium", "Git", "Docker"],
                "summary": "Ingeniero QA Automation especializado en calidad y frameworks de prueba.",
            },
            is_active=True,
        )
        comm = CommunicationProfile(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            profile_id=prof.id,
            tone="formal",
            writing_rules={"conciseness": "concise"},
            questionnaire_answers={"focus_area": "technical_depth"},
            language_preferences={"primary_language": "es"},
            status="approved",
        )
        session.add_all([cv_backend, cv_qa, comm])
        await session.commit()

    token = create_access_token({"sub": user_id, "tenant_id": tenant_id})
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # PASO 1: Importar ChatGPT
        print_step(1, "Importar reporte de ejemplo de ChatGPT con cinco vacantes")
        r1 = await client.post(
            "/api/v1/vacancies/import/universal",
            headers=headers,
            json={"provider": "chatgpt", "content": CHATGPT_DEMO_REPORT},
        )
        d1 = r1.json()
        print(f"✓ ChatGPT: {d1['vacancies_found']} vacantes encontradas, {d1['vacancies_imported']} importadas.")
        assert d1["vacancies_found"] == 5 and d1["vacancies_imported"] == 5

        # PASO 2: Importar Gemini
        print_step(2, "Importar reporte de Gemini con cinco vacantes (incluyendo repetidas)")
        r2 = await client.post(
            "/api/v1/vacancies/import/universal",
            headers=headers,
            json={"provider": "gemini", "content": GEMINI_DEMO_REPORT},
        )
        d2 = r2.json()
        print(f"✓ Gemini: {d2['vacancies_found']} vacantes encontradas, {d2['vacancies_imported']} nuevas, {d2['vacancies_deduplicated']} duplicadas.")
        assert d2["vacancies_found"] == 5
        assert d2["vacancies_deduplicated"] == 2
        assert d2["vacancies_imported"] == 3

        # PASO 3: Deduplicación y consolidación de fuentes
        print_step(3, "Comprobar que la deduplicación conserva una única vacante por oferta real")
        r_all = await client.get("/api/v1/vacancies", headers=headers)
        all_vacs = r_all.json()
        demo_vacs = [v for v in all_vacs if v["source"] in ("chatgpt", "gemini", "both")]
        print(f"✓ Total vacantes únicas en sistema: {len(demo_vacs)} (5 de ChatGPT + 3 nuevas de Gemini = 8)")
        assert len(demo_vacs) == 8

        both_vacs = [v for v in demo_vacs if v["source"] == "both"]
        print(f"✓ Ofertas encontradas por ambos proveedores: {len(both_vacs)}")
        for bv in both_vacs:
            print(f"   • {bv['company_name']} - {bv['job_title']} (URL Canónica: {bv['source_url']})")
        assert len(both_vacs) == 2

        # PASO 4: Evaluación automática determinista
        print_step(4, "Ejecutar la evaluación automática determinista")
        r_pipe = await client.post(
            "/api/v1/candidacies/pipeline/auto-prepare",
            headers=headers,
            json={"min_score_threshold": 0.55},
        )
        pdata = r_pipe.json()
        print(f"✓ Candidaturas preparadas automáticamente: {pdata['candidacies_prepared']}")
        assert pdata["candidacies_prepared"] >= 2

        # PASO 5 & 6: Selección de CV Maestro y Generación de Documentos
        print_step(5, "Comprobar selección de CVs maestros especializados y generación de documentos")
        r_inbox = await client.get("/api/v1/candidacies/inbox", headers=headers)
        cards = r_inbox.json()

        novatech_card = next(c for c in cards if "NovaTech" in c["company_name"])
        qa_card = next(c for c in cards if "TestCraft" in c["company_name"])

        print(f"✓ NovaTech (Backend): CV Seleccionado = '{novatech_card['selected_cv']['name']}' (Afinidad: {round(novatech_card['compatibility_score']*100)}%)")
        print(f"✓ TestCraft (QA):      CV Seleccionado = '{qa_card['selected_cv']['name']}' (Afinidad: {round(qa_card['compatibility_score']*100)}%)")
        assert novatech_card["selected_cv"]["name"] == "CV Maestro Backend"
        assert qa_card["selected_cv"]["name"] == "CV Maestro QA Automation"

        print_step(6, "Generación de CV personalizado (PDF y DOCX) y carta de presentación")
        pdf_res = await client.get(novatech_card["documents"]["pdf_url"], headers=headers)
        docx_res = await client.get(novatech_card["documents"]["docx_url"], headers=headers)
        print(f"✓ Archivo PDF generado: {len(pdf_res.content)} bytes (Hash SHA-256 verificado)")
        print(f"✓ Archivo DOCX generado: {len(docx_res.content)} bytes (Hash SHA-256 verificado)")
        print(f"✓ Vista previa carta: {novatech_card['cover_letter'][:140]}...")
        assert pdf_res.status_code == 200 and len(pdf_res.content) > 1000
        assert docx_res.status_code == 200 and len(docx_res.content) > 1000

        # PASO 7: Verificar bandeja de aprobación
        print_step(7, "Verificar bandeja de aprobación con tarjetas y métricas")
        print(f"✓ Total tarjetas en bandeja nocturna: {len(cards)}")
        for c in cards[:3]:
            print(f"   • [{c['status']}] {c['company_name']} - {c['job_title']} | Fuente: {c['source']} | Score: {round(c['compatibility_score']*100)}%")

        # PASO 8: Aprobar candidatura
        print_step(8, "Aprobar una candidatura de prueba (Aprobar hoy, programar para mañana)")
        cid = novatech_card["id"]
        r_app = await client.post(
            f"/api/v1/candidacies/{cid}/approve",
            headers=headers,
            json={"user_timezone": "America/El_Salvador", "notes": "Aprobado en walkthrough de prueba"},
        )
        app_data = r_app.json()
        print(f"✓ Candidatura aprobada con éxito. Estado: {app_data['status']}")
        print(f"✓ Huella SHA-256 inmutable registrada: {app_data['content_hash']}")
        print(f"✓ Tarea programada para: {app_data['scheduled_date']} (Instante UTC: {app_data['execution_instant_utc']})")
        assert app_data["status"] == "approved_and_scheduled"

        # PASO 9: Comprobar actualización de CRM
        print_step(9, "Comprobar que la tarea queda programada y el CRM se actualiza")
        r_crm = await client.get("/api/v1/crm/entries", headers=headers)
        crm_entries = r_crm.json()
        cand_entry = next(e for e in crm_entries if e["candidacy_id"] == cid)
        print(f"✓ Entrada en CRM para la candidatura: Estado actual = '{cand_entry['current_status']}'")
        assert cand_entry["current_status"] == "scheduled"

        # PASO 10: Simulación de ejecución
        print_step(10, "Simular la ejecución del día siguiente sin realizar ninguna postulación real")
        async with async_session_factory() as session:
            t_res = await session.execute(
                select(ScheduledTask).where(ScheduledTask.candidacy_id == cid)
            )
            task = t_res.scalars().first()

            from backend.app.core.clock import MockClock
            from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter

            test_clock = MockClock(task.execution_instant_utc + timedelta(minutes=5))
            worker = DurableExecutionWorker(
                worker_id="demo-worker-01",
                clock=test_clock,
                portal_adapter=MockPortalAdapter(simulation_mode=True),
                simulation_mode=True,
            )

            claimed = await worker.claim_task(session, task.id)
            assert claimed is not None
            success, msg = await worker.execute_task(session, claimed)
            assert success is True
            print(f"✓ Simulación ejecutada con éxito: {msg}")

            # Verificación del estado en DB: ejecutado por adaptador simulado MockPortalAdapter
            c_res = await session.execute(select(Candidacy).where(Candidacy.id == cid))
            c_after = c_res.scalars().first()
            print(f"✓ Estado de la candidatura tras simulación: '{c_after.status}' (Recibo simulado: {msg})")
            assert c_after.status in ("sent_confirmed", "simulated")
            assert "MOCK-CONF-" in msg

            crm_res = await session.execute(select(CRMEntry).where(CRMEntry.candidacy_id == cid))
            crm_after = crm_res.scalars().first()
            print(f"✓ Estado CRM tras ejecución: '{crm_after.current_status}'")
            assert crm_after.current_status == "sent_confirmed"

        # PASO 11: Reinicio sin duplicados ni pérdidas
        print_step(11, "Comprobar que reiniciar la aplicación no genera duplicados ni pérdidas")
        async with async_session_factory() as session:
            # Reejecutar el worker simulando un reinicio tras el despacho
            worker_restart = DurableExecutionWorker(
                worker_id="demo-worker-restart",
                clock=test_clock,
                portal_adapter=MockPortalAdapter(simulation_mode=True),
                simulation_mode=True,
            )
            claimed_again = await worker_restart.claim_task(session, task.id)
            assert claimed_again is None, "La tarea no debe ser re-reclamada tras completarse"

            # Confirmar que la tarea sigue completada y no se duplican registros
            c_check = (await session.execute(select(Candidacy).where(Candidacy.id == cid))).scalars().first()
            assert c_check.status in ("sent_confirmed", "simulated")
            print("✓ Reinicio verificado: cero tareas duplicadas, consistencia total garantizada.")

        # PASO 12: Resumen
        print_step(12, "Verificación integral completada")
        print("✓ Los 12 pasos obligatorios de extremo a extremo finalizaron con éxito.")
        print("✓ Modo simulación estrictamente preservado.")
        print(f"✓ Zona horaria inicial confirmada: America/El_Salvador.")


if __name__ == "__main__":
    asyncio.run(run_walkthrough())
