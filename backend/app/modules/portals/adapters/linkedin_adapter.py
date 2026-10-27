"""
Adaptador de portal asistido para LinkedIn (REQ-024, D7, ADR-005).
Reglas y directrices de seguridad:
1. LinkedIn opera SIEMPRE en modo asistido: NUNCA realiza envíos headless no autorizados
   ni automatización de navegador que vulnere términos de servicio o arriesgue la cuenta del usuario.
2. Genera un paquete asistido con checklist estructurado, respuestas de filtrado y carta preparada.
3. El resultado del despacho programado requiere intervención humana para confirmar el envío real.
4. Validación estricta anti-SSRF de todas las URLs destino.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from backend.app.core.config import get_settings
from backend.app.core.security import validate_url
from backend.app.modules.portals.adapters.mock_adapter import PortalSubmissionResult

logger = logging.getLogger("maquinachamba.portals.linkedin")


class LinkedInAssistedAdapter:
    """
    Adaptador para LinkedIn en Modo Asistido (REQ-024).
    Prepara la postulación, guía al usuario paso a paso y registra evidencia,
    delegando el clic final y la interacción con la sesión de LinkedIn al usuario humano.
    """

    PORTAL_NAME = "linkedin"
    CAPABILITIES = {
        "supports_direct_submit": False,      # Jamás envía directamente a LinkedIn
        "supports_file_attachment": True,     # Prepara CV descargable para adjuntar
        "requires_account": True,             # Cuenta de LinkedIn requerida en navegador
        "mode": "assisted_only",              # Siempre asistido
    }

    def __init__(self, simulation_mode: Optional[bool] = None):
        self.simulation_mode = (
            simulation_mode if simulation_mode is not None else get_settings().simulation_mode
        )

    async def submit_application(
        self,
        tenant_id: str,
        target_url: str,
        applicant_data: Optional[Dict[str, Any]] = None,
        cover_letter: Optional[str] = None,
        cv_payload: Optional[Dict[str, Any]] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        idempotency_key: Optional[str] = None,
        **kwargs: Any,
    ) -> PortalSubmissionResult:
        """
        Prepara el despacho asistido de la candidatura en LinkedIn.
        Valida la URL de destino contra SSRF y retorna resultado 'uncertain'
        para que el sistema pause y requiera confirmación explícita del usuario.
        """
        # 1. Validación anti-SSRF de la URL
        is_safe_url, url_reason = validate_url(target_url)
        if not is_safe_url:
            return PortalSubmissionResult(
                success=False,
                confirmation_number="",
                status_code=400,
                evidence={"error": url_reason, "target_url": target_url},
                error_message=f"URL de LinkedIn no válida o insegura: {url_reason}",
                result_type="failure",
            )

        confirmation_ref = f"LI-ASSIST-{uuid.uuid4().hex[:8].upper()}"
        checklist = [
            {
                "id": "open_url",
                "step": 1,
                "label": "Abrir publicación en LinkedIn",
                "description": "Abre el enlace directo de la vacante en una pestaña nueva con tu sesión de LinkedIn iniciada.",
                "action_url": target_url,
                "completed": False,
            },
            {
                "id": "attach_cv",
                "step": 2,
                "label": "Descargar y adjuntar CV especializado",
                "description": "Descarga el CV en formato PDF adaptado y adjúntalo en el formulario de postulación.",
                "completed": False,
            },
            {
                "id": "copy_cover_letter",
                "step": 3,
                "label": "Copiar carta de presentación / nota personalizada",
                "description": "Copia el mensaje adaptado y pégalo en la sección de nota de solicitud.",
                "completed": False,
            },
            {
                "id": "review_screeners",
                "step": 4,
                "label": "Completar preguntas de filtrado (Screening)",
                "description": "Usa las respuestas verificadas preparadas para expectativas salariales y años de experiencia.",
                "completed": False,
            },
            {
                "id": "confirm_submission",
                "step": 5,
                "label": "Confirmar postulación en Máquina de Chamba",
                "description": "Una vez enviada la solicitud en LinkedIn, pulsa 'Confirmar envío' para registrar la evidencia en tu CRM.",
                "completed": False,
            },
        ]

        evidence = {
            "mode": "assisted",
            "portal": "linkedin",
            "requires_manual_confirmation": True,
            "target_url": target_url,
            "confirmation_reference": confirmation_ref,
            "idempotency_key": idempotency_key,
            "checklist": checklist,
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        # En modo asistido el resultado es incierto/requiere intervención humana intencionalmente
        return PortalSubmissionResult(
            success=True,
            confirmation_number=confirmation_ref,
            status_code=200,
            evidence=evidence,
            error_message="Modo asistido activo: paquete preparado para postulación manual guiada.",
            result_type="uncertain",  # Marca 'result_uncertain' para pausar en 'requires_intervention'
        )
