"""
Pruebas exhaustivas de autenticación, autorización y aislamiento estricto por tenant
(SEC-001, SEC-002, P1 Dictamen Work sobre 43c6136).

Verifica:
1. Acceso sin credenciales a todas las rutas protegidas (401 Unauthorized).
2. Credenciales inválidas, malformadas o expiradas (401 Unauthorized).
3. Intento de suplantación de tenant mediante cabecera X-Tenant-ID (403 Forbidden).
4. Protección en rutas críticas: aprobaciones y ciclo de ejecución.
5. Aislamiento cruzado entre tenants en candidaturas, aprobaciones y ejecución.
"""

from datetime import timedelta
import pytest
from httpx import AsyncClient
from jose import jwt

from backend.app.core.config import get_settings
from backend.app.core.security import create_access_token


@pytest.mark.asyncio
async def test_access_without_credentials_rejected(client: AsyncClient):
    """Verifica que todas las rutas protegidas rechazan peticiones sin credenciales con 401."""
    protected_endpoints = [
        ("GET", "/api/v1/profile"),
        ("POST", "/api/v1/profile"),
        ("GET", "/api/v1/vacancies"),
        ("POST", "/api/v1/vacancies"),
        ("POST", "/api/v1/evaluations"),
        ("POST", "/api/v1/candidacies/prepare"),
        ("GET", "/api/v1/candidacies/00000000-0000-0000-0000-000000000000"),
        ("POST", "/api/v1/candidacies/00000000-0000-0000-0000-000000000000/approve"),
        ("POST", "/api/v1/candidacies/00000000-0000-0000-0000-000000000000/reject"),
        ("GET", "/api/v1/crm/entries"),
        ("GET", "/api/v1/execution/tasks"),
        ("POST", "/api/v1/execution/process-cycle"),
        ("GET", "/api/v1/auth/me"),
    ]

    for method, path in protected_endpoints:
        if method == "GET":
            res = await client.get(path)
        else:
            res = await client.post(path, json={})
        assert res.status_code == 401, f"Ruta {path} ({method}) no retornó 401 sin credenciales: {res.status_code}"
        assert "WWW-Authenticate" in res.headers


@pytest.mark.asyncio
async def test_invalid_and_expired_tokens_rejected(client: AsyncClient):
    """Verifica el rechazo de tokens corruptos, expirados o con firma no autorizada."""
    settings = get_settings()

    # 1. Token con formato totalmente corrupto
    bad_res1 = await client.get("/api/v1/profile", headers={"Authorization": "Bearer not-a-valid-jwt"})
    assert bad_res1.status_code == 401

    # 2. Token firmado con clave secreta incorrecta (ataque de falsificación)
    fake_token = jwt.encode({"sub": "fake-user", "tenant_id": "fake-tenant"}, "wrong-secret", algorithm="HS256")
    bad_res2 = await client.get("/api/v1/profile", headers={"Authorization": f"Bearer {fake_token}"})
    assert bad_res2.status_code == 401

    # 3. Token expirado en el tiempo
    expired_token = create_access_token(
        data={"sub": "user-expired", "tenant_id": "tenant-expired"},
        expires_delta=timedelta(seconds=-60),  # Expiró hace 1 minuto
    )
    bad_res3 = await client.get("/api/v1/profile", headers={"Authorization": f"Bearer {expired_token}"})
    assert bad_res3.status_code == 401

    # 4. Token válido pero con usuario inexistente en base de datos
    nonexistent_user_token = create_access_token(
        data={"sub": "non-existent-user-id", "tenant_id": "some-tenant"}
    )
    bad_res4 = await client.get("/api/v1/profile", headers={"Authorization": f"Bearer {nonexistent_user_token}"})
    assert bad_res4.status_code == 401


