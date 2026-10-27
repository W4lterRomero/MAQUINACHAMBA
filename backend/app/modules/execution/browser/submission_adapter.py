"""
Adaptador de Envío Supervisado y Extracción de Confirmaciones (FASE B y FASE G).
Gestiona el clic final de postulación, la extracción de códigos de confirmación
y la recolección de evidencias inmutables (captura de pantalla y hashes).
"""

import asyncio
from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from backend.app.core.config import get_settings
from backend.app.modules.execution.browser.evidence_collector import EvidenceCollector

logger = logging.getLogger("maquinachamba.browser.submission")


class SubmissionAdapter:
    """
    Controla el envío final de la postulación y comprueba la confirmación auténtica del portal.
    """

    SUBMIT_BUTTON_SELECTORS = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit Application')",
        "button:has-text('Enviar Solicitud')",
        "button:has-text('Apply')",
        "button:has-text('Postular')",
        "button:has-text('Enviar Candidatura')",
    ]

    CONFIRMATION_PATTERNS = [
        r"thank you for applying",
        r"application submitted",
        r"solicitud enviada",
        r"postulación recibida",
        r"candidatura enviada con éxito",
        r"hemos recibido tu solicitud",
        r"confirmation\s*(?:code|number|#)?\s*:?\s*([a-zA-Z0-9_-]+)",
    ]

    def __init__(self, tenant_id: str, simulation_mode: Optional[bool] = None):
        settings = get_settings()
        self.tenant_id = tenant_id
        self.simulation_mode = simulation_mode if simulation_mode is not None else settings.simulation_mode

    async def execute_submission(
        self,
        page: Page,
        portal_name: str,
        target_url: str,
    ) -> Dict[str, Any]:
        """
        Ejecuta el envío de la postulación respetando la política de simulación.
        """
        now = datetime.now(timezone.utc)

        # 1. MODO SIMULACIÓN POR DEFECTO
        if self.simulation_mode:
            logger.info(f"[SIMULACIÓN] Evitando clic real de envío en '{portal_name}' ({target_url}).")
            # Tomar captura del formulario completado para evidencia
            screenshot_meta = await EvidenceCollector.capture_page_screenshot(
                page=page,
                tenant_id=self.tenant_id,
                label=f"simulated_{portal_name}",
            )
            receipt = EvidenceCollector.build_submission_receipt(
                portal_name=portal_name,
                target_url=target_url,
                confirmation_code="SIMULATED_PREVIEW_ONLY",
                status_code=200,
                is_simulation=True,
                screenshot_meta=screenshot_meta,
                extra_details={"note": "Modo simulación activo. Ninguna petición externa fue enviada."},
            )
            return {
                "success": True,
                "is_simulation": True,
                "confirmation_number": "SIMULATED_PREVIEW_ONLY",
                "confirmation_code": "SIMULATED_PREVIEW_ONLY",
                "evidence": receipt,
                "receipt": receipt,
                "screenshot": screenshot_meta,
                "result_type": "simulated_success",
            }

        # 2. MODO ENVÍO REAL SUPERVISADO
        # Buscar el botón de envío
        submit_btn = None
        for sel in self.SUBMIT_BUTTON_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible() and await el.is_enabled():
                    submit_btn = el
                    break
            except Exception:
                continue

        if not submit_btn:
            screenshot_meta = await EvidenceCollector.capture_page_screenshot(
                page=page,
                tenant_id=self.tenant_id,
                label=f"failed_no_button_{portal_name}",
            )
            return {
                "success": False,
                "is_simulation": False,
                "confirmation_number": None,
                "error_message": "No se localizó un botón visible y habilitado de envío en el formulario.",
                "evidence": {"screenshot": screenshot_meta},
                "result_type": "failure",
            }

        try:
            # Clic supervisado y espera de respuesta
            await submit_btn.click()
            await asyncio.sleep(2.0)  # Breve espera para procesamiento

            # Verificar confirmación en la página resultante
            page_text = (await page.inner_text("body")).lower()
            is_confirmed = False
            confirmation_code = None

            for pat in self.CONFIRMATION_PATTERNS:
                m = re.search(pat, page_text, re.I)
                if m:
                    is_confirmed = True
                    if m.groups():
                        confirmation_code = m.group(1)
                    break

            screenshot_meta = await EvidenceCollector.capture_page_screenshot(
                page=page,
                tenant_id=self.tenant_id,
                label=f"live_{portal_name}",
            )

            if is_confirmed:
                receipt = EvidenceCollector.build_submission_receipt(
                    portal_name=portal_name,
                    target_url=target_url,
                    confirmation_code=confirmation_code or f"LIVE-CONF-{page.url[:20]}",
                    status_code=200,
                    is_simulation=False,
                    screenshot_meta=screenshot_meta,
                )
                return {
                    "success": True,
                    "is_simulation": False,
                    "confirmation_number": receipt["confirmation_code"],
                    "evidence": receipt,
                    "result_type": "success",
                }
            else:
                # El clic ocurrió pero la respuesta es incierta (no se reconoce confirmación ni error)
                receipt = EvidenceCollector.build_submission_receipt(
                    portal_name=portal_name,
                    target_url=target_url,
                    confirmation_code=None,
                    status_code=202,
                    is_simulation=False,
                    screenshot_meta=screenshot_meta,
                    extra_details={"uncertain": True, "note": "Postulación enviada pero sin pantalla de confirmación clara."},
                )
                return {
                    "success": False,
                    "is_simulation": False,
                    "confirmation_number": None,
                    "evidence": receipt,
                    "result_type": "uncertain",
                    "error_message": "Respuesta ambigua del portal: se requiere verificación humana antes de reintentar.",
                }

        except Exception as exc:
            logger.error(f"Error durante el envío de formulario en {portal_name}: {exc}")
            screenshot_meta = await EvidenceCollector.capture_page_screenshot(
                page=page,
                tenant_id=self.tenant_id,
                label=f"error_{portal_name}",
            )
            return {
                "success": False,
                "is_simulation": False,
                "confirmation_number": None,
                "evidence": {"screenshot": screenshot_meta, "uncertain": True},
                "result_type": "uncertain",
                "error_message": f"Excepción durante el despacho: {exc}",
            }
