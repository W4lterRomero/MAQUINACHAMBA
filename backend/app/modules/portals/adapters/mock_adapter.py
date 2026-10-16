"""
Adaptador de portal de prueba local (MockPortalAdapter).
Demuestra el circuito completo de postulación de forma segura y auditable
sin realizar peticiones externas reales.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid
from typing import Any, Dict, Optional

from backend.app.core.config import get_settings


@dataclass
class PortalSubmissionResult:
    """Resultado estructurado de la postulación en portal."""
    success: bool
    confirmation_number: str
    status_code: int
    evidence: Dict[str, Any]
    error_message: Optional[str] = None
    result_type: str = "success"  # "success", "failure", "uncertain"


class MockPortalAdapter:
    """
    Adaptador de prueba local para simulación de postulaciones.
    Cumple con el principio de simulación por defecto y generación de evidencia.
    """

    PORTAL_NAME = "mock_test_portal"
    CAPABILITIES = {
        "supports_direct_submit": True,
        "supports_file_attachment": True,
        "requires_account": False,
        "mode": "simulation_only",
    }

    def __init__(
        self,
        simulation_mode: Optional[bool] = None,
        result_override: Optional[PortalSubmissionResult] = None,
    ):
        self.simulation_mode = (
            simulation_mode if simulation_mode is not None else get_settings().simulation_mode
        )
        self.submission_count: int = 0
        self.result_override = result_override

    async def submit_application(
        self,
        tenant_id: str,
        target_url: str,
        applicant_data: Dict[str, Any],
        cover_letter: str,
        cv_payload: Dict[str, Any],
        actions: list,
        idempotency_key: Optional[str] = None,
    ) -> PortalSubmissionResult:
        """
        Ejecuta la postulación en el portal de prueba.
        Genera recibo sintético y evidencia inmutable.
        """
        self.submission_count += 1
        if self.result_override is not None:
            return self.result_override

        # Soporte para barrera de prueba controlada (demostración de crash durante despacho real)
        barrier_path = os.environ.get("MAQUINACHAMBA_DISPATCH_BARRIER_FILE")
        if barrier_path:
            bp = Path(barrier_path)
            bp.write_text(f"entered:{self.submission_count}", encoding="utf-8")
            # Pausa controlada esperando terminación o liberación
            wait_limit = 30.0
            start_wait = asyncio.get_event_loop().time()
            while bp.exists() and (asyncio.get_event_loop().time() - start_wait) < wait_limit:
                await asyncio.sleep(0.05)

        now = datetime.now(timezone.utc)
        receipt_id = f"MOCK-CONF-{uuid.uuid4().hex[:10].upper()}"

        # Evidencia auditable sin secretos
        evidence_hash = hashlib.sha256(
            f"{tenant_id}:{receipt_id}:{target_url}:{applicant_data.get('email')}".encode("utf-8")
        ).hexdigest()

        evidence = {
            "portal": self.PORTAL_NAME,
            "simulation_mode": self.simulation_mode,
            "target_url": target_url,
            "idempotency_key": idempotency_key,
            "submitted_at_utc": now.isoformat(),
            "confirmation_code": receipt_id,
            "evidence_digest": evidence_hash,
            "actions_executed": [a.get("action") for a in actions if isinstance(a, dict)],
            "applicant_email": applicant_data.get("email"),
            "applicant_name": applicant_data.get("name"),
            "simulated_response_headers": {
                "server": "MockPortal/1.0",
                "x-submission-receipt": receipt_id,
                "content-type": "application/json",
            },
        }

        return PortalSubmissionResult(
            success=True,
            confirmation_number=receipt_id,
            status_code=201,
            evidence=evidence,
            error_message=None,
        )
