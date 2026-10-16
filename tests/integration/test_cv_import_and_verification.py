"""
Pruebas de integración del Hito 2.1: Importación local de CV con revisión humana.
Verifica:
1. Extracción separada de verificación (todo ítem importado inicia en 'pending').
2. Procedencia obligatoria por dato (source_id vinculante en skills y experiencias).
3. Aceptación, rechazo y modificación explícitos por el usuario.
4. Aislamiento estricto por tenant_id (Usuario B no puede ver ni modificar fuentes de Usuario A).
5. Garantía estricta: ningún hecho en 'pending' o 'rejected' se incorpora a evaluaciones ni candidaturas;
   únicamente hechos con 'verified' son considerados.
6. Carga de archivo multipart y parseo local sin servicios externos.
"""

import pytest
from httpx import AsyncClient

from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.service import DeterministicEvaluationService


@pytest.mark.asyncio
async def test_cv_import_creates_source_and_pending_items_with_provenance(client: AsyncClient):
    """
    Verifica que la importación de un CV crea la fuente profesional y genera habilidades y
    experiencias con procedencia 'source_id' y estado 'pending' obligatorio por defecto.
    """
    # 1. Registrar usuario y obtener token
    reg = await client.post("/api/v1/auth/register", json={
        "email": "candidata_hito21@example.com",
        "password": "PasswordHito21!",
        "full_name": "Ingeniera Hito 2.1",
    })
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Configurar perfil
    prof_res = await client.post("/api/v1/profile", json={
        "full_name": "Ingeniera Hito 2.1",
        "email": "candidata_hito21@example.com",
        "location_city": "Guadalajara",
        "location_country": "México",
    }, headers=headers)
    assert prof_res.status_code == 200

    # 3. Importar CV en formato texto plano
    cv_content = """
    Ingeniera Hito 2.1
    candidata_hito21@example.com
    +52 33 1122 3344
    Guadalajara, México

    Resumen:
    Especialista en desarrollo backend y sistemas distribuidos.

    Habilidades:
    Python, FastAPI, Docker, PostgreSQL, Linux, Git.

    Experiencia:
    Lead Backend Developer - Tech Cloud Solutions
    2020 - 2023
    Diseño e implementación de microservicios con FastAPI y PostgreSQL.
    """

    import_res = await client.post("/api/v1/profile/sources/import", json={
        "filename": "cv_ingeniera_2026.txt",
        "raw_text": cv_content,
    }, headers=headers)
    assert import_res.status_code == 201
    data = import_res.json()

    source = data["source"]
    assert source["source_name"] == "cv_ingeniera_2026.txt"
    assert source["source_type"] == "cv_import_txt"
    assert source["verification_status"] == "pending"  # Extracción != Verificación
    assert len(source["file_hash"]) == 64
    source_id = source["id"]

    # Comprobar procedencia y estado de habilidades
    pending_skills = data["pending_skills"]
    assert len(pending_skills) >= 4
    for skill in pending_skills:
        assert skill["source_id"] == source_id, "Falta procedencia: source_id no coincide con la fuente"
        assert skill["verification_status"] == "pending", "Regla violada: la habilidad no inició en 'pending'"

    # Comprobar procedencia y estado de experiencias
    pending_experiences = data["pending_experiences"]
    assert len(pending_experiences) >= 1
    for exp in pending_experiences:
        assert exp["source_id"] == source_id
        assert exp["verification_status"] == "pending"

    # 4. Consultar listado de fuentes
    list_res = await client.get("/api/v1/profile/sources", headers=headers)
    assert list_res.status_code == 200
    sources_list = list_res.json()
    assert any(s["id"] == source_id for s in sources_list)

    # 5. Consultar pendientes globales
    pend_res = await client.get("/api/v1/profile/items/pending", headers=headers)
    assert pend_res.status_code == 200
    p_data = pend_res.json()
    assert len(p_data["pending_skills"]) >= 4


