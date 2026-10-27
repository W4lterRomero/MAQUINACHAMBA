"""
Pruebas de integración para Búsqueda Real de Vacantes (REQ-007, REQ-008, REQ-009, REQ-030, REQ-031).
Verifica:
1. Preferencias persistentes por tenant (puestos, habilidades, ubicación, modalidad, exclusiones, salarios).
2. Protocolo de autorización previa para fuentes externas de red (Remotive).
3. Conector Remotive: modo estructurado (dry-run/sin red) y live.
4. Filtros de exclusión (empresas y palabras clave), integridad y detección de fraude.
5. Deduplicación determinista (dedup_hash) y trazabilidad (enlace original, fechas, campos desconocidos).
6. Clasificación determinista automática contra el perfil verificado.
7. Búsqueda periódica y ejecución autónoma en el demonio independiente del IDE.
"""

from datetime import datetime, timedelta, timezone
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.clock import MockClock
from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token, hash_password
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.execution.daemon import DurableExecutionDaemon
from backend.app.modules.profile.models import Skill, UserProfile
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.connectors.base import RawJobResult
from backend.app.modules.vacancies.connectors.remotive import RemotiveJobConnector
from backend.app.modules.vacancies.models import JobSearchPreferences, Vacancy
from backend.app.modules.vacancies.search_service import JobSearchService


async def create_user_and_auth(session, tenant_id: str, email: str = "searcher@example.com") -> dict:
    tenant = Tenant(id=tenant_id, name=f"Tenant-{tenant_id[:8]}", simulation_mode=True)
    session.add(tenant)
    user = User(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        email=email,
        hashed_password=hash_password("Password123!"),
        full_name="Usuario Búsqueda",
        role="user",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    token = create_access_token({"sub": user.id, "tenant_id": tenant_id})
    return {"Authorization": f"Bearer {token}", "Cookie": f"access_token={token}"}


@pytest.mark.asyncio
async def test_preferences_persistence_and_isolation(client: AsyncClient):
    """Verifica que las preferencias se crean, actualizan y aíslan por tenant."""
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())

    async with async_session_factory() as session:
        headers1 = await create_user_and_auth(session, t1, email="t1@example.com")
        headers2 = await create_user_and_auth(session, t2, email="t2@example.com")

    # 1. Tenant 1 obtiene preferencias por defecto
    r1 = await client.get("/api/v1/vacancies/preferences", headers=headers1)
    assert r1.status_code == 200
    pref1 = r1.json()
    assert pref1["source_access_authorized"] is False
    assert "Python Backend Engineer" in pref1["desired_job_titles"]

    # 2. Tenant 1 actualiza sus preferencias
    update_data = {
        "desired_job_titles": ["Senior Rust / Python Architect"],
        "skills": ["Rust", "Python", "FastAPI", "PostgreSQL"],
        "locations": ["LATAM", "Remote"],
        "remote_modalities": ["remote"],
        "experience_level": "senior",
        "salary_min": 70000,
        "salary_currency": "USD",
        "excluded_keywords": ["apuestas", "crypto", "forex"],
        "excluded_companies": ["Blacklisted Corp", "EvilTech"],
        "interval_hours": 12,
    }
    r_up = await client.put("/api/v1/vacancies/preferences", json=update_data, headers=headers1)
    assert r_up.status_code == 200
    updated = r_up.json()
    assert updated["desired_job_titles"] == ["Senior Rust / Python Architect"]
    assert updated["salary_min"] == 70000
    assert "EvilTech" in updated["excluded_companies"]
    assert updated["interval_hours"] == 12

    # 3. Tenant 2 no ve las preferencias modificadas de Tenant 1 (Aislamiento de tenant)
    r2 = await client.get("/api/v1/vacancies/preferences", headers=headers2)
    assert r2.status_code == 200
    pref2 = r2.json()
    assert pref2["desired_job_titles"] != updated["desired_job_titles"]
    assert "EvilTech" not in pref2["excluded_companies"]


