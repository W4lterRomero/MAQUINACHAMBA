"""
Pruebas de integración para Dashboard consolidado y Reporte Diario por Correo (REQ-028, D8).
Verifica:
1. Endpoint /api/v1/reports/dashboard-metrics: cálculo correcto de KPIs (vacantes, candidaturas, despachos, IA).
2. Endpoint /api/v1/reports/daily-email/preview: generación determinista de reporte en texto plano y HTML.
3. Rótulo [SIMULACIÓN] en asunto y cuerpo en SIMULATION_MODE=true.
4. Endpoint /api/v1/reports/daily-email/send: despacho simulado seguro sin conexiones SMTP externas.
5. Aislamiento estricto por tenant_id en métricas y reportes.
6. Presencia de controles y visualizaciones en la interfaz web.
"""

from datetime import date, timedelta
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token, hash_password
from backend.app.modules.approvals.models import Approval, Candidacy, ScheduledTask
from backend.app.modules.profile.models import Skill, UserProfile
from backend.app.modules.secrets.models import APIKeyConfig
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.models import Vacancy


@pytest.mark.asyncio
async def test_dashboard_metrics_aggregation(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """Verifica que el dashboard agrega métricas consolidadas del tenant."""
    # Insertar datos de prueba para el tenant
    async with async_session_factory() as session:
        # Vacante activa y sospechosa
        v1 = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="Empresa A",
            job_title="Dev A",
            source_url="https://ejemplo.com/v1",
            is_active=True,
            fraud_signals=[{"id": "upfront_payment", "severity": "high", "title": "Cobro"}],
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(v1)

        # Habilidad verificada
        prof = (await session.execute(select(UserProfile).where(UserProfile.tenant_id == test_tenant_id))).scalars().first()
        if not prof:
            prof = UserProfile(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                full_name="Usuario Metrics",
                email="metrics@test.org",
                location_city="CDMX",
                location_country="México",
            )
            session.add(prof)
            await session.flush()

        s1 = Skill(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            profile_id=prof.id,
            name="Python",
            verification_status="verified",
        )
        session.add(s1)
        await session.commit()

    res = await client.get("/api/v1/reports/dashboard-metrics", headers=auth_headers)
    assert res.status_code == 200
    metrics = res.json()

    assert "vacancies" in metrics
    assert "candidacies" in metrics
    assert "execution" in metrics
    assert "profile" in metrics
    assert "ai_budget" in metrics

    assert metrics["vacancies"]["total"] >= 1
    assert metrics["vacancies"]["suspect"] >= 1
    assert metrics["profile"]["verified_skills"] >= 1


@pytest.mark.asyncio
async def test_daily_email_preview_and_simulation(client: AsyncClient, auth_headers: dict):
    """
    Verifica la generación de la vista previa del reporte diario por correo:
    - Incluye rotulado [SIMULACIÓN] en asunto.
    - Genera versión en texto plano y HTML.
    - Detalla el estado de la búsqueda laboral.
    """
    res = await client.get("/api/v1/reports/daily-email/preview", headers=auth_headers)
    assert res.status_code == 200
    report = res.json()

    assert "[SIMULACIÓN]" in report["subject"]
    assert "text_body" in report
    assert "html_body" in report
    assert report["is_simulation"] is True

    # El texto plano contiene las secciones clave
    body = report["text_body"]
    assert "MÁQUINA DE CHAMBA" in body
    assert "POSTULACIONES PROGRAMADAS" in body
    assert "PRESUPUESTO DE IA" in body


@pytest.mark.asyncio
async def test_daily_email_send_simulated_dispatch(client: AsyncClient, auth_headers: dict):
    """
    Verifica el despacho simulado del reporte:
    - No lanza errores de conexión SMTP.
    - Retorna status='simulated_success'.
    """
    payload = {"recipient_email": "candidato.notificaciones@ejemplo.com"}
    res = await client.post("/api/v1/reports/daily-email/send", json=payload, headers=auth_headers)
    assert res.status_code == 200
    data = res.json()

    assert data["status"] == "simulated_success"
    assert data["is_simulation"] is True
    assert data["recipient"] == "candidato.notificaciones@ejemplo.com"
    assert "[SIMULACIÓN]" in data["subject"]


@pytest.mark.asyncio
async def test_reports_and_metrics_tenant_isolation(client: AsyncClient, test_tenant_id: str):
    """Verifica que las métricas y reportes están estrictamente aislados por tenant."""
    other_tenant = f"other-rep-tenant-{uuid.uuid4().hex[:8]}"
    other_user_id = str(uuid.uuid4())

    async with async_session_factory() as session:
        t2 = Tenant(id=other_tenant, tenant_id=other_tenant, name="Tenant Dos Reportes")
        session.add(t2)
        u2 = User(
            id=other_user_id,
            tenant_id=other_tenant,
            email=f"rep_user_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pw123456"),
            full_name="User Reportes Dos",
            is_active=True,
        )
        session.add(u2)
        # Vacante exclusiva de Tenant 2
        vac_t2 = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            company_name="Private Tenant 2 Corp",
            job_title="Exclusive Role",
            source_url="https://ejemplo.com/t2",
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(vac_t2)
        await session.commit()

    token2 = create_access_token(data={"sub": other_user_id, "tenant_id": other_tenant})
    headers2 = {"Authorization": f"Bearer {token2}"}

    # Tenant 2 consulta sus métricas: exactamente 1 vacante
    m2_res = await client.get("/api/v1/reports/dashboard-metrics", headers=headers2)
    assert m2_res.status_code == 200
    m2 = m2_res.json()
    assert m2["vacancies"]["total"] == 1

    # Tenant 2 previsualiza su reporte: contiene el nombre de su usuario
    rep2_res = await client.get("/api/v1/reports/daily-email/preview", headers=headers2)
    assert rep2_res.status_code == 200
    rep2 = rep2_res.json()
    assert "User Reportes Dos" in rep2["text_body"]


def test_ui_contains_dashboard_and_report_controls():
    """Verifica que index.html contiene el contenedor de dashboard y modal de reporte."""
    with open("backend/app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    assert 'id="tab-dashboard"' in html
    assert 'id="nav-tab-dashboard"' in html
    assert 'id="modalDailyEmailReport"' in html
    assert 'id="dashKpiScheduled"' in html
    assert 'id="dashKpiPendingApproval"' in html
    assert 'loadDashboardMetrics' in html
    assert 'openDailyReportModal' in html
    assert 'dispatchDailyEmailReport' in html
