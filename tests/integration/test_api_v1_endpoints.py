"""
Pruebas de integración de la API REST v1.
Verifica endpoints de perfil, vacantes, evaluaciones, aprobaciones, CRM,
ciclo de ejecución y aislamiento estricto por tenant_id resuelto del lado del servidor
(REQ-001, 007, 010, 014, 015, 017, 018, 026, 031, SEC-001, SEC-002).
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_api_health(client: AsyncClient):
    """Verifica el endpoint base de health check y simulación activa."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["simulation_mode"] is True


@pytest.mark.asyncio
async def test_profile_and_cv_api_flow(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """REQ-001 y REQ-004: Configuración de perfil profesional y CVs vía API autenticada."""
    headers = auth_headers

    # 1. Crear perfil
    profile_payload = {
        "full_name": "Juan Desarrollador",
        "email": "juan@example.com",
        "phone": "+525512345678",
        "location_city": "Ciudad de México",
        "location_country": "México",
        "work_countries": ["México", "Remoto"],
        "availability": "immediate",
    }
    res = await client.post("/api/v1/profile", json=profile_payload, headers=headers)
    assert res.status_code == 200
    p_data = res.json()
    assert p_data["full_name"] == "Juan Desarrollador"
    assert p_data["tenant_id"] == test_tenant_id

    # 2. Añadir habilidades con estado de verificación
    skill_payload = {
        "name": "Python",
        "category": "technical",
        "proficiency": "advanced",
        "years_experience": 4.5,
        "verification_status": "verified",
    }
    s_res = await client.post("/api/v1/profile/skills", json=skill_payload, headers=headers)
    assert s_res.status_code == 201
    assert s_res.json()["name"] == "Python"
    assert s_res.json()["verification_status"] == "verified"

    # 3. Crear CV maestro especializado
    cv_payload = {
        "name": "CV Backend Python",
        "specialization": "Backend Developer",
        "summary": "Especialista en arquitecturas backend Python y FastAPI.",
        "sections": {"experience": "4 años", "skills": ["Python", "FastAPI"]},
    }
    cv_res = await client.post("/api/v1/profile/cv", json=cv_payload, headers=headers)
    assert cv_res.status_code == 201
    assert cv_res.json()["specialization"] == "Backend Developer"

    # 4. Obtener perfil completo
    get_res = await client.get("/api/v1/profile", headers=headers)
    assert get_res.status_code == 200
    full_data = get_res.json()
    assert full_data["profile"]["full_name"] == "Juan Desarrollador"
    assert len(full_data["skills"]) >= 1
    assert len(full_data["master_cvs"]) >= 1


@pytest.mark.asyncio
async def test_vacancies_api_and_deduplication(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """REQ-007, REQ-008: Registro de vacantes, deduplicación y anti-SSRF."""
    headers = auth_headers

    vacancy_payload = {
        "company_name": "Tech Corp",
        "job_title": "Senior Python Engineer",
        "source_url": "https://careers.techcorp.com/jobs/123",
        "source": "manual",
        "location": "Remoto",
        "remote_type": "remote",
        "salary_min": 3500.0,
        "salary_max": 5000.0,
        "requirements": ["Python", "FastAPI", "SQLAlchemy"],
        "description": "Buscamos desarrollador senior para aplicaciones backend escalables.",
    }

    # 1. Crear vacante
    res = await client.post("/api/v1/vacancies", json=vacancy_payload, headers=headers)
    assert res.status_code == 201
    v_data = res.json()
    assert v_data["status"] == "created"
    assert v_data["company_name"] == "Tech Corp"
    vac_id = v_data["id"]

    # 2. Re-importar la misma vacante -> deduplicación determinista
    dup_res = await client.post("/api/v1/vacancies", json=vacancy_payload, headers=headers)
    assert dup_res.status_code == 201
    dup_data = dup_res.json()
    assert dup_data["status"] == "already_exists"
    assert dup_data["id"] == vac_id

    # 3. URL maliciosa o privada bloqueada por anti-SSRF (SEC-004)
    bad_payload = dict(vacancy_payload)
    bad_payload["source_url"] = "http://127.0.0.1:8080/internal/admin"
    bad_res = await client.post("/api/v1/vacancies", json=bad_payload, headers=headers)
    assert bad_res.status_code == 400
    assert "seguridad" in bad_res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_full_api_candidacy_and_approval_cycle(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """
    REQ-010, REQ-014, REQ-015, REQ-017, REQ-026:
    Flujo integral vía API: Evaluar -> Preparar -> Revisar -> Modificar -> Aprobar -> CRM.
    """
    headers = auth_headers

    # 1. Asegurar perfil y vacante
    await client.post("/api/v1/profile", json={
        "full_name": "Ana Ingeniera",
        "email": "ana@example.com",
        "location_city": "Guadalajara",
        "location_country": "México",
    }, headers=headers)
    await client.post("/api/v1/profile/skills", json={
        "name": "Python",
        "category": "technical",
        "proficiency": "expert",
        "verification_status": "verified",
    }, headers=headers)
    cv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Backend Ana",
        "specialization": "Backend Specialist",
    }, headers=headers)
    cv_id = cv_res.json()["id"]

    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Innovate AI",
        "job_title": "Python Developer",
        "source_url": "https://innovate.ai/jobs/dev-42",
        "requirements": ["Python"],
    }, headers=headers)
    vac_id = vac_res.json()["id"]

    # 2. Evaluar deterministamente (REQ-010)
    ev_res = await client.post("/api/v1/evaluations", json={"vacancy_id": vac_id, "cv_id": cv_id}, headers=headers)
    assert ev_res.status_code == 201
    ev_data = ev_res.json()
    assert ev_data["compatibility_score"] >= 0.5
    assert ev_data["ai_used"] is False

    # 3. Prepara candidatura (REQ-016: sin envíos)
    prep_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vac_id,
        "cv_id": cv_id,
        "evaluation_id": ev_data["id"],
    }, headers=headers)
    assert prep_res.status_code == 201
    cand_data = prep_res.json()
    cand_id = cand_data["id"]
    initial_hash = cand_data["content_hash"]

    # 4. REQ-014: Revisar paquete completo antes de aprobar
    review_res = await client.get(f"/api/v1/candidacies/{cand_id}", headers=headers)
    assert review_res.status_code == 200
    rev_data = review_res.json()
    assert rev_data["candidacy"]["content_hash"] == initial_hash
    assert "Estimado equipo" in rev_data["candidacy"]["cover_letter"]
    assert rev_data["approval"] is None  # Aún no aprobada

    # 5. REQ-015: Aprobar hoy -> programa para mañana
    app_res = await client.post(f"/api/v1/candidacies/{cand_id}/approve", json={
        "user_timezone": "America/Mexico_City",
        "notes": "Aprobado para envío matutino.",
    }, headers=headers)
    assert app_res.status_code == 200
    app_data = app_res.json()
    assert app_data["status"] == "approved_and_scheduled"
    assert app_data["scheduled_task_id"] is not None

    # 6. REQ-017: Modificar contenido -> INVALIDA aprobación previa
    mod_res = await client.post(f"/api/v1/candidacies/{cand_id}/modify", json={
        "new_cover_letter": "Nueva carta de presentación completamente reescrita.",
    }, headers=headers)
    assert mod_res.status_code == 200
    assert mod_res.json()["current_status"] == "pending_approval"

    # Revisar de nuevo: la aprobación anterior quedó invalidada
    rev2_res = await client.get(f"/api/v1/candidacies/{cand_id}", headers=headers)
    assert rev2_res.json()["approval"] is None

    # 7. Re-aprobar paquete modificado
    app2_res = await client.post(f"/api/v1/candidacies/{cand_id}/approve", json={}, headers=headers)
    assert app2_res.status_code == 200

    # 8. REQ-026: Verificar visibilidad en CRM
    crm_res = await client.get("/api/v1/crm/entries", headers=headers)
    assert crm_res.status_code == 200
    crm_entries = crm_res.json()
    assert len(crm_entries) >= 1
    assert any(e["candidacy_id"] == cand_id for e in crm_entries)