@pytest.mark.asyncio
async def test_human_review_acceptance_rejection_and_modification(client: AsyncClient):
    """
    Verifica que el usuario puede revisar explícitamente:
    - Aceptar -> pasa a 'verified'
    - Rechazar -> pasa a 'rejected'
    - Modificar -> actualiza campos y pasa a 'verified'
    """
    reg = await client.post("/api/v1/auth/register", json={
        "email": "reviewer_user@example.com",
        "password": "PasswordReview123!",
        "full_name": "Reviewer User",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Reviewer User",
        "email": "reviewer_user@example.com",
        "location_city": "Monterrey",
        "location_country": "México",
    }, headers=headers)

    import_res = await client.post("/api/v1/profile/sources/import", json={
        "filename": "cv_test_review.txt",
        "raw_text": "Nombre: Reviewer\nEmail: reviewer_user@example.com\nHabilidades: Python, Docker, Redis.",
    }, headers=headers)
    skills = import_res.json()["pending_skills"]
    assert len(skills) >= 3

    skill_python = next(s for s in skills if s["name"].lower() == "python")
    skill_docker = next(s for s in skills if s["name"].lower() == "docker")
    skill_redis = next(s for s in skills if s["name"].lower() == "redis")

    # 1. ACEPTAR Python
    acc_res = await client.post(
        f"/api/v1/profile/items/skills/{skill_python['id']}/review",
        json={"decision": "accept"},
        headers=headers,
    )
    assert acc_res.status_code == 200
    assert acc_res.json()["verification_status"] == "verified"

    # 2. RECHAZAR Docker
    rej_res = await client.post(
        f"/api/v1/profile/items/skills/{skill_docker['id']}/review",
        json={"decision": "reject"},
        headers=headers,
    )
    assert rej_res.status_code == 200
    assert rej_res.json()["verification_status"] == "rejected"

    # 3. MODIFICAR Redis (cambiar nombre y años de experiencia)
    mod_res = await client.post(
        f"/api/v1/profile/items/skills/{skill_redis['id']}/review",
        json={
            "decision": "modify",
            "modifications": {
                "name": "Redis In-Memory Caching",
                "proficiency": "expert",
                "years_experience": 4.5,
            },
        },
        headers=headers,
    )
    assert mod_res.status_code == 200
    mod_data = mod_res.json()
    assert mod_data["name"] == "Redis In-Memory Caching"
    assert mod_data["proficiency"] == "expert"
    assert mod_data["years_experience"] == 4.5
    assert mod_data["verification_status"] == "verified"


@pytest.mark.asyncio
async def test_strict_tenant_isolation_on_sources_and_items(client: AsyncClient):
    """
    Verifica que Usuario B de Tenant B no puede ver ni modificar fuentes o elementos de Usuario A.
    """
    # 1. Crear Usuario A
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "tenant_a_cv@example.com",
        "password": "PasswordA123!",
        "full_name": "Usuario A CV",
    })
    token_a = reg_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Usuario A CV",
        "email": "tenant_a_cv@example.com",
        "location_city": "CDMX",
        "location_country": "México",
    }, headers=headers_a)

    import_a = await client.post("/api/v1/profile/sources/import", json={
        "filename": "cv_a.txt",
        "raw_text": "Nombre: A\nEmail: a@example.com\nHabilidades: Python, FastAPI.",
    }, headers=headers_a)
    source_a_id = import_a.json()["source"]["id"]
    skill_a_id = import_a.json()["pending_skills"][0]["id"]

    # 2. Crear Usuario B (Tenant distinto)
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": "tenant_b_cv@example.com",
        "password": "PasswordB123!",
        "full_name": "Usuario B CV",
    })
    token_b = reg_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Usuario B CV",
        "email": "tenant_b_cv@example.com",
        "location_city": "Bogotá",
        "location_country": "Colombia",
    }, headers=headers_b)

    # 3. Usuario B intenta consultar la fuente de Usuario A -> 404
    get_source_res = await client.get(f"/api/v1/profile/sources/{source_a_id}", headers=headers_b)
    assert get_source_res.status_code == 404

    # 4. Usuario B intenta revisar la habilidad de Usuario A -> 404
    rev_res = await client.post(
        f"/api/v1/profile/items/skills/{skill_a_id}/review",
        json={"decision": "accept"},
        headers=headers_b,
    )
    assert rev_res.status_code == 404

    # 5. Usuario B consulta sus pendientes -> vacíos, no ve los de A
    pending_b = await client.get("/api/v1/profile/items/pending", headers=headers_b)
    assert pending_b.status_code == 200
    assert len(pending_b.json()["pending_skills"]) == 0