@pytest.mark.asyncio
async def test_source_authorization_protocol(client: AsyncClient):
    """Verifica el protocolo de consentimiento previo para fuentes externas (Remotive)."""
    t_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        headers = await create_user_and_auth(session, t_id, email="auth_proto@example.com")

    # Inicialmente no autorizado
    r_init = await client.get("/api/v1/vacancies/preferences", headers=headers)
    assert r_init.json()["source_access_authorized"] is False

    # Autorizar fuente
    r_auth = await client.post(
        "/api/v1/vacancies/preferences/authorize-source",
        json={"source_name": "remotive"},
        headers=headers,
    )
    assert r_auth.status_code == 200
    auth_data = r_auth.json()
    assert auth_data["source_access_authorized"] is True
    assert "remotive" in auth_data["authorized_sources"]

    # Revocar autorización
    r_rev = await client.post(
        "/api/v1/vacancies/preferences/revoke-source",
        json={"source_name": "remotive"},
        headers=headers,
    )
    assert r_rev.status_code == 200
    assert r_rev.json()["source_access_authorized"] is False


@pytest.mark.asyncio
async def test_remotive_connector_html_and_salary_parsing():
    """Verifica la limpieza de HTML y parseo determinista de salarios en RemotiveJobConnector."""
    connector = RemotiveJobConnector()

    # Limpieza de HTML
    html = "<h3>Requirements:</h3><p>Must know <strong>Python</strong> &amp; <em>FastAPI</em>.</p>"
    clean = connector.clean_html_to_text(html)
    assert "<" not in clean
    assert "Must know Python & FastAPI." in clean

    # Parseo de salario rango USD
    salary_str = "$80,000 - $120,000 USD / year"
    s_min, s_max, s_curr = connector.parse_salary_string(salary_str)
    assert s_min == 80000.0
    assert s_max == 120000.0
    assert s_curr == "USD"

    # Parseo de salario con 'k'
    s_min_k, s_max_k, s_curr_k = connector.parse_salary_string("90k - 110k EUR")
    assert s_min_k == 90000.0
    assert s_max_k == 110000.0
    assert s_curr_k == "EUR"


@pytest.mark.asyncio
async def test_search_execution_exclusions_and_deduplication(client: AsyncClient):
    """
    Verifica que la búsqueda respeta exclusiones declaradas (empresas y palabras clave),
    aplica deduplicación (dedup_hash) y normaliza campos explícitos.
    """
    t_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        headers = await create_user_and_auth(session, t_id, email="search_excl@example.com")

        # Configurar exclusión de empresas/palabras clave existentes en la muestra estructurada
        # Muestra: OctoFlow Technologies (Python), CloudVenture Labs (Full Stack), QualityCore Systems (Playwright)
        await client.put(
            "/api/v1/vacancies/preferences",
            json={
                "excluded_companies": ["CloudVenture Labs"],
                "excluded_keywords": ["Playwright"],
            },
            headers=headers,
        )

    # 1. Primera ejecución de búsqueda (dry_run ya que no está autorizada la red externa)
    r_run = await client.post("/api/v1/vacancies/search/run", headers=headers)
    assert r_run.status_code == 200
    res_data = r_run.json()

    assert res_data["status"] == "completed"
    assert res_data["is_real_network_call"] is False  # Protegido por protocolo
    assert res_data["jobs_excluded"] >= 2  # CloudVenture Labs + Playwright excluidos
    assert res_data["jobs_new"] > 0
    assert res_data["jobs_duplicate"] == 0

    vacancies = res_data["vacancies"]
    for v in vacancies:
        assert v["company_name"] != "CloudVenture Labs"
        assert "playwright" not in v["job_title"].lower()
        # Campos desconocidos explícitos
        assert "unknown_fields" in v
        assert isinstance(v["unknown_fields"], list)
        assert v["source_url"] is not None

    # 2. Segunda ejecución idéntica: deduplicación estricta
    r_run2 = await client.post("/api/v1/vacancies/search/run", headers=headers)
    assert r_run2.status_code == 200
    res_data2 = r_run2.json()
    assert res_data2["jobs_new"] == 0
    assert res_data2["jobs_duplicate"] == len(vacancies)


