"""
Pruebas de detección determinista de vacantes cerradas y señales de fraude laboral (REQ-009, D5).
Verifica:
1. Reglas deterministas de integridad (depósito previo, pagos en cripto, canales informales, ingresos irreales).
2. Detección de cierre por fecha vencida y por palabras clave en la descripción.
3. Almacenamiento y exposición en API de señales de fraude y estado de vigencia.
4. Bloqueo de preparación de candidatura en vacantes sospechosas o cerradas a menos que se fuerce explícitamente (force_proceed=True).
5. Endpoint de reverificación de estado (/api/v1/vacancies/{id}/verify-status).
6. Aislamiento por tenant_id en vacantes y operaciones de integridad.
7. Presencia de controles en la interfaz web (alertas visuales, badges y confirmación).
"""

from datetime import date, timedelta
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token, hash_password
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService


@pytest.mark.asyncio
async def test_vacancy_integrity_service_fraud_rules():
    """Verifica reglas heurísticas y deterministas de detección de fraude."""
    # 1. Pago por adelantado / depósito previo (alta gravedad)
    res_upfront = VacancyIntegrityService.analyze_vacancy(
        title="Desarrollador Junior",
        description="Se requiere un depósito previo de garantía de $50 USD para el envío de equipo.",
    )
    assert res_upfront["is_fraud_suspect"] is True
    assert any(s["id"] == "upfront_payment" for s in res_upfront["fraud_signals"])

    # 2. Criptomonedas / tarjetas de regalo (alta gravedad)
    res_crypto = VacancyIntegrityService.analyze_vacancy(
        title="Asistente Remoto",
        description="El pago en bitcoin se realiza semanalmente tras completar tareas.",
    )
    assert res_crypto["is_fraud_suspect"] is True
    assert any(s["id"] == "unconventional_payment" for s in res_crypto["fraud_signals"])

    # 3. Canales informales exclusivos (mediana gravedad)
    res_telegram = VacancyIntegrityService.analyze_vacancy(
        title="Soporte Técnico",
        description="Interesados contactar exclusivamente por telegram @soporte_empleo.",
    )
    assert res_telegram["has_warnings"] is True
    assert any(s["id"] == "suspicious_contact_channel" for s in res_telegram["fraud_signals"])

    # 4. Promesa de ingresos irreales / salario anómalo
    res_unrealistic = VacancyIntegrityService.analyze_vacancy(
        title="Capturista de Datos",
        description="Hazte rico desde casa trabajando 2 horas diarias con ingresos garantizados.",
    )
    assert res_unrealistic["is_fraud_suspect"] is True
    assert any(s["id"] == "unrealistic_income_promise" for s in res_unrealistic["fraud_signals"])

    res_salary_outlier = VacancyIntegrityService.analyze_vacancy(
        title="Auxiliar Administrativo",
        description="Tareas de archivo y recepción de documentos.",
        salary_max=400000.0,
        salary_currency="USD",
    )
    assert res_salary_outlier["is_fraud_suspect"] is True

    # 5. Vacante legítima sin señales de fraude
    res_clean = VacancyIntegrityService.analyze_vacancy(
        title="Senior Python Backend Developer",
        description="Empresa de tecnología busca ingeniero con experiencia en FastAPI, PostgreSQL y Docker.",
        salary_min=4000.0,
        salary_max=6000.0,
        salary_currency="USD",
    )
    assert res_clean["is_fraud_suspect"] is False
    assert len(res_clean["fraud_signals"]) == 0
    assert res_clean["is_closed"] is False


@pytest.mark.asyncio
async def test_vacancy_integrity_service_closure_detection():
    """Verifica detección de vacantes cerradas o expiradas por fecha y por contenido textual."""
    today = date.today()

    # Expirada por fecha límite vencida
    past_date = today - timedelta(days=5)
    res_expired = VacancyIntegrityService.analyze_vacancy(
        title="Frontend React Developer",
        description="Desarrollo de interfaces en Next.js.",
        closing_date=past_date,
    )
    assert res_expired["is_closed"] is True
    assert res_expired["is_active"] is False
    assert "vencida" in res_expired["closure_reason"].lower()

    # Fecha límite futura: activa
    future_date = today + timedelta(days=30)
    res_future = VacancyIntegrityService.analyze_vacancy(
        title="Frontend React Developer",
        description="Desarrollo de interfaces en Next.js.",
        closing_date=future_date,
    )
    assert res_future["is_closed"] is False
    assert res_future["is_active"] is True

    # Cierre por palabras clave en el texto
    res_kw_closed = VacancyIntegrityService.analyze_vacancy(
        title="DevOps Engineer (Vacante Cerrada)",
        description="Agradecemos el interés. La posición ya no se encuentra disponible, convocatoria concluida.",
    )
    assert res_kw_closed["is_closed"] is True
    assert res_kw_closed["is_active"] is False
    assert res_kw_closed["closure_reason"] is not None


