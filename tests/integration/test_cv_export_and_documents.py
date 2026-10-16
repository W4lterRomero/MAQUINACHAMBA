"""
Pruebas de integración para la exportación y descarga local de CVs en PDF y DOCX (REQ-013, D9, D12).
Verifica:
1. Generación y exportación de CV maestro en PDF y DOCX con datos verificados y acentos en español.
2. Formatos binarios válidos (%PDF- para PDF, PK zip para DOCX) y cabeceras Content-Disposition y X-Document-Hash.
3. Exportación desde snapshot congelado de candidatura con registro inmutable en candidacy_documents.
4. Aislamiento estricto por tenant_id (un tenant no puede descargar documentos de otro).
5. Detección y prevención de sustituciones silenciosas de documentos de candidaturas aprobadas.
6. Presencia de botones de descarga en la interfaz servida.
"""

import io
import uuid
import zipfile
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_export_master_cv_pdf_and_docx(client: AsyncClient):
    """
    Verifica la exportación de un CV maestro en formatos PDF y DOCX.
    Comprueba:
    - Bytes binarios válidos.
    - Cabeceras de contenido y hash SHA-256.
    - Preservación de acentos y habilidades verificadas.
    """
    uid = uuid.uuid4().hex[:8]
    reg = await client.post("/api/v1/auth/register", json={
        "email": f"export_{uid}@example.com",
        "password": "Password123!",
        "full_name": "Alejandra Gómez Montes",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Configurar perfil con acentos y caracteres UTF-8
    await client.post("/api/v1/profile", json={
        "full_name": "Alejandra Gómez Montes",
        "email": f"export_{uid}@example.com",
        "phone": "+52 55 9876 5432",
        "location_city": "Querétaro",
        "location_country": "México",
        "job_preferences": {"portfolio_url": "https://github.com/alejandragomez"},
    }, headers=headers)

    # 2. Importar y verificar habilidades y experiencia laboral
    cv_text = (
        "Alejandra Gómez Montes\n"
        "Querétaro, México\n\n"
        "Habilidades:\n"
        "- Python avanzado\n"
        "- FastAPI intermedio\n\n"
        "Experiencia:\n"
        "Líder Técnica - Soluciones Tecnológicas de Querétaro\n"
        "2022 - Presente\n"
        "Desarrollo e integración de arquitecturas resilientes con microservicios."
    )
    await client.post("/api/v1/profile/sources/import", json={
        "source_type": "cv_text",
        "source_name": "CV_Alejandra.txt",
        "raw_text": cv_text,
    }, headers=headers)

    pending = await client.get("/api/v1/profile/items/pending", headers=headers)
    for s in pending.json()["pending_skills"]:
        await client.post(f"/api/v1/profile/items/skills/{s['id']}/review", json={"decision": "accept"}, headers=headers)
    for e in pending.json()["pending_experiences"]:
        await client.post(f"/api/v1/profile/items/experiences/{e['id']}/review", json={"decision": "accept"}, headers=headers)

    await client.post("/api/v1/profile/verify", headers=headers)

    # 3. Crear CV maestro especializado
    mcv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Backend y Arquitectura",
        "specialization": "Arquitectura de Software y Servicios Cloud",
        "summary": "Especialista en desarrollo backend de alta concurrencia y arquitecturas limpias.",
        "sections": {"skills": ["Python", "FastAPI"]},
    }, headers=headers)
    assert mcv_res.status_code == 201
    cv_id = mcv_res.json()["id"]

    # 4. Exportar en formato PDF
    pdf_res = await client.get(f"/api/v1/profile/cv/{cv_id}/export/pdf", headers=headers)
    assert pdf_res.status_code == 200
    assert pdf_res.headers["Content-Type"] == "application/pdf"
    assert "attachment; filename=" in pdf_res.headers["Content-Disposition"]
    assert "CV_Backend_y_Arquitectura" in pdf_res.headers["Content-Disposition"]
    assert "X-Document-Hash" in pdf_res.headers
    assert len(pdf_res.headers["X-Document-Hash"]) == 64  # SHA-256 hex

    pdf_bytes = pdf_res.content
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000

    # 5. Exportar en formato DOCX
    docx_res = await client.get(f"/api/v1/profile/cv/{cv_id}/export/docx", headers=headers)
    assert docx_res.status_code == 200
    assert docx_res.headers["Content-Type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment; filename=" in docx_res.headers["Content-Disposition"]
    assert "X-Document-Hash" in docx_res.headers

    docx_bytes = docx_res.content
    assert docx_bytes.startswith(b"PK")  # ZIP magic bytes

    # Inspeccionar estructura DOCX para verificar integridad de archivo Word
    docx_zip = zipfile.ZipFile(io.BytesIO(docx_bytes))
    assert "word/document.xml" in docx_zip.namelist()
    doc_xml = docx_zip.read("word/document.xml").decode("utf-8")
    assert "Alejandra Gómez Montes" in doc_xml
    assert "Python" in doc_xml
    assert "Arquitectura de Software" in doc_xml


@pytest.mark.asyncio
async def test_export_candidacy_cv_and_document_linking(client: AsyncClient):
    """
    Verifica la exportación desde el snapshot congelado de una candidatura
    y la vinculación inmutable en candidacy_documents.
    """
    uid = uuid.uuid4().hex[:8]
    reg = await client.post("/api/v1/auth/register", json={
        "email": f"cand_doc_{uid}@example.com",
        "password": "Password123!",
        "full_name": "Rodrigo Valenzuela",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Perfil y CV maestro
    await client.post("/api/v1/profile", json={
        "full_name": "Rodrigo Valenzuela",
        "email": f"cand_doc_{uid}@example.com",
        "location_city": "Mérida",
        "location_country": "México",
    }, headers=headers)

    cv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Rodrigo QA",
        "specialization": "Automatización de Pruebas",
    }, headers=headers)
    cv_id = cv_res.json()["id"]

    # Registrar vacante
    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Fintech Segura",
        "job_title": "QA Automation Engineer",
        "source": "portal_mock",
        "source_url": "https://jobs.example.com/qa-auto",
    }, headers=headers)
    vac_id = vac_res.json()["id"]

    # Preparar candidatura
    prep_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vac_id,
        "cv_id": cv_id,
    }, headers=headers)
    assert prep_res.status_code == 201
    cand_id = prep_res.json()["id"]

    # 1. Exportar CV adaptado de la candidatura en PDF
    cand_pdf_res = await client.get(f"/api/v1/candidacies/{cand_id}/export/pdf", headers=headers)
    assert cand_pdf_res.status_code == 200
    assert cand_pdf_res.headers["Content-Type"] == "application/pdf"
    pdf_hash = cand_pdf_res.headers["X-Document-Hash"]
    assert cand_pdf_res.content.startswith(b"%PDF-")

    # 2. Exportar CV adaptado de la candidatura en DOCX
    cand_docx_res = await client.get(f"/api/v1/candidacies/{cand_id}/export/docx", headers=headers)
    assert cand_docx_res.status_code == 200
    assert cand_docx_res.headers["Content-Type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    docx_hash = cand_docx_res.headers["X-Document-Hash"]
    assert cand_docx_res.content.startswith(b"PK")

    # 3. Aprobar la candidatura y verificar que el hash y la versión se preservan sin sobreescrituras silenciosas
    appr_res = await client.post(f"/api/v1/candidacies/{cand_id}/approve", json={
        "user_timezone": "America/Mexico_City",
        "notes": "Aprobación formal con documentos generados",
    }, headers=headers)
    assert appr_res.status_code == 200

    # Tras aprobación, una consulta subsecuente del documento debe coincidir con el hash verificado
    post_appr_pdf = await client.get(f"/api/v1/candidacies/{cand_id}/export/pdf", headers=headers)
    assert post_appr_pdf.status_code == 200
    assert post_appr_pdf.headers["X-Document-Hash"] == pdf_hash


@pytest.mark.asyncio
async def test_tenant_isolation_on_cv_exports(client: AsyncClient):
    """
    Verifica que un usuario de Tenant B no pueda exportar ni descargar
    el CV maestro ni las candidaturas de Tenant A (aislamiento estricto 404).
    """
    uid_a = uuid.uuid4().hex[:8]
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": f"tenant_a_doc_{uid_a}@example.com",
        "password": "Password123!",
        "full_name": "Usuario A Docs",
    })
    token_a = reg_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    uid_b = uuid.uuid4().hex[:8]
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": f"tenant_b_doc_{uid_b}@example.com",
        "password": "Password123!",
        "full_name": "Usuario B Docs",
    })
    token_b = reg_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Tenant A crea perfil y CV maestro
    await client.post("/api/v1/profile", json={
        "full_name": "Usuario A Docs",
        "email": f"tenant_a_doc_{uid_a}@example.com",
    }, headers=headers_a)

    cv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Privado Tenant A",
        "specialization": "Confidencial A",
    }, headers=headers_a)
    cv_id_a = cv_res.json()["id"]

    # Tenant A crea vacante y candidatura
    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Empresa A",
        "job_title": "Puesto A",
        "source": "portal_mock",
        "source_url": "https://jobs.example.com/a",
    }, headers=headers_a)
    vac_id_a = vac_res.json()["id"]

    prep_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vac_id_a,
        "cv_id": cv_id_a,
    }, headers=headers_a)
    cand_id_a = prep_res.json()["id"]

    # 1. Tenant A puede exportar su propio CV
    own_res = await client.get(f"/api/v1/profile/cv/{cv_id_a}/export/pdf", headers=headers_a)
    assert own_res.status_code == 200

    # 2. Tenant B intenta exportar el CV maestro de Tenant A -> 404 Not Found
    cross_cv_pdf = await client.get(f"/api/v1/profile/cv/{cv_id_a}/export/pdf", headers=headers_b)
    assert cross_cv_pdf.status_code == 404

    cross_cv_docx = await client.get(f"/api/v1/profile/cv/{cv_id_a}/export/docx", headers=headers_b)
    assert cross_cv_docx.status_code == 404

    # 3. Tenant B intenta exportar la candidatura de Tenant A -> 404 Not Found
    cross_cand_pdf = await client.get(f"/api/v1/candidacies/{cand_id_a}/export/pdf", headers=headers_b)
    assert cross_cand_pdf.status_code == 404

    cross_cand_docx = await client.get(f"/api/v1/candidacies/{cand_id_a}/export/docx", headers=headers_b)
    assert cross_cand_docx.status_code == 404


