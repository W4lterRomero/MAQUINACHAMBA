"""
Pruebas de integración para la interfaz y flujo completo de importación de CV,
revisión humana (aceptar, modificar, rechazar) y gestión de CVs maestros especializados
(REQ-001, REQ-002, REQ-004, REQ-005, D9, D12).
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_ui_html_served_with_cv_import_and_master_cv_elements(client: AsyncClient):
    """Verifica que FastAPI sirve la interfaz HTML con los elementos de importación, revisión y CVs maestros."""
    for path in ["/", "/app"]:
        res = await client.get(path)
        assert res.status_code == 200
        html = res.text

        # 1. Pestañas de navegación
        assert 'id="nav-tab-cv-import"' in html
        assert 'id="nav-tab-master-cvs"' in html
        assert 'id="tab-cv-import"' in html
        assert 'id="tab-master-cvs"' in html

        # 2. Zona de carga y controles de importación
        assert 'id="dropZoneCV"' in html
        assert 'id="fileCVInput"' in html
        assert 'id="btnUploadCV"' in html
        assert 'id="cvUploadProgressBar"' in html
        assert 'id="cvUploadError"' in html
        assert 'id="tblSources"' in html

        # 3. Controles de revisión humana
        assert 'id="btnMarkProfileVerified"' in html
        assert 'id="pendingSkillsContainer"' in html
        assert 'id="pendingExperiencesContainer"' in html
        assert 'id="modalEditSkill"' in html
        assert 'id="modalEditExperience"' in html

        # 4. Gestión de CVs maestros
        assert 'id="frmMasterCVForm"' in html
        assert 'id="mcvName"' in html
        assert 'id="mcvSpecialization"' in html
        assert 'id="mcvTemplate"' in html
        assert 'id="mcvSkillsCheckboxes"' in html
        assert 'id="masterCVsCardsContainer"' in html
        assert 'id="modalMasterCvDetail"' in html


@pytest.mark.asyncio
async def test_cv_import_and_human_review_workflow(client: AsyncClient):
    """
    Flujo integral:
    1. Registro y perfil.
    2. Importación de CV con habilidades y experiencia.
    3. Consulta de pendientes con fragmento de procedencia (source_fragment).
    4. Aceptar habilidad (conserva valores extraídos sin inventar datos).
    5. Modificar habilidad (corrige valores por indicación humana).
    6. Rechazar habilidad (se excluye de verified).
    7. Aceptar experiencia laboral.
    8. Marcar perfil como verificado.
    """
    # 1. Registro
    reg_res = await client.post("/api/v1/auth/register", json={
        "email": "carlos_review@maquinachamba.local",
        "password": "Password123!",
        "full_name": "Carlos Reviewer"
    })
    assert reg_res.status_code == 201
    token = reg_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Perfil inicial
    await client.post("/api/v1/profile", json={
        "full_name": "Carlos Reviewer",
        "email": "carlos_review@maquinachamba.local",
        "location_city": "Bogotá",
        "location_country": "Colombia",
    }, headers=headers)

    # 2. Importar CV con texto sintético
    cv_text = (
        "Carlos Reviewer\n"
        "carlos_review@maquinachamba.local\n\n"
        "Habilidades:\n"
        "- Python avanzado\n"
        "- Rust básico\n"
        "- Docker\n\n"
        "Experiencia:\n"
        "Backend Developer - Tech Solutions\n"
        "2020 - 2023\n"
        "Desarrollo de microservicios y APIs asíncronas."
    )
    imp_res = await client.post("/api/v1/profile/sources/import", json={
        "source_type": "cv_text",
        "source_name": "CV_Carlos.txt",
        "raw_text": cv_text,
    }, headers=headers)
    assert imp_res.status_code == 201
    imp_data = imp_res.json()
    assert imp_data["source"]["verification_status"] == "pending"
    assert len(imp_data["pending_skills"]) >= 3
    assert len(imp_data["pending_experiences"]) >= 1

    # 3. Consultar pendientes
    pending_res = await client.get("/api/v1/profile/items/pending", headers=headers)
    assert pending_res.status_code == 200
    pending_data = pending_res.json()
    skills = pending_data["pending_skills"]
    exps = pending_data["pending_experiences"]

    python_skill = next((s for s in skills if s["name"].lower() == "python"), None)
    rust_skill = next((s for s in skills if s["name"].lower() == "rust"), None)
    docker_skill = next((s for s in skills if s["name"].lower() == "docker"), None)

    assert python_skill is not None
    assert rust_skill is not None
    assert docker_skill is not None
    assert python_skill["proficiency"] == "advanced"
    assert rust_skill["proficiency"] == "beginner"
    assert docker_skill["proficiency"] is None  # Sin nivel inventado
    assert python_skill["source_fragment"] is not None

    assert len(exps) >= 1
    exp_item = exps[0]
    assert "Tech Solutions" in exp_item["company"]
    assert exp_item["source_fragment"] is not None

    # 4. Aceptar habilidad Python (conserva proficiency=advanced sin inventar años)
    act_res = await client.post(
        f"/api/v1/profile/items/skills/{python_skill['id']}/review",
        json={"decision": "accept"},
        headers=headers,
    )
    assert act_res.status_code == 200
    assert act_res.json()["verification_status"] == "verified"
    assert act_res.json()["proficiency"] == "advanced"

    # 5. Modificar habilidad Rust (humano corrige nivel a intermediate y asigna 2.0 años)
    mod_res = await client.post(
        f"/api/v1/profile/items/skills/{rust_skill['id']}/review",
        json={
            "decision": "modify",
            "modifications": {
                "proficiency": "intermediate",
                "years_experience": 2.0,
            }
        },
        headers=headers,
    )
    assert mod_res.status_code == 200
    assert mod_res.json()["verification_status"] == "verified"
    assert mod_res.json()["proficiency"] == "intermediate"
    assert mod_res.json()["years_experience"] == 2.0

    # 6. Rechazar habilidad Docker
    rej_res = await client.post(
        f"/api/v1/profile/items/skills/{docker_skill['id']}/review",
        json={"decision": "reject"},
        headers=headers,
    )
    assert rej_res.status_code == 200
    assert rej_res.json()["verification_status"] == "rejected"

    # 7. Aceptar experiencia laboral
    exp_act_res = await client.post(
        f"/api/v1/profile/items/experiences/{exp_item['id']}/review",
        json={"decision": "accept"},
        headers=headers,
    )
    assert exp_act_res.status_code == 200
    assert exp_act_res.json()["verification_status"] == "verified"

    # 8. Marcar perfil verificado
    ver_res = await client.post("/api/v1/profile/verify", headers=headers)
    assert ver_res.status_code == 200
    assert ver_res.json()["status"] == "verified"

    # 9. Comprobar perfil consolidado
    prof_res = await client.get("/api/v1/profile", headers=headers)
    assert prof_res.status_code == 200
    prof_data = prof_res.json()
    assert prof_data["profile"]["verification_status"] == "verified"

    skill_names = [s["name"] for s in prof_data["skills"]]
    assert "Python" in skill_names
    assert "Rust" in skill_names
    assert "Docker" not in skill_names  # Excluida porque fue rechazada

    exp_titles = [e["title"] for e in prof_data["experiences"]]
    assert "Backend Developer" in exp_titles


@pytest.mark.asyncio
async def test_master_cv_crud_and_selection_lifecycle(client: AsyncClient):
    """
    Verifica el ciclo de vida de CVs maestros especializados (REQ-004):
    - Creación con especialización, plantilla y resumen
    - Listado
    - Actualización con incremento de versión
    - Selección de CV activo
    - Inspección con habilidades y experiencias verificadas
    - Eliminación
    """
    # 1. Registro
    reg = await client.post("/api/v1/auth/register", json={
        "email": "mcv_user@maquinachamba.local",
        "password": "Password123!",
        "full_name": "Dev Master"
    })
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # Perfil y habilidad verificada
    await client.post("/api/v1/profile", json={
        "full_name": "Dev Master",
        "email": "mcv_user@maquinachamba.local",
        "location_city": "Madrid",
        "location_country": "España",
    }, headers=headers)
    await client.post("/api/v1/profile/skills", json={
        "name": "FastAPI",
        "category": "technical",
        "proficiency": "expert",
        "years_experience": 4.0,
        "verification_status": "verified",
    }, headers=headers)

    # 2. Crear dos CVs maestros
    cv1_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Backend Python",
        "specialization": "Backend Architect",
        "template_id": "modern_clean",
        "summary": "Especialista en APIs REST y microservicios",
        "sections": {"included_skills": ["FastAPI"]},
    }, headers=headers)
    assert cv1_res.status_code == 201
    cv1_id = cv1_res.json()["id"]
    assert cv1_res.json()["version"] == 1

    cv2_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV DevOps Cloud",
        "specialization": "Cloud & SRE",
        "template_id": "minimal_tech",
        "summary": "Especialista en Kubernetes y CI/CD",
        "sections": {"included_skills": []},
    }, headers=headers)
    assert cv2_res.status_code == 201
    cv2_id = cv2_res.json()["id"]

    # 3. Listar CVs
    list_res = await client.get("/api/v1/profile/cv", headers=headers)
    assert list_res.status_code == 200
    cv_list = list_res.json()
    assert len(cv_list) >= 2
    ids = [c["id"] for c in cv_list]
    assert cv1_id in ids
    assert cv2_id in ids

    # 4. Seleccionar CV 2 como activo
    sel_res = await client.post(f"/api/v1/profile/cv/{cv2_id}/select", headers=headers)
    assert sel_res.status_code == 200
    assert sel_res.json()["status"] == "active"

    # Verificar que CV2 está activo y CV1 no
    list_res2 = await client.get("/api/v1/profile/cv", headers=headers)
    cv2_obj = next(c for c in list_res2.json() if c["id"] == cv2_id)
    cv1_obj = next(c for c in list_res2.json() if c["id"] == cv1_id)
    assert cv2_obj["is_active"] is True
    assert cv1_obj["is_active"] is False

    # 5. Modificar CV 1 (debe incrementar versión)
    upd_res = await client.put(f"/api/v1/profile/cv/{cv1_id}", json={
        "summary": "Especialista senior en APIs REST de alto rendimiento",
        "template_id": "executive_ats",
    }, headers=headers)
    assert upd_res.status_code == 200
    assert upd_res.json()["version"] == 2
    assert upd_res.json()["template_id"] == "executive_ats"

    # 6. Inspeccionar detalle de CV 1
    detail_res = await client.get(f"/api/v1/profile/cv/{cv1_id}", headers=headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["cv"]["name"] == "CV Backend Python"
    assert detail["cv"]["version"] == 2
    assert any(s["name"] == "FastAPI" for s in detail["verified_skills"])

    # 7. Eliminar CV 2
    del_res = await client.delete(f"/api/v1/profile/cv/{cv2_id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "deleted"

    # Comprobar que ya no aparece
    list_after = await client.get("/api/v1/profile/cv", headers=headers)
    assert not any(c["id"] == cv2_id for c in list_after.json())


@pytest.mark.asyncio
async def test_tenant_isolation_on_master_cv_and_review(client: AsyncClient):
    """Garantía de seguridad: Aislamiento por tenant_id en CVs maestros y revisión (SEC-002)."""
    # Tenant A
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": "user_a@tenant.local",
        "password": "Password123!",
        "full_name": "User A"
    })
    headers_a = {"Authorization": f"Bearer {reg_a.json()['access_token']}"}

    # Tenant B
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": "user_b@tenant.local",
        "password": "Password123!",
        "full_name": "User B"
    })
    headers_b = {"Authorization": f"Bearer {reg_b.json()['access_token']}"}

    # Tenant A crea perfil y CV maestro
    await client.post("/api/v1/profile", json={"full_name": "User A", "email": "a@tenant.local"}, headers=headers_a)
    cv_a_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Tenant A",
        "specialization": "Backend A"
    }, headers=headers_a)
    cv_a_id = cv_a_res.json()["id"]

    # Tenant B intenta consultar, modificar o eliminar CV de Tenant A -> 404
    assert (await client.get(f"/api/v1/profile/cv/{cv_a_id}", headers=headers_b)).status_code == 404
    assert (await client.put(f"/api/v1/profile/cv/{cv_a_id}", json={"name": "Hacked"}, headers=headers_b)).status_code == 404
    assert (await client.delete(f"/api/v1/profile/cv/{cv_a_id}", headers=headers_b)).status_code == 404
    assert (await client.post(f"/api/v1/profile/cv/{cv_a_id}/select", headers=headers_b)).status_code == 404