@pytest.mark.asyncio
async def test_vacancies_api_import_detects_fraud_and_persists(client: AsyncClient, auth_headers: dict):
    """Verifica que el endpoint de creación de vacante ejecuta el análisis de integridad y lo persiste."""
    # 1. Crear vacante con señales de fraude
    payload_fraud = {
        "company_name": "Inversiones Express Global",
        "job_title": "Operador de Fondos",
        "source_url": "https://ejemplo.com/vacante-sospechosa-1",
        "location": "Remoto",
        "remote_type": "remote",
        "requirements": ["Internet"],
        "description": "Se solicita pago por adelantado para kit de inicio con costo administrativo.",
        "salary_min": 1000.0,
        "salary_max": 2000.0,
        "salary_currency": "USD",
        "source": "manual",
    }
    res = await client.post("/api/v1/vacancies", json=payload_fraud, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert data["is_fraud_suspect"] is True
    assert len(data["fraud_signals"]) >= 1
    assert any(s["id"] == "upfront_payment" for s in data["fraud_signals"])
    vac_id = data["id"]

    # 2. Consultar detalle de la vacante y listado
    detail_res = await client.get(f"/api/v1/vacancies/{vac_id}", headers=auth_headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["is_fraud_suspect"] is True
    assert len(detail["fraud_signals"]) >= 1

    list_res = await client.get("/api/v1/vacancies", headers=auth_headers)
    assert list_res.status_code == 200
    items = list_res.json()
    found = next((v for v in items if v["id"] == vac_id), None)
    assert found is not None
    assert found["is_fraud_suspect"] is True


@pytest.mark.asyncio
async def test_verify_status_endpoint(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """Verifica el endpoint dinámico /api/v1/vacancies/{id}/verify-status."""
    vac_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        vac = Vacancy(
            id=vac_id,
            tenant_id=test_tenant_id,
            external_id=f"ext-{uuid.uuid4().hex[:6]}",
            source="manual",
            source_url="https://ejemplo.com/vacante-test-verify",
            company_name="Tech Corp",
            job_title="Cloud Architect",
            description="Convocatoria finalizada gracias por participar.",
            is_active=True,
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(vac)
        await session.commit()

    # Llamar al endpoint de verificación
    verify_res = await client.post(f"/api/v1/vacancies/{vac_id}/verify-status", headers=auth_headers)
    assert verify_res.status_code == 200
    vdata = verify_res.json()
    assert vdata["is_active"] is False
    assert vdata["is_closed"] is True
    assert "cierre" in vdata["closure_reason"].lower()

    # Comprobar actualización en BD
    async with async_session_factory() as session:
        reloaded = (await session.execute(select(Vacancy).where(Vacancy.id == vac_id))).scalar_one()
        assert reloaded.is_active is False
        assert reloaded.closure_reason is not None


@pytest.mark.asyncio
async def test_prepare_candidacy_protection_and_force_proceed(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str
):
    """
    Verifica que la preparación de candidatura se bloquea por defecto
    si la vacante es fraudulenta o está cerrada, requiriendo force_proceed=True explícito.
    """
    profile_id = str(uuid.uuid4())
    cv_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        prof_res = await session.execute(
            select(UserProfile).where(UserProfile.tenant_id == test_tenant_id)
        )
        profile = prof_res.scalars().first()
        if not profile:
            profile = UserProfile(
                id=profile_id,
                tenant_id=test_tenant_id,
                full_name="Candidato Verificado",
                email="candidato@test.org",
                location_city="Ciudad de México",
                location_country="México",
                verification_status="verified",
            )
            session.add(profile)
            await session.flush()
        else:
            profile_id = profile.id
        cv = MasterCV(
            id=cv_id,
            tenant_id=test_tenant_id,
            profile_id=profile_id,
            name="CV Backend",
            specialization="Backend",
            is_active=True,
            version=1,
            sections={"experience": [], "skills": ["Python"]},
        )
        session.add(cv)

        # Vacante fraudulenta
        vac_fraud = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="Empresa Dudosa",
            job_title="Capturista",
            source_url="https://ejemplo.com/vacante-fraud",
            description="Pago por adelantado requerido",
            is_active=True,
            fraud_signals=[{"id": "upfront_payment", "severity": "high", "title": "Cobro previo"}],
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(vac_fraud)

        # Vacante cerrada
        vac_closed = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=test_tenant_id,
            company_name="Empresa Seria",
            job_title="Ingeniero de Datos",
            source_url="https://ejemplo.com/vacante-closed",
            description="Buscamos data engineer",
            is_active=False,
            closure_reason="Posición ya cubierta",
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(vac_closed)
        await session.commit()

    # 1. Intentar preparar candidatura para vacante fraudulenta sin force_proceed
    res_blocked_fraud = await client.post(
        "/api/v1/candidacies/prepare",
        json={"vacancy_id": vac_fraud.id, "cv_id": cv_id},
        headers=auth_headers,
    )
    assert res_blocked_fraud.status_code == 400
    assert "bloqueada por seguridad" in res_blocked_fraud.json()["detail"].lower()

    # 2. Intentar preparar candidatura para vacante cerrada sin force_proceed
    res_blocked_closed = await client.post(
        "/api/v1/candidacies/prepare",
        json={"vacancy_id": vac_closed.id, "cv_id": cv_id},
        headers=auth_headers,
    )
    assert res_blocked_closed.status_code == 400
    assert "bloqueada por seguridad" in res_blocked_closed.json()["detail"].lower()

    # 3. Preparar con force_proceed=True: debe proceder correctamente
    res_forced = await client.post(
        "/api/v1/candidacies/prepare",
        json={"vacancy_id": vac_fraud.id, "cv_id": cv_id, "force_proceed": True},
        headers=auth_headers,
    )
    assert res_forced.status_code == 201
    data_forced = res_forced.json()
    assert data_forced["status"] == "pending_approval"
    assert "content_hash" in data_forced


@pytest.mark.asyncio
async def test_tenant_isolation_on_vacancies(client: AsyncClient, auth_headers: dict):
    """Verifica aislamiento estricto por tenant_id en vacantes y operaciones."""
    other_tenant = "other-tenant-99999999-9999-9999-9999-999999999999"
    other_user_id = "other-user-99999999-9999-9999-9999-999999999999"

    async with async_session_factory() as session:
        t2 = Tenant(id=other_tenant, tenant_id=other_tenant, name="Tenant Dos")
        session.add(t2)
        u2 = User(
            id=other_user_id,
            tenant_id=other_tenant,
            email="tenant2@example.com",
            hashed_password=hash_password("pw12345678"),
            full_name="Usuario Dos",
            is_active=True,
        )
        session.add(u2)
        vac2 = Vacancy(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            company_name="Private Corp",
            job_title="Secret Role",
            source_url="https://ejemplo.com/private-role",
            description="Rol privado del tenant 2",
            dedup_hash=f"dedup-{uuid.uuid4().hex}",
        )
        session.add(vac2)
        await session.commit()

    token2 = create_access_token(data={"sub": other_user_id, "tenant_id": other_tenant})
    headers2 = {"Authorization": f"Bearer {token2}"}

    # Tenant 1 (usando auth_headers) no debe ver la vacante de Tenant 2 en el listado
    list_t1 = await client.get("/api/v1/vacancies", headers=auth_headers)
    assert list_t1.status_code == 200
    assert not any(v["id"] == vac2.id for v in list_t1.json())

    # Tenant 1 no puede verificar el estado de la vacante de Tenant 2
    verify_other = await client.post(f"/api/v1/vacancies/{vac2.id}/verify-status", headers=auth_headers)
    assert verify_other.status_code == 404

    # Tenant 2 sí puede verificar su propia vacante
    verify_own = await client.post(f"/api/v1/vacancies/{vac2.id}/verify-status", headers=headers2)
    assert verify_own.status_code == 200


def test_ui_markup_contains_fraud_and_closure_controls():
    """Verifica que index.html contiene los elementos de interfaz para REQ-009."""
    html_path = "backend/app/static/index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Modal de alerta de fraude / integridad
    assert 'id="evalVacancyIntegrityAlert"' in content
    assert 'id="lblEvalVacancyIntegrityDetails"' in content

    # Campo de fecha de cierre en formulario
    assert 'id="vacClosingDate"' in content

    # Badges de estado en la tabla de vacantes
    assert "badge-fraud" in content or "Sospechosa" in content
    assert "Cerrada" in content

    # Función JS de reverificación
    assert "reverifyVacancyStatus" in content

    # Manejo de confirmación en JavaScript
    assert "force_proceed" in content


def test_migration_additive_compatibility_with_prior_schema(tmp_path):
    """
    Verifica compatibilidad aditiva de migración (Work dictamen b74d90c).
    Actualiza una base de datos con esquema anterior (2a00f58) que tiene api_key_configs
    sin billing_period_month y consumo acumulado existente, asegurando:
    1. Agregado seguro de billing_period_month sin pérdida de datos.
    2. Conservación intacta del consumo actual.
    3. Agregado de closing_date, fraud_signals y closure_reason en vacancies.
    """
    import sqlite3
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from importlib import import_module
    import sqlalchemy as sa

    db_file = tmp_path / "synthetic_legacy.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    # 1. Crear esquema sintético previo a b74d90c (sin billing_period_month)
    cur.execute("""
        CREATE TABLE api_key_configs (
            id VARCHAR(36) PRIMARY KEY,
            tenant_id VARCHAR(36) NOT NULL,
            provider VARCHAR(50) NOT NULL,
            model VARCHAR(100) NOT NULL,
            key_reference VARCHAR(255),
            key_preview VARCHAR(32),
            monthly_budget_usd FLOAT NOT NULL,
            current_month_usage_usd FLOAT NOT NULL,
            reserved_budget_usd FLOAT NOT NULL,
            max_tokens_per_request INTEGER NOT NULL,
            max_concurrent_requests INTEGER NOT NULL,
            is_active BOOLEAN NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)
    # Insertar registro con consumo previo que DEBE conservarse
    cur.execute("""
        INSERT INTO api_key_configs VALUES (
            'cfg-legacy-1', 'tenant-101', 'openai', 'gpt-4o', 'vault://ref', 'sk-...1234',
            10.0, 0.005432, 0.0, 4096, 2, 1, '2026-10-15 12:00:00', '2026-10-15 12:00:00'
        )
    """)

    # Crear tabla vacancies con esquema previo
    cur.execute("""
        CREATE TABLE vacancies (
            id VARCHAR(36) PRIMARY KEY,
            tenant_id VARCHAR(36) NOT NULL,
            external_id VARCHAR(255),
            source VARCHAR(50) NOT NULL,
            source_url TEXT NOT NULL,
            company_name VARCHAR(255) NOT NULL,
            job_title VARCHAR(255) NOT NULL,
            location VARCHAR(255) NOT NULL,
            remote_type VARCHAR(20) NOT NULL,
            salary_min NUMERIC(10, 2),
            salary_max NUMERIC(10, 2),
            salary_currency VARCHAR(3),
            requirements JSON NOT NULL,
            description TEXT NOT NULL,
            posted_date DATE,
            is_active BOOLEAN NOT NULL,
            dedup_hash VARCHAR(64) NOT NULL,
            last_verified_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        INSERT INTO vacancies VALUES (
            'vac-legacy-1', 'tenant-101', 'ext-1', 'manual', 'https://ejemplo.com', 'Acme', 'Dev',
            'Remoto', 'remote', 2000, 3000, 'USD', '[]', 'Desc', '2026-10-10', 1, 'hash123',
            '2026-10-10 10:00:00', '2026-10-10 10:00:00', '2026-10-10 10:00:00'
        )
    """)
    conn.commit()
    conn.close()

    # 2. Ejecutar la función upgrade de la migración usando un engine de SQLAlchemy
    engine = sa.create_engine(f"sqlite:///{db_file}")
    with engine.connect() as sa_conn:
        ctx = MigrationContext.configure(sa_conn)
        op = Operations(ctx)

        # Cargar módulo de migración
        mig = import_module("migrations.versions.1982c954031f_add_fraud_signals_and_closing_date_to_")
        
        # Inyectar op en el contexto de alembic para ejecutar upgrade()
        from alembic import op as alembic_op
        alembic_op._proxy = op
        mig.upgrade()
        sa_conn.commit()

    # 3. Verificar que las columnas fueron agregadas y los datos preservados
    conn_verify = sqlite3.connect(str(db_file))
    cur_v = conn_verify.cursor()

    # Inspeccionar columnas de api_key_configs
    cur_v.execute("PRAGMA table_info(api_key_configs)")
    cols_api = [r[1] for r in cur_v.fetchall()]
    assert "billing_period_month" in cols_api

    # Verificar consumo intacto
    cur_v.execute("SELECT current_month_usage_usd, billing_period_month FROM api_key_configs WHERE id = 'cfg-legacy-1'")
    row = cur_v.fetchone()
    assert row is not None
    assert abs(row[0] - 0.005432) < 1e-7
    assert row[1] == "2026-10"

    # Inspeccionar columnas de vacancies
    cur_v.execute("PRAGMA table_info(vacancies)")
    cols_vac = [r[1] for r in cur_v.fetchall()]
    assert "closing_date" in cols_vac
    assert "fraud_signals" in cols_vac
    assert "closure_reason" in cols_vac

    conn_verify.close()