@pytest.mark.asyncio
async def test_ui_includes_download_actions_for_cvs_and_candidacies(client: AsyncClient):
    """
    Verifica que la interfaz servida contenga las funciones y referencias
    de descarga para CV maestro y candidatura.
    """
    res = await client.get("/")
    assert res.status_code == 200
    html = res.text

    assert "downloadCVDocument" in html
    assert "/export/pdf" in html
    assert "/export/docx" in html
    assert "📄 Descargar CV (PDF)" in html
    assert "📝 Descargar CV (DOCX)" in html


@pytest.mark.asyncio
async def test_immutability_tampering_detection_on_approved_candidacy(client: AsyncClient):
    """
    Verifica que si un documento ya aprobado sufre una alteración en disco,
    el sistema detecte la violación de integridad y bloquee la entrega con 409 Conflict.
    """
    import os
    uid = uuid.uuid4().hex[:8]
    reg = await client.post("/api/v1/auth/register", json={
        "email": f"tamper_{uid}@example.com",
        "password": "Password123!",
        "full_name": "Hugo Verificador",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/api/v1/profile", json={
        "full_name": "Hugo Verificador",
        "email": f"tamper_{uid}@example.com",
    }, headers=headers)

    cv_res = await client.post("/api/v1/profile/cv", json={
        "name": "CV Inmutable Hugo",
        "specialization": "Seguridad y Auditoría",
    }, headers=headers)
    cv_id = cv_res.json()["id"]

    vac_res = await client.post("/api/v1/vacancies", json={
        "company_name": "Banco Seguro",
        "job_title": "Auditor de Software",
        "source": "portal_mock",
        "source_url": "https://jobs.example.com/auditor",
    }, headers=headers)
    vac_id = vac_res.json()["id"]

    prep_res = await client.post("/api/v1/candidacies/prepare", json={
        "vacancy_id": vac_id,
        "cv_id": cv_id,
    }, headers=headers)
    cand_id = prep_res.json()["id"]

    # 1. Generar documento PDF
    doc_res = await client.get(f"/api/v1/candidacies/{cand_id}/export/pdf", headers=headers)
    assert doc_res.status_code == 200

    # 2. Aprobar candidatura
    appr_res = await client.post(f"/api/v1/candidacies/{cand_id}/approve", json={
        "user_timezone": "America/Mexico_City",
    }, headers=headers)
    assert appr_res.status_code == 200

    # 3. Localizar el archivo en disco y alterar su contenido maliciosamente
    t_id = reg.json()["tenant_id"]
    doc_path = os.path.join("data", "documents", t_id, f"Candidatura_{cand_id[:8]}_CV_v1.pdf")
    assert os.path.exists(doc_path)

    with open(doc_path, "wb") as f:
        f.write(b"%PDF-1.4 TAMPERED_MALICIOUS_BYTES_NOT_MATCHING_APPROVED_HASH")

    # 4. Intentar exportar el documento alterado -> debe ser rechazado con 409 Conflict
    tampered_res = await client.get(f"/api/v1/candidacies/{cand_id}/export/pdf", headers=headers)
    assert tampered_res.status_code == 409
    assert "integridad comprometida" in tampered_res.json()["detail"].lower()