@pytest.mark.asyncio
async def test_cross_tenant_spoofing_via_header_rejected_with_403(client: AsyncClient):
    """
    P1 Dictamen Work: Usuario A (tenant A) intenta seleccionar tenant B mediante X-Tenant-ID.
    Debe rechazarse con 403 Forbidden en TODAS las rutas (perfil, aprobaciones, ejecución).
    """
    # Registrar usuario A sin tenant_id (servidor asigna tenant seguro)
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "user_a_sec@example.com",
        "password": "Password123!",
        "full_name": "Usuario A",
    })
    assert reg_a.status_code == 201
    token_a = reg_a.json()["access_token"]

    spoof_headers = {
        "Authorization": f"Bearer {token_a}",
        "X-Tenant-ID": "tenant-foreign-b",  # Intento de operar sobre tenant B
    }

    # 1. Ruta de perfil
    res_prof = await client.get("/api/v1/profile", headers=spoof_headers)
    assert res_prof.status_code == 403
    assert "no tiene permisos sobre el tenant" in res_prof.json()["detail"]

    # 2. Ruta de vacantes
    res_vac = await client.get("/api/v1/vacancies", headers=spoof_headers)
    assert res_vac.status_code == 403

    # 3. Ruta de aprobaciones
    res_app = await client.post(
        "/api/v1/candidacies/some-fake-id/approve",
        json={"notes": "Intento de aprobación en tenant ajeno"},
        headers=spoof_headers,
    )
    assert res_app.status_code == 403

    # 4. Ruta de ejecución (process-cycle)
    res_exec = await client.post("/api/v1/execution/process-cycle", headers=spoof_headers)
    assert res_exec.status_code == 403


@pytest.mark.asyncio
async def test_strict_cross_tenant_data_and_execution_isolation(client: AsyncClient):
    """
    Demuestra que dos usuarios legítimos operan en silos estrictos:
    - Usuario B no puede ver ni aprobar candidaturas creadas por Usuario A.
    - El ciclo de ejecución de Usuario B no despacha tareas de Usuario A.
    """
    # 1. Registrar usuario A (servidor genera ámbito seguro)
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "usera_iso@example.com",
        "password": "Password123!",
    })
    token_a = reg_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # 2. Registrar usuario B (servidor genera ámbito independiente)
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": "userb_iso@example.com",
        "password": "Password123!",
    })
    token_b = reg_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 3. Usuario A crea perfil, vacante y prepara candidatura
    await client.post("/api/v1/profile", json={
        "full_name": "A User",
        "email": "a@example.com",
        "location_city": "Bogota",
        "location_country": "Colombia",
    }, headers=headers_a)
    cv_res = await client.post("/api/v1/profile/cv", json={"name": "CV A", "specialization": "Dev"}, headers=headers_a)

    cv_id_a = cv_res.json()["id"]

    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Company A",
        "job_title": "Python Developer",
        "source_url": "https://company-a.com/job/1",
    }, headers=headers_a)
    vac_id_a = vac_res.json()["id"]

    ev_res = await client.post("/api/v1/evaluations", json={"vacancy_id": vac_id_a, "cv_id": cv_id_a}, headers=headers_a)
    ev_id_a = ev_res.json()["id"]

    prep_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vac_id_a,
        "cv_id": cv_id_a,
        "evaluation_id": ev_id_a,
    }, headers=headers_a)
    cand_id_a = prep_res.json()["id"]

    # 4. Usuario B intenta ver la candidatura de Usuario A -> 404 (no existe en tenant B)
    view_res_b = await client.get(f"/api/v1/candidacies/{cand_id_a}", headers=headers_b)
    assert view_res_b.status_code == 404

    # 5. Usuario B intenta aprobar la candidatura de Usuario A -> 404 (no existe en tenant B)
    app_res_b = await client.post(f"/api/v1/candidacies/{cand_id_a}/approve", json={}, headers=headers_b)
    assert app_res_b.status_code == 404

    # 6. Usuario A aprueba su propia candidatura -> se programa
    app_res_a = await client.post(f"/api/v1/candidacies/{cand_id_a}/approve", json={}, headers=headers_a)
    assert app_res_a.status_code == 200

    # 7. Usuario B ejecuta process-cycle en su tenant -> 0 tareas procesadas
    cycle_res_b = await client.post("/api/v1/execution/process-cycle", headers=headers_b)
    assert cycle_res_b.status_code == 200
    assert len(cycle_res_b.json()["processed_tasks"]) == 0


    # La tarea de Usuario A permanece intacta en tenant A
    tasks_res_a = await client.get("/api/v1/execution/tasks", headers=headers_a)
    assert tasks_res_a.status_code == 200
    assert len(tasks_res_a.json()) == 1
    assert tasks_res_a.json()[0]["candidacy_id"] == cand_id_a