@pytest.mark.asyncio
async def test_unverified_facts_strictly_excluded_from_evaluation(client: AsyncClient):
    """
    CRITERIO CRÍTICO Hito 2.1:
    Ningún hecho sin verificar ('pending' o 'rejected') se incorpora automáticamente
    a la evaluación ni cuenta para coincidencia con la vacante.
    Solo hechos con 'verified' son considerados.
    """
    # Crear usuario y perfil
    reg = await client.post("/api/v1/auth/register", json={
        "email": "eval_guarantee@example.com",
        "password": "PasswordEval123!",
        "full_name": "Candidato Garantía",
    })
    token = reg.json()["access_token"]
    tenant_id = reg.json()["tenant_id"]
    headers = {"Authorization": f"Bearer {token}"}

    prof_res = await client.post("/api/v1/profile", json={
        "full_name": "Candidato Garantía",
        "email": "eval_guarantee@example.com",
        "location_city": "CDMX",
        "location_country": "México",
    }, headers=headers)
    profile_id = prof_res.json()["id"]

    # Importar CV que contiene 'Python' y 'Docker' (quedan en 'pending')
    import_res = await client.post("/api/v1/profile/sources/import", json={
        "filename": "cv_eval_test.txt",
        "raw_text": "Nombre: Candidato\nEmail: eval@test.com\nHabilidades: Python, Docker.",
    }, headers=headers)
    skills_imported = import_res.json()["pending_skills"]
    python_skill_id = next(s["id"] for s in skills_imported if s["name"].lower() == "python")
    docker_skill_id = next(s["id"] for s in skills_imported if s["name"].lower() == "docker")

    # Crear objetos ORM para prueba directa de evaluación determinista
    profile_orm = UserProfile(
        id=profile_id,
        tenant_id=tenant_id,
        full_name="Candidato Garantía",
        email="eval_guarantee@example.com",
        location_city="CDMX",
        location_country="México",
    )
    cv_orm = MasterCV(
        tenant_id=tenant_id,
        profile_id=profile_id,
        name="CV Backend",
        specialization="Backend Developer",
        summary="Especialista",
        template_id="ats",
        sections={},
    )
    vacancy_orm = Vacancy(
        id="vac-eval-123",
        tenant_id=tenant_id,
        source="manual",
        source_url="https://example.com/job1",
        company_name="TechCorp",
        job_title="Backend Developer",
        location="CDMX, México",
        remote_type="remote",
        requirements=["python", "docker"],
        description="Buscamos backend en Python y Docker.",
        dedup_hash="hash12345",
    )

    # 1. EVALUAR ANTES DE LA REVISIÓN HUMANA (ambas habilidades siguen en 'pending')
    skill_py_orm = Skill(
        id=python_skill_id,
        tenant_id=tenant_id,
        profile_id=profile_id,
        name="Python",
        verification_status="pending",
    )
    skill_dk_orm = Skill(
        id=docker_skill_id,
        tenant_id=tenant_id,
        profile_id=profile_id,
        name="Docker",
        verification_status="pending",
    )

    eval_before = DeterministicEvaluationService.evaluate(
        tenant_id=tenant_id,
        profile=profile_orm,
        cv=cv_orm,
        skills=[skill_py_orm, skill_dk_orm],
        vacancy=vacancy_orm,
    )

    # Debe haber 0 habilidades coincidentes porque NINGUNA está verificada
    assert len(eval_before.matched_skills) == 0, "Se incluyeron habilidades en estado 'pending'"
    assert "python" in eval_before.missing_skills
    assert "docker" in eval_before.missing_skills
    assert eval_before.score_breakdown["skills_score"] == 0.0

    # 2. EL USUARIO REVISA: Acepta 'Python', pero RECHAZA 'Docker'
    await client.post(
        f"/api/v1/profile/items/skills/{python_skill_id}/review",
        json={"decision": "accept"},
        headers=headers,
    )
    await client.post(
        f"/api/v1/profile/items/skills/{docker_skill_id}/review",
        json={"decision": "reject"},
        headers=headers,
    )

    skill_py_orm.verification_status = "verified"
    skill_dk_orm.verification_status = "rejected"

    # 3. RE-EVALUAR DESPUÉS DE REVISIÓN HUMANA
    eval_after = DeterministicEvaluationService.evaluate(
        tenant_id=tenant_id,
        profile=profile_orm,
        cv=cv_orm,
        skills=[skill_py_orm, skill_dk_orm],
        vacancy=vacancy_orm,
    )

    # Únicamente 'Python' (verificada) debe coincidir; 'Docker' (rechazada) debe faltar
    assert len(eval_after.matched_skills) == 1
    assert eval_after.matched_skills[0]["requirement"] == "python"
    assert eval_after.matched_skills[0]["verified"] is True
    assert "docker" in eval_after.missing_skills
    assert eval_after.score_breakdown["skills_score"] == 0.5


@pytest.mark.asyncio
async def test_upload_file_multipart_endpoint(client: AsyncClient):
    """
    Verifica que la carga mediante multipart/form-data (/sources/upload)
    procesa el archivo y retorna la estructura esperada con estado 'pending'.
    """
    reg = await client.post("/api/v1/auth/register", json={
        "email": "multipart_user@example.com",
        "password": "PasswordMulti123!",
        "full_name": "Usuario Multipart",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Usuario Multipart",
        "email": "multipart_user@example.com",
        "location_city": "Mérida",
        "location_country": "México",
    }, headers=headers)

    cv_bytes = b"Nombre: Candidato Multipart\nEmail: multi@test.com\nHabilidades: Python, Linux, Bash."
    files = {"file": ("curriculum_vitae.txt", cv_bytes, "text/plain")}

    upload_res = await client.post("/api/v1/profile/sources/upload", files=files, headers=headers)
    assert upload_res.status_code == 201
    data = upload_res.json()

    assert data["source"]["source_name"] == "curriculum_vitae.txt"
    assert data["source"]["verification_status"] == "pending"
    assert len(data["pending_skills"]) >= 2
    for s in data["pending_skills"]:
        assert s["verification_status"] == "pending"


