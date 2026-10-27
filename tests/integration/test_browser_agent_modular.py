"""
Pruebas de Integración y Unidad del Agente de Navegación Web Autónomo con Playwright (FASE B).
Verifica:
1. EvidenceCollector: capturas de pantalla, hashes SHA-256 inmutables y redacción de secretos.
2. AuthenticationAdapter: aislamiento por tenant, permisos restrictivos 0600 de sesiones y verificación de estado.
3. AccountRegistrationAdapter: compuerta estricta REQ-025 (rechazo inmediato sin autorización previa).
4. ApplicationFormAdapter: detección y llenado de campos en formularios ATS sin inventar datos.
5. SubmissionAdapter: simulación por defecto (cero clic de envío) y extracción de confirmación auténtica.
6. BrowserAgent: manejo de contexto, digitación humana y detección fail-closed de retos CAPTCHA.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from backend.app.modules.execution.browser.evidence_collector import EvidenceCollector
from backend.app.modules.execution.browser.auth_adapter import AuthenticationAdapter
from backend.app.modules.execution.browser.account_adapter import AccountRegistrationAdapter
from backend.app.modules.execution.browser.form_adapter import ApplicationFormAdapter
from backend.app.modules.execution.browser.submission_adapter import SubmissionAdapter
from backend.app.modules.execution.browser.browser_agent import BrowserAgent, HumanInterventionRequiredError
from backend.app.modules.execution.browser.search_adapter import JobSearchBrowserAdapter


@pytest.mark.asyncio
async def test_evidence_collector_screenshot_and_receipt(tmp_path):
    """Verifica que el recolector de evidencias guarde capturas y genere recibos con hashes válidos."""
    test_tenant = "tenant-test-evidence-001"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<html><body><h1>Confirmación de Postulación</h1><p>Código: CONF-9988</p></body></html>")

        meta = await EvidenceCollector.capture_page_screenshot(page, test_tenant, label="unit_test")
        assert meta["screenshot_path"] is not None
        assert Path(meta["screenshot_path"]).exists()
        assert meta["screenshot_hash"] is not None
        assert len(meta["screenshot_hash"]) == 64

        receipt = EvidenceCollector.build_submission_receipt(
            portal_name="greenhouse",
            target_url="https://boards.greenhouse.io/test/jobs/1",
            confirmation_code="CONF-9988",
            status_code=200,
            is_simulation=True,
            screenshot_meta=meta,
            extra_details={"secret_token": "sk-test12345678901234567890", "info": "OK"},
        )
        assert receipt["confirmation_code"] == "CONF-9988"
        assert receipt["receipt_digest"] is not None
        # Redacción de secretos en detalles
        assert "sk-test" not in json.dumps(receipt["details"])
        assert "REDACTED" in json.dumps(receipt["details"])

        await browser.close()


@pytest.mark.asyncio
async def test_auth_adapter_session_isolation_and_permissions(tmp_path):
    """Verifica el almacenamiento seguro y la verificación de sesiones por portal."""
    test_tenant = "tenant-test-auth-002"
    adapter = AuthenticationAdapter(tenant_id=test_tenant)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("<html><body><div>Sesión Activa</div></body></html>")

        session_path = await adapter.save_session_state(context, "linkedin")
        assert session_path.exists()
        assert adapter.has_saved_session("linkedin")

        if os.name == "posix":
            mode = os.stat(session_path).st_mode & 0o777
            assert mode == 0o600, f"Los permisos deben ser restrictivos 0600 (actual: {oct(mode)})"

        # Comprobar sesión activa
        is_active = await adapter.check_session_active(
            page=page,
            portal_name="linkedin",
            test_url="data:text/html,<html><body><div id='profile'>Hola</div></body></html>",
            logged_in_selector="#profile",
        )
        assert is_active is True

        await browser.close()


@pytest.mark.asyncio
async def test_account_registration_authorization_gate():
    """REQ-025 / Bloqueador 4: Falla si no existe autorización persistida específica para el portal."""
    from backend.app.modules.portals.models import PortalAuthorization

    adapter = AccountRegistrationAdapter(tenant_id="tenant-test-acc-003")

    # 1. Sin autorización explícita -> No permitido
    allowed, reason = adapter.verify_registration_authorization("Indeed", creation_authorized=False)
    assert allowed is False
    assert "autorización" in reason.lower()

    # 2. Con flag booleano solo sin registro persistido -> RECHAZADO (Bloqueador 4)
    allowed, reason = adapter.verify_registration_authorization("Indeed", creation_authorized=True)
    assert allowed is False
    assert "no es evidencia suficiente" in reason or "booleano" in reason

    # 3. Con registro persistido válido para 'register_account' -> Permitido
    auth_rec = PortalAuthorization(
        tenant_id="tenant-test-acc-003",
        portal_name="indeed",
        action="register_account",
        authorized=True,
    )
    allowed, reason = adapter.verify_registration_authorization("Indeed", auth_record=auth_rec)
    assert allowed is True


@pytest.mark.asyncio
async def test_application_form_adapter_filling():
    """Verifica que el adaptador de formularios rellene los campos ATS sin inventar hechos."""
    form_html = """
    <html>
      <body>
        <form id="application_form">
          <input name="first_name" type="text" />
          <input name="last_name" type="text" />
          <input name="email" type="email" />
          <input name="phone" type="tel" />
          <input name="location" type="text" />
          <input name="linkedin" type="text" />
          <input name="github" type="text" />
          <textarea name="cover_letter"></textarea>
          <button type="submit">Submit Application</button>
        </form>
      </body>
    </html>
    """
    adapter = ApplicationFormAdapter(tenant_id="tenant-test-form-004")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(form_html)

        applicant_data = {
            "full_name": "Walter Romero",
            "email": "walter@example.com",
            "phone": "+50370000000",
            "location_city": "San Salvador",
            "linkedin_url": "https://linkedin.com/in/walter-romero",
            "github_url": "https://github.com/W4lterRomero",
        }

        res = await adapter.fill_application_form(
            page=page,
            applicant_data=applicant_data,
            cover_letter="Estimado equipo, adjunto mi postulación para su consideración.",
        )
        assert res["success"] is True
        assert "first_name" in res["filled_fields"]
        assert "last_name" in res["filled_fields"]
        assert "email" in res["filled_fields"]
        assert "linkedin" in res["filled_fields"]
        assert "github" in res["filled_fields"]
        assert "cover_letter" in res["filled_fields"]

        # Verificar valores en los inputs del DOM
        fn_val = await page.input_value("input[name='first_name']")
        assert fn_val == "Walter"
        ln_val = await page.input_value("input[name='last_name']")
        assert ln_val == "Romero"
        em_val = await page.input_value("input[name='email']")
        assert em_val == "walter@example.com"

        await browser.close()


@pytest.mark.asyncio
async def test_submission_adapter_simulation_safety():
    """Verifica que en modo simulación NUNCA se pulse el botón de envío y se genere evidencia simulada."""
    adapter = SubmissionAdapter(tenant_id="tenant-test-sub-005", simulation_mode=True)

    form_html = """
    <html>
      <body>
        <form>
          <input name="email" value="walter@example.com" />
          <button type="submit" id="btnSubmit">Submit Application</button>
        </form>
      </body>
    </html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(form_html)

        result = await adapter.execute_submission(
            page=page,
            portal_name="greenhouse",
            target_url="https://boards.greenhouse.io/demo/jobs/1",
        )
        assert result["success"] is True
        assert result["is_simulation"] is True
        assert result["evidence"]["is_simulation"] is True
        assert result["evidence"]["confirmation_code"] == "SIMULATED_PREVIEW_ONLY"

        await browser.close()


