"""
Prueba de integración para el agente de navegador en una página de empleo realista (P0 Item 3 y Item 9).
Demuestra:
1. Navegación e inspección estructurada con Playwright.
2. Detección heurística y llenado determinista de campos ATS (nombre, email, teléfono, carta).
3. Ofuscación de PII en el DOM antes de capturar pantalla de evidencia.
4. Detención ESTRICTA antes del envío (Modo Simulación / Fail-Closed).
5. Generación de recibo inmutable con 'simulated_success'.
6. API de FastAPI (/api/v1/portals/authorizations y /api/v1/portals/browser/preview).
"""

import asyncio
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os
from pathlib import Path
import threading
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from backend.app.main import app
from backend.app.modules.portals.adapters.browser_adapter import BrowserPortalAdapter
from backend.app.modules.portals.factory import get_portal_adapter
from backend.app.core.config import get_settings


# HTML realista simulando un portal de empleo moderno (tipo Greenhouse / Lever)
JOB_PAGE_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Senior Python Engineer - TechCorp Careers</title>
</head>
<body>
    <header>
        <h1>TechCorp Solutions</h1>
        <h2>Senior Python Backend Engineer (Remote)</h2>
    </header>
    <main>
        <div class="job-description">
            <p>Buscamos un Ingeniero Backend Senior con experiencia en Python, FastAPI y bases de datos relacionales.</p>
        </div>

        <form id="application-form" action="/submit-job" method="POST">
            <div class="form-group">
                <label for="full_name">Nombre completo *</label>
                <input type="text" id="full_name" name="full_name" required>
            </div>

            <div class="form-group">
                <label for="email">Correo electrónico *</label>
                <input type="email" id="email" name="email" required>
            </div>

            <div class="form-group">
                <label for="phone">Teléfono de contacto</label>
                <input type="tel" id="phone" name="phone">
            </div>

            <div class="form-group">
                <label for="location">Ciudad / País de residencia</label>
                <input type="text" id="location" name="location">
            </div>

            <div class="form-group">
                <label for="cover_letter">Carta de presentación</label>
                <textarea id="cover_letter" name="cover_letter" rows="5"></textarea>
            </div>

            <div class="form-actions">
                <button type="submit" id="submit-button" class="btn-primary">Submit Application</button>
            </div>
        </form>
    </main>
