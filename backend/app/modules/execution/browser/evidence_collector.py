"""
Recolector de Evidencias de Navegación Web (FASE B).
Genera capturas inmutables, recibos estructurados y digest criptográfico SHA-256.
Cumple estrictamente con la protección de datos personales y redacción de secretos.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from backend.app.core.security import redact_secrets

logger = logging.getLogger("maquinachamba.browser.evidence")


class EvidenceCollector:
    """
    Gestiona la captura y archivo auditable de evidencias de ejecución:
    - Capturas de pantalla en formato PNG antes y después del despacho.
    - Hashes SHA-256 inmutables para garantizar que la evidencia no fue manipulada.
    - Metadatos del recibo emitido por el portal.
    """

    @classmethod
    def get_evidence_dir(cls, tenant_id: str) -> Path:
        """Retorna y asegura el directorio aislado de evidencias para el tenant."""
        base_dir = Path("data") / "tenants" / tenant_id / "evidence"
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @classmethod
    async def capture_page_screenshot(
        cls,
        page: Any,
        tenant_id: str,
        label: str = "submission_receipt",
    ) -> Dict[str, Any]:
        """
        Toma una captura de pantalla completa de la página actual y calcula su hash SHA-256.
        """
        evidence_dir = cls.get_evidence_dir(tenant_id)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{label}_{timestamp_str}.png"
        filepath = evidence_dir / filename

        try:
            screenshot_bytes = await page.screenshot(full_page=True)
            filepath.write_bytes(screenshot_bytes)
            file_hash = hashlib.sha256(screenshot_bytes).hexdigest()

            return {
                "screenshot_path": str(filepath),
                "screenshot_hash": file_hash,
                "screenshot_size_bytes": len(screenshot_bytes),
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            logger.error(f"Error al capturar pantalla en evidencia: {exc}")
            return {
                "screenshot_path": None,
                "screenshot_error": str(exc),
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            }

    @classmethod
    def build_submission_receipt(
        cls,
        portal_name: str,
        target_url: str,
        confirmation_code: Optional[str],
        status_code: int,
        is_simulation: bool,
        screenshot_meta: Optional[Dict[str, Any]] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Construye el paquete inmutable de evidencia para el registro de auditoría.
        """
        now = datetime.now(timezone.utc)
        clean_details = {}
        if extra_details:
            for k, v in extra_details.items():
                if isinstance(v, str):
                    clean_details[k] = redact_secrets(v)
                else:
                    clean_details[k] = v

        raw_digest_str = f"{portal_name}:{target_url}:{confirmation_code}:{now.isoformat()}"
        receipt_digest = hashlib.sha256(raw_digest_str.encode("utf-8")).hexdigest()

        receipt = {
            "portal": portal_name,
            "target_url": target_url,
            "confirmation_code": confirmation_code or "UNVERIFIED_OR_PENDING",
            "status_code": status_code,
            "is_simulation": is_simulation,
            "submitted_at_utc": now.isoformat(),
            "receipt_digest": receipt_digest,
            "screenshot": screenshot_meta or {},
            "details": clean_details,
        }
        return receipt