@pytest.mark.asyncio
async def test_browser_agent_captcha_detection_fail_closed():
    """Verifica que ante un CAPTCHA se lance HumanInterventionRequiredError sin evadir protecciones."""
    captcha_html = """
    <html>
      <body>
        <h1>Cloudflare Security Check</h1>
        <div id="cf-challenge-running">Please wait...</div>
        <div class="cf-turnstile-wrapper"></div>
      </body>
    </html>
    """
    agent = BrowserAgent(tenant_id="tenant-test-agent-006", headless=True)
    async with agent:
        assert agent.current_page is not None
        await agent.current_page.set_content(captcha_html)

        with pytest.raises(HumanInterventionRequiredError) as exc_info:
            await agent.check_for_bot_challenge()

        err = exc_info.value
        assert "intervención manual" in str(err).lower()
        assert err.screenshot_path is not None
        assert Path(err.screenshot_path).exists()


@pytest.mark.asyncio
async def test_job_search_browser_adapter():
    """Verifica la extracción estructurada de vacantes desde un listado HTML."""
    listing_html = """
    <html>
      <body>
        <div class="job-card">
          <h2 class="job-title"><a href="/jobs/101">Senior Python Developer</a></h2>
          <span class="company-name">Acme Corp</span>
          <span class="job-location">Remoto (Worldwide)</span>
        </div>
        <div class="job-card">
          <h2 class="job-title"><a href="/jobs/102">Lead Backend Engineer</a></h2>
          <span class="company-name">Beta Tech</span>
          <span class="job-location">LATAM</span>
        </div>
      </body>
    </html>
    """
    adapter = JobSearchBrowserAdapter(tenant_id="tenant-test-search-007")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(listing_html)

        jobs = await adapter.extract_jobs_from_listing_page(
            page=page,
            container_selector=".job-card",
            title_selector=".job-title",
            company_selector=".company-name",
            location_selector=".job-location",
            link_selector=".job-title a",
        )
        assert len(jobs) == 2
        assert jobs[0]["job_title"] == "Senior Python Developer"
        assert jobs[0]["company_name"] == "Acme Corp"
        assert jobs[0]["location"] == "Remoto (Worldwide)"
        assert jobs[1]["job_title"] == "Lead Backend Engineer"
        assert jobs[1]["company_name"] == "Beta Tech"

        await browser.close()
