"""
Adaptador de Automatización de Navegador (BrowserPortalAdapter) para ATS y Portales Web.
Integra BrowserAgent, ApplicationFormAdapter, SubmissionAdapter y EvidenceCollector.
Respeta estrictamente el modo simulación, la política de autorización por portal y acción,
y la captura protegida con ofuscación de datos personales en el DOM.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backend.app.core.config import get_settings
from backend.app.core.security import AuthorizationPolicy, validate_url
from backend.app.modules.portals.adapters.mock_adapter import PortalSubmissionResult
from backend.app.modules.portals.service import PortalAuthorizationService
from backend.app.modules.execution.browser.browser_agent import BrowserAgent
from backend.app.modules.execution.browser.form_adapter import ApplicationFormAdapter
from backend.app.modules.execution.browser.submission_adapter import SubmissionAdapter
from backend.app.modules.execution.browser.evidence_collector import EvidenceCollector

logger = logging.getLogger("maquinachamba.portals.browser_adapter")


class BrowserPortalAdapter:
    """
    Adaptador para portales basados en formularios web (Greenhouse, Lever, Ashby, portales de empresa).
    Orquesta la navegación segura, el llenado determinista de formularios y la detención antes del envío.
    """

    PORTAL_NAME = "browser_agent"
    CAPABILITIES = {
        "supports_direct_submit": True,
        "supports_file_attachment": True,
        "requires_account": False,
        "mode": "browser_automation",
    }

    def __init__(self, simulation_mode: Optional[bool] = None):
        settings = get_settings()
        self.simulation_mode = simulation_mode if simulation_mode is not None else settings.simulation_mode

    async def submit_application(
        self,
        tenant_id: str,
        target_url: str,
        applicant_data: Dict[str, Any],
        cover_letter: str,
        cv_payload: Dict[str, Any],
        actions: list,
        idempotency_key: Optional[str] = None,
        db_session: Optional[Any] = None,
    ) -> PortalSubmissionResult:
        """
        Ejecuta el flujo de automatización en el navegador.
        En modo simulación (por defecto): navega, llena campos, toma captura con PII enmascarada
        y SE DETIENE estrictamente antes del envío, devolviendo 'simulated_success'.
        En modo real: exige autorización persistida previa para 'submit_application' en el portal.
        """
        # 1. Validación de seguridad de URL (anti-SSRF)
        parsed = urlparse(target_url)
        portal_domain = (parsed.netloc or "").lower()
        is_local_dev = self.simulation_mode and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        is_safe, reason = validate_url(target_url, allow_localhost=is_local_dev)
        if not is_safe:
            return PortalSubmissionResult(
                success=False,
                confirmation_number="",
                status_code=400,
                evidence={"error": reason, "target_url": target_url},
                error_message=f"URL inválida o no permitida: {reason}",
                result_type="failure",
            )

        # 3. Comprobar autorización si no estamos en simulación
        if not self.simulation_mode:
            if db_session:
                is_authorized = await PortalAuthorizationService.verify_authorization(
                    session=db_session,
                    tenant_id=tenant_id,
                    portal_name=portal_domain,
                    action="submit_application",
                )
                if not is_authorized:
                    return PortalSubmissionResult(
                        success=False,
                        confirmation_number="",
                        status_code=403,
                        evidence={"error": "Falta autorización persistida", "portal": portal_domain},
                        error_message=f"Postulación real no autorizada para el portal '{portal_domain}'.",
                        result_type="failure",
                    )
            else:
                return PortalSubmissionResult(
                    success=False,
                    confirmation_number="",
                    status_code=403,
                    evidence={"error": "Falta sesión para verificar autorización"},
                    error_message="No se puede ejecutar postulación real sin verificación de sesión.",
                    result_type="failure",
                )

        # 4. Iniciar agente de navegador
        agent = BrowserAgent(tenant_id=tenant_id, headless=True)
        try:
            await agent.start()
            page = await agent.navigate_to(target_url)
            if not page:
                page = agent.current_page

            # 5. Llenar formulario
            form_adapter = ApplicationFormAdapter(tenant_id=tenant_id)
            cv_p = cv_payload or {}
            form_fill_data = {
                "full_name": cv_p.get("full_name") or applicant_data.get("name") or applicant_data.get("full_name", ""),
                "first_name": cv_p.get("first_name", ""),
                "last_name": cv_p.get("last_name", ""),
                "email": applicant_data.get("email") or cv_p.get("email", ""),
                "phone": cv_p.get("phone", ""),
                "location": cv_p.get("location", ""),
            }
            cv_path = cv_p.get("pdf_path")

            fill_results = await form_adapter.fill_application_form(
                page=page,
                applicant_data=form_fill_data,
                cover_letter=cover_letter,
                cv_file_path=cv_path,
            )

            # 6. Ejecutar etapa de envío (con detención garantizada en simulación)
            submission_adapter = SubmissionAdapter(tenant_id=tenant_id, simulation_mode=self.simulation_mode)
            sub_res = await submission_adapter.execute_submission(
                page=page,
                portal_name=portal_domain,
                target_url=target_url,
            )

            is_sim = self.simulation_mode or sub_res.get("result_type") == "simulated_success"
            receipt = sub_res.get("receipt") or sub_res.get("evidence") or {}
            receipt["form_filled_fields"] = fill_results.get("filled_fields", [])
            receipt["is_simulation"] = is_sim

            return PortalSubmissionResult(
                success=sub_res.get("success", False),
                confirmation_number=sub_res.get("confirmation_number") or sub_res.get("confirmation_code") or "SIMULATED_PREVIEW_ONLY",
                status_code=200 if sub_res.get("success") else 500,
                evidence=receipt,
                error_message=sub_res.get("error_message"),
                result_type="simulated_success" if is_sim else "success",
            )
        except Exception as e:
            logger.exception("Error durante la ejecución de BrowserPortalAdapter: %s", e)
            return PortalSubmissionResult(
                success=False,
                confirmation_number="",
                status_code=500,
                evidence={"error": str(e), "target_url": target_url},
                error_message=str(e),
                result_type="failure",
            )
        finally:
            await agent.stop()