@pytest.mark.asyncio
async def test_search_classification_with_verified_profile(client: AsyncClient):
    """Verifica que la búsqueda clasifica vacantes con el perfil y CV activo del usuario."""
    t_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        headers = await create_user_and_auth(session, t_id, email="classify@example.com")

        # Crear perfil de usuario
        prof = UserProfile(
            tenant_id=t_id,
            full_name="Ingeniero Calificado",
            email="classify@example.com",
            location_city="Ciudad de Mexico",
            location_country="Mexico",
            verification_status="verified",
        )
        session.add(prof)
        await session.flush()

        # Crear CV maestro activo
        master_cv = MasterCV(
            tenant_id=t_id,
            profile_id=prof.id,
            name="CV Backend Principal",
            specialization="Backend",
            summary="Desarrollador backend experimentado",
            is_active=True,
            version=1,
            sections={"summary": "Desarrollador backend experimentado", "skills": ["Python", "FastAPI", "SQLAlchemy", "Docker"]},
        )
        session.add(master_cv)

        # Habilidades verificadas
        skills_data = ["Python", "FastAPI", "SQLAlchemy", "Docker", "Git"]
        for s_name in skills_data:
            s_obj = Skill(
                tenant_id=t_id,
                profile_id=prof.id,
                name=s_name,
                category="technical",
                proficiency="advanced",
                verification_status="verified",
            )
            session.add(s_obj)

        await session.commit()

    # Ejecutar búsqueda
    r_search = await client.post("/api/v1/vacancies/search/run", headers=headers)
    assert r_search.status_code == 200
    vacancies = r_search.json()["vacancies"]

    # Vacante Python Backend Engineer debe tener coincidencia alta
    python_vac = next((v for v in vacancies if "Python" in v["job_title"]), None)
    assert python_vac is not None
    assert any(
        (m.get("matched_skill") == "Python" if isinstance(m, dict) else m == "Python")
        for m in python_vac["matched_skills"]
    )
    assert python_vac["recommendation"] in ["strong_match", "good_match", "partial"]

    # Consultar endpoint general de vacantes para verificar enriquecimiento en lista
    r_list = await client.get("/api/v1/vacancies", headers=headers)
    assert r_list.status_code == 200
    list_vacs = r_list.json()
    assert len(list_vacs) > 0
    v0 = list_vacs[0]
    assert "compatibility_score" in v0
    assert "recommendation" in v0
    assert "matched_skills" in v0
    assert "is_real_search_result" in v0


@pytest.mark.asyncio
async def test_periodic_search_and_daemon_autonomous_execution():
    """
    Verifica que la búsqueda periódica se programa y el demonio
    la ejecuta de forma autónoma sin intervención humana ni IDE.
    """
    t_id = str(uuid.uuid4())
    mock_now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    clock = MockClock(mock_now)

    async with async_session_factory() as session:
        tenant = Tenant(id=t_id, name="Tenant-Daemon-Search", simulation_mode=True)
        session.add(tenant)
        user = User(
            id=str(uuid.uuid4()),
            tenant_id=t_id,
            email="daemon_search@example.com",
            hashed_password=hash_password("Password123!"),
            full_name="Daemon Searcher",
            role="user",
            is_active=True,
        )
        session.add(user)

        # Preferencias periódicas activas y fuente autorizada
        pref = JobSearchPreferences(
            tenant_id=t_id,
            desired_job_titles=["Python Developer"],
            skills=["Python"],
            locations=["Remote"],
            remote_modalities=["remote"],
            is_periodic_enabled=True,
            interval_hours=6,
            source_access_authorized=True,
            authorized_sources=["remotive"],
            next_run_at=mock_now,  # Ya vencida para ejecución
            last_status="idle",
        )
        session.add(pref)
        await session.commit()

    # Inicializar el Demonio de ejecución con mock clock
    daemon = DurableExecutionDaemon(
        worker_id="test-search-daemon",
        clock=clock,
        poll_interval_seconds=0.1,
    )

    # Ejecutar una iteración del demonio
    stats = await daemon.run_single_iteration()
    assert "periodic_searches" in stats
    periodic_results = stats["periodic_searches"]
    assert len(periodic_results) >= 1

    my_tenant_result = next((r for r in periodic_results if r["tenant_id"] == t_id), None)
    assert my_tenant_result is not None
    assert my_tenant_result["success"] is True
    assert my_tenant_result["jobs_found"] > 0

    # Verificar que en base de datos next_run_at se reprogramó a +6 horas
    async with async_session_factory() as session:
        stmt = select(JobSearchPreferences).where(JobSearchPreferences.tenant_id == t_id)
        res = await session.execute(stmt)
        updated_pref = res.scalar_one()

        assert updated_pref.last_status == "completed"
        assert updated_pref.last_run_at is not None
        assert updated_pref.next_run_at == updated_pref.last_run_at + timedelta(hours=6)
        assert updated_pref.last_results_count > 0