@pytest.mark.asyncio
async def test_api_tenant_isolation(client: AsyncClient):
    """
    SEC-001 y SEC-002: Aislamiento absoluto por tenant_id resuelto del lado servidor.
    Verifica que:
    - Petición sin credenciales se rechaza con 401.
    - Usuario Alpha opera en tenant Alpha.
    - Usuario Beta opera en tenant Beta (aislado, no ve perfil de Alpha).
    - Usuario Alpha intentando suplantar a tenant Beta mediante X-Tenant-ID se rechaza con 403 Forbidden.
    """
    # 0. Petición sin credenciales -> 401 Unauthorized
    unauth_res = await client.get("/api/v1/profile")
    assert unauth_res.status_code == 401

    # 1. Registrar e iniciar sesión Usuario Alpha
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "alpha_user@example.com",
        "password": "Password123!",
        "full_name": "Usuario Alpha",
        "tenant_id": "tenant-alpha-0001",
    })
    assert reg_a.status_code == 201
    token_a = reg_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # 2. Registrar e iniciar sesión Usuario Beta
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": "beta_user@example.com",
        "password": "Password123!",
        "full_name": "Usuario Beta",
        "tenant_id": "tenant-beta-0002",
    })
    assert reg_b.status_code == 201
    token_b = reg_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 3. Crear perfil para Usuario Alpha
    p_res = await client.post("/api/v1/profile", json={
        "full_name": "Usuario Alpha",
        "email": "alpha_user@example.com",
        "location_city": "Monterrey",
        "location_country": "México",
    }, headers=headers_a)
    assert p_res.status_code == 200

    # 4. Usuario Beta consulta su perfil -> 404 (no ve los datos de Alpha)
    res_b = await client.get("/api/v1/profile", headers=headers_b)
    assert res_b.status_code == 404

    # 5. Usuario Alpha consulta su perfil -> 200
    res_a = await client.get("/api/v1/profile", headers=headers_a)
    assert res_a.status_code == 200
    assert res_a.json()["profile"]["full_name"] == "Usuario Alpha"

    # 6. P1: Intento de Usuario Alpha de usar X-Tenant-ID para acceder al tenant de Beta -> 403 FORBIDDEN
    spoof_headers = {
        "Authorization": f"Bearer {token_a}",
        "X-Tenant-ID": "tenant-beta-0002",
    }
    spoof_res = await client.get("/api/v1/profile", headers=spoof_headers)
    assert spoof_res.status_code == 403
    assert "no tiene permisos sobre el tenant" in spoof_res.json()["detail"]


@pytest.mark.asyncio
async def test_minimal_ui_served(client: AsyncClient):
    """
    REQ-001, REQ-014, REQ-026, REQ-031:
    Verifica que la interfaz web mínima local se sirve correctamente en / y /app,
    permitiendo operar perfil, vacantes, aprobaciones y CRM sin tocar código.
    """
    for endpoint in ["/", "/app"]:
        res = await client.get(endpoint)
        assert res.status_code == 200
        html = res.text
        assert "Máquina de Chamba" in html
        assert "Modo Simulación Activo" in html
        assert "Perfil y Habilidades" in html
        assert "Revisión y Aprobaciones" in html
        assert "Tablero CRM" in html

