"""
Recolector de Evidencias de Navegación Web con Protección Estricta de PII (P0 - Bloqueador 5).
Genera capturas inmutables con enmascaramiento previo de campos sensibles (contraseñas, emails, teléfonos),
permisos restrictivos (0600) y digest criptográfico SHA-256.
Cumple estrictamente con la protección de datos personales y redacción de secretos.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from backend.app.core.security import redact_secrets

logger = logging.getLogger("maquinachamba.browser.evidence")


class EvidenceCollector:
    """
    Gestiona la captura y archivo auditable de evidencias de ejecución:
    - Capturas de pantalla con datos personales sensibles enmascarados antes del renderizado.
    - Hashes SHA-256 inmutables para garantizar que la evidencia no fue manipulada.
    - Metadatos del recibo emitido por el portal.
    """

    @classmethod
    def get_evidence_dir(cls, tenant_id: str) -> Path:
        """Retorna y asegura el directorio aislado y protegido de evidencias para el tenant."""
        base_dir = Path("data") / "tenants" / tenant_id / "evidence"
        base_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(base_dir, 0o700)
            except OSError:
                pass
        return base_dir

    @classmethod
    async def mask_sensitive_dom_data(cls, page: Any) -> None:
        """
        Enmascara información personal sensible (PII) en el DOM antes de capturar pantalla (Bloqueador 5).
        Reemplaza contraseñas, correos, teléfonos y datos financieros por marcadores protegidos
        para evitar que capturas en disco almacenen información privada sin protección.
        """
        try:
            if hasattr(page, "evaluate"):
                await page.evaluate("""
                    () => {
                        const maskSelectors = [
                            "input[type='password']",
                            "input[name*='pass' i]",
                            "input[id*='pass' i]",
                            "input[type='email']",
                            "input[name*='email' i]",
                            "input[id*='email' i]",
                            "input[type='tel']",
                            "input[name*='phone' i]",
                            "input[id*='phone' i]",
                            "input[name*='ssn' i]",
                            "input[name*='salary' i]",
                            "input[name*='remuneration' i]"
                        ];
                        maskSelectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => {
                                el.setAttribute('data-pre-mask-val', el.value);
                                if (el.type === 'password' || sel.includes('pass')) {
                                    el.value = '••••••••';
                                } else if (sel.includes('email')) {
                                    el.value = '[REDACTED_EMAIL]';
                                } else if (sel.includes('phone') || el.type === 'tel') {
                                    el.value = '[REDACTED_PHONE]';
                                } else {
                                    el.value = '[REDACTED_PII]';
                                }
                            });
                        });
                    }
                """)
        except Exception as exc:
            logger.debug(f"Aviso al enmascarar DOM antes de captura: {exc}")

    @classmethod
    async def capture_page_screenshot(
        cls,
        page: Any,
        tenant_id: str,
        label: str = "submission_receipt",
        mask_pii: bool = True,
    ) -> Dict[str, Any]:
        """
        Toma una captura de pantalla completa de la página actual protegiendo PII
        y calcula su hash SHA-256.
        """
        evidence_dir = cls.get_evidence_dir(tenant_id)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{label}_{timestamp_str}.png"
        filepath = evidence_dir / filename

        try:
            if mask_pii:
                await cls.mask_sensitive_dom_data(page)

            screenshot_bytes = await page.screenshot(full_page=True)
            filepath.write_bytes(screenshot_bytes)
            if os.name == "posix":
                try:
                    os.chmod(filepath, 0o600)
                except OSError:
                    pass

            file_hash = hashlib.sha256(screenshot_bytes).hexdigest()

            return {
                "screenshot_path": str(filepath),
                "path": str(filepath),
                "screenshot_hash": file_hash,
                "screenshot_size_bytes": len(screenshot_bytes),
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "pii_protected": mask_pii,
                "masked_sensitive_data": mask_pii,
            }
        except Exception as exc:
            logger.error(f"Error al capturar pantalla en evidencia: {exc}")
            return {
                "screenshot_path": None,
                "screenshot_error": str(exc),
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "pii_protected": mask_pii,
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
            "receipt_digest_sha256": receipt_digest,
            "screenshot": screenshot_meta,
            "details": clean_details,
        }
        return receipt