@pytest.mark.asyncio
async def test_negative_work_synthetic_api_flow(client: AsyncClient):
    """
    PRUEBA INTEGRAL END-TO-END DEL CASO NEGATIVO DE WORK:
    'Persona ficticia\\nInteres en aprender Python. Sin experiencia laboral ni fechas declaradas.'
    Garantía: Cero años/nivel/empleos inventados, TAMPOCO tras aceptar por el usuario.
    """
    reg = await client.post("/api/v1/auth/register", json={
        "email": "work_negative@example.com",
        "password": "PasswordNegative123!",
        "full_name": "Persona ficticia",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Persona ficticia",
        "email": "work_negative@example.com",
        "location_city": "Toluca",
        "location_country": "México",
    }, headers=headers)

    # 1. Importar texto ficticio
    text = "Persona ficticia\nInteres en aprender Python. Sin experiencia laboral ni fechas declaradas."
    import_res = await client.post("/api/v1/profile/sources/import", json={
        "filename": "cv_work_test.txt",
        "raw_text": text,
    }, headers=headers)
    assert import_res.status_code == 201
    data = import_res.json()

    # CERO empleos inventados
    assert len(data["pending_experiences"]) == 0

    # Habilidades: Python con años=None y nivel=None
    skills = data["pending_skills"]
    assert len(skills) == 1
    py = skills[0]
    assert py["name"] == "Python"
    assert py["years_experience"] is None, f"Se inventaron años: {py['years_experience']}"
    assert py["proficiency"] is None, f"Se inventó nivel: {py['proficiency']}"
    assert py["context_type"] == "interest"
    assert py["verification_status"] == "pending"

    # 2. ACEPTAR la habilidad
    accept_res = await client.post(
        f"/api/v1/profile/items/skills/{py['id']}/review",
        json={"decision": "accept"},
        headers=headers,
    )
    assert accept_res.status_code == 200
    acc_data = accept_res.json()

    # CRITERIO DE WORK: Cero años ni niveles inventados TAMPOCO tras aceptar
    assert acc_data["verification_status"] == "verified"
    assert acc_data["years_experience"] is None, f"Se inventaron años tras aceptar: {acc_data['years_experience']}"
    assert acc_data["proficiency"] is None, f"Se inventó nivel tras aceptar: {acc_data['proficiency']}"


@pytest.mark.asyncio
async def test_upload_file_chunked_streaming_limit_rejection(client: AsyncClient):
    """Verifica que el endpoint de carga rechace archivos que excedan 5 MB con lectura acotada."""
    reg = await client.post("/api/v1/auth/register", json={
        "email": "stream_user@example.com",
        "password": "PasswordStream123!",
        "full_name": "Stream User",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Stream User",
        "email": "stream_user@example.com",
        "location_city": "Mérida",
        "location_country": "México",
    }, headers=headers)

    # Crear 5 MB + 64 KB de datos
    oversized = b"A" * (5 * 1024 * 1024 + 64 * 1024)
    files = {"file": ("oversized_cv.txt", oversized, "text/plain")}

    res = await client.post("/api/v1/profile/sources/upload", files=files, headers=headers)
    assert res.status_code == 400
    assert "excede el tamaño máximo" in res.json()["detail"]


@pytest.mark.asyncio
async def test_import_json_size_limit_rejection(client: AsyncClient):
    """Verifica que el endpoint JSON rechace cadenas base64 o texto que excedan 5 MB previo a decodificar."""
    reg = await client.post("/api/v1/auth/register", json={
        "email": "json_limit_user@example.com",
        "password": "PasswordJson123!",
        "full_name": "Json Limit User",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Json Limit User",
        "email": "json_limit_user@example.com",
        "location_city": "Cancún",
        "location_country": "México",
    }, headers=headers)

    # 1. Base64 excesivo
    oversized_b64 = "A" * (7 * 1024 * 1024)
    res_b64 = await client.post("/api/v1/profile/sources/import", json={
        "filename": "huge.pdf",
        "content_base64": oversized_b64,
    }, headers=headers)
    assert res_b64.status_code == 400
    assert "excede el tamaño máximo" in res_b64.json()["detail"]

    # 2. Texto plano excesivo
    oversized_text = "B" * (5 * 1024 * 1024 + 1024)
    res_txt = await client.post("/api/v1/profile/sources/import", json={
        "filename": "huge.txt",
        "raw_text": oversized_text,
    }, headers=headers)
    assert res_txt.status_code == 400
    assert "excede el tamaño máximo" in res_txt.json()["detail"]