@pytest.mark.asyncio
async def test_unauthorized_registration_into_existing_tenant_rejected(client: AsyncClient):
    """
    REQ P1 Dictamen Work 0473d01:
    - Crear usuario A y datos privados.
    - Registrar B con tenant_id de A.
    - Comprobar rechazo (400) sin crear usuario/membresía y sin emitir token.
    - Con registro normal B recibe ámbito distinto y no puede leer ni aprobar datos de A.
    """
    # 1. Crear usuario A con ámbito generado por el servidor
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "user_a_private@example.com",
        "password": "Password123!",
        "full_name": "Usuario A Legítimo",
    })
    assert reg_a.status_code == 201
    data_a = reg_a.json()
    token_a = data_a["access_token"]
    tenant_a = data_a["tenant_id"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Usuario A crea perfil y vacante privada
    prof_res = await client.post("/api/v1/profile", json={
        "full_name": "Usuario A Legítimo",
        "email": "user_a_private@example.com",
        "location_city": "Bogota",
        "location_country": "Colombia",
    }, headers=headers_a)
    assert prof_res.status_code == 200

    cv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Privado A",
        "specialization": "Backend Seguro",
    }, headers=headers_a)
    assert cv_res.status_code == 201
    cv_id_a = cv_res.json()["id"]

    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Empresa Confidencial A",
        "job_title": "Arquitecto Seguro",
        "source_url": "https://empresa-a.com/jobs/1",
        "requirements": ["Python", "FastAPI"],
    }, headers=headers_a)
    assert vac_res.status_code == 201
    vacancy_a_id = vac_res.json()["id"]

    cand_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vacancy_a_id,
        "cv_id": cv_id_a,
        "cover_letter": "Carta confidencial de A",
    }, headers=headers_a)
    assert cand_res.status_code == 201
    candidacy_a_id = cand_res.json()["id"]

    # 2. Registrar B enviando explícitamente el tenant_id de A
    reg_b_attack = await client.post("/api/v1/auth/register", json={
        "email": "attacker_b@example.com",
        "password": "AttackerPass123!",
        "full_name": "Usuario Malicioso B",
        "tenant_id": tenant_a,
    })

    # 3. Comprobar rechazo (400 Bad Request) sin crear usuario/membresía y sin emitir token
    assert reg_b_attack.status_code == 400
    assert "no permite especificar tenant_id" in reg_b_attack.json()["detail"]
    assert "access_token" not in reg_b_attack.json()

    # Comprobar que no se creó el usuario: login falla con 401
    login_attempt = await client.post("/api/v1/auth/login", json={
        "email": "attacker_b@example.com",
        "password": "AttackerPass123!",
    })
    assert login_attempt.status_code == 401

    # 4. Con registro normal B recibe ámbito distinto
    reg_b_legit = await client.post("/api/v1/auth/register", json={
        "email": "attacker_b@example.com",
        "password": "AttackerPass123!",
        "full_name": "Usuario B Legítimo",
    })
    assert reg_b_legit.status_code == 201
    data_b = reg_b_legit.json()
    token_b = data_b["access_token"]
    tenant_b = data_b["tenant_id"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # El ámbito de B es diferente del de A
    assert tenant_b != tenant_a

    # B no puede leer la candidatura de A (404)
    read_cand_res = await client.get(f"/api/v1/candidacies/{candidacy_a_id}", headers=headers_b)
    assert read_cand_res.status_code == 404

    # B no puede aprobar la candidatura de A (404)
    approve_cand_res = await client.post(
        f"/api/v1/candidacies/{candidacy_a_id}/approve",
        json={"notes": "Intento no autorizado de aprobación"},
        headers=headers_b,
    )
    assert approve_cand_res.status_code == 404

    # B no ve la vacante privada de A en su listado
    vac_list_res = await client.get("/api/v1/vacancies", headers=headers_b)
    assert vac_list_res.status_code == 200
    vac_ids = [v["id"] for v in vac_list_res.json()]
    assert vacancy_a_id not in vac_ids