</body>
</html>
"""


class JobPageHTTPHandler(SimpleHTTPRequestHandler):
    """Manejador HTTP simple para servir la página de empleo durante la prueba."""
    def do_GET(self):
        if self.path in ("/", "/job/senior-python", "/jobs/101"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(JOB_PAGE_HTML.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # Si algo intentara enviar datos reales al servidor, registrar el fallo de simulación
        self.send_response(400)
        self.end_headers()
        self.wfile.write(b"ERROR: POST request reached live submission endpoint!")

    def log_message(self, format, *args):
        pass  # Silenciar logs en pruebas


@pytest.fixture(scope="module")
def local_job_server():
    """Levanta un servidor local efímero en un hilo separado para simular una página de empleo."""
    server = HTTPServer(("127.0.0.1", 0), JobPageHTTPHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_browser_portal_adapter_fills_form_and_stops_before_submission(local_job_server: str):
    """
    REQ-024 / REQ-025 / Item 9:
    El BrowserPortalAdapter navega a la página de empleo, detecta los campos,
    los completa con los datos proporcionados, enmascara PII y se detiene ESTRICTAMENTE
    antes del botón de envío, retornando 'simulated_success'.
    """
    target_url = f"{local_job_server}/job/senior-python"
    tenant_id = f"test-tenant-{uuid.uuid4().hex[:8]}"

    adapter = BrowserPortalAdapter(simulation_mode=True)
    assert adapter.PORTAL_NAME == "browser_agent"

    applicant_data = {
        "name": "Walter Romero",
        "email": "walter.romero@example.com",
    }
    cover_letter = "Estimado equipo de TechCorp, postulo a la vacante de Senior Python con entusiasmo."
    cv_payload = {
        "full_name": "Walter Romero",
        "email": "walter.romero@example.com",
        "phone": "+503 7123-4567",
        "location": "San Salvador, El Salvador",
    }

    result = await adapter.submit_application(
        tenant_id=tenant_id,
        target_url=target_url,
        applicant_data=applicant_data,
        cover_letter=cover_letter,
        cv_payload=cv_payload,
        actions=[],
    )

    # 1. Comprobar que el resultado es exactamente 'simulated_success'
    assert result.success is True
    assert result.result_type == "simulated_success"
    assert result.confirmation_number == "SIMULATED_PREVIEW_ONLY"
    assert result.status_code == 200

    # 2. Comprobar evidencia inmutable generada
    evidence = result.evidence
    assert evidence["is_simulation"] is True
    assert "receipt_digest" in evidence
    assert len(evidence["receipt_digest"]) == 64

    # 3. Comprobar que los campos fueron detectados y rellenados
    filled_fields = evidence.get("form_filled_fields", [])
    assert len(filled_fields) >= 3  # full_name, email, cover_letter, etc.

    # 4. Comprobar captura de pantalla protegida con PII enmascarada
    screenshot_meta = evidence.get("screenshot")
    assert screenshot_meta is not None
    assert screenshot_meta["masked_sensitive_data"] is True
    assert os.path.exists(screenshot_meta["path"])

    # Limpiar captura
    try:
        os.remove(screenshot_meta["path"])
    except OSError:
        pass


@pytest.mark.asyncio
async def test_portal_factory_resolves_browser_adapter():
    """Comprueba que la fábrica resuelve BrowserPortalAdapter para portales web soportados."""
    adapter_browser = get_portal_adapter("browser")
    assert isinstance(adapter_browser, BrowserPortalAdapter)

    adapter_greenhouse = get_portal_adapter("greenhouse")
    assert isinstance(adapter_greenhouse, BrowserPortalAdapter)

    adapter_lever = get_portal_adapter("lever")
    assert isinstance(adapter_lever, BrowserPortalAdapter)


@pytest.mark.asyncio
async def test_portals_api_authorizations_crud(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """
    Prueba los endpoints de FastAPI para administración de autorizaciones de portales:
    GET /portals/authorizations
    POST /portals/authorizations
    DELETE /portals/authorizations/{id}
    """
    headers = auth_headers

    # 1. Otorgar autorización de búsqueda
    grant_payload = {
        "portal_name": "remotive.com",
        "action": "search_jobs",
        "authorized_by": "user",
        "notes": "Prueba de búsqueda pública",
    }
    res_grant = await client.post("/api/v1/portals/authorizations", json=grant_payload, headers=headers)
    assert res_grant.status_code == 201
    auth_data = res_grant.json()
    assert auth_data["status"] == "granted"
    auth_id = auth_data["authorization_id"]

    # 2. Listar autorizaciones
    res_list = await client.get("/api/v1/portals/authorizations", headers=headers)
    assert res_list.status_code == 200
    authorizations = res_list.json()
    matching = [a for a in authorizations if a["id"] == auth_id]
    assert len(matching) == 1
    assert matching[0]["portal_name"] == "remotive.com"
    assert matching[0]["action"] == "search_jobs"

    # 3. Revocar autorización
    res_del = await client.delete(f"/api/v1/portals/authorizations/{auth_id}", headers=headers)
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "revoked"

    # 4. Comprobar que ya no aparece como activa
    res_list_after = await client.get("/api/v1/portals/authorizations", headers=headers)
    active_ids = [a["id"] for a in res_list_after.json() if a.get("is_active")]
    assert auth_id not in active_ids


@pytest.mark.asyncio
async def test_portals_api_browser_preview_endpoint(client: AsyncClient, local_job_server: str, auth_headers: dict):
    """Prueba el endpoint /portals/browser/preview ejecutando navegación y detención en simulación."""
    headers = auth_headers
    payload = {
        "target_url": f"{local_job_server}/job/senior-python",
        "full_name": "Candidato Verificado",
        "email": "candidato@test.org",
        "phone": "+503 7000-0000",
        "cover_letter": "Carta de presentación enviada para previsualización segura.",
    }

    res = await client.post("/api/v1/portals/browser/preview", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()

    assert data["success"] is True
    assert data["result_type"] == "simulated_success"
    assert data["evidence"]["is_simulation"] is True
    assert "receipt_digest" in data["evidence"]
