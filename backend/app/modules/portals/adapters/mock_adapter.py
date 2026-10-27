"""
Adaptador de portal de prueba local (MockPortalAdapter).
Demuestra el circuito completo de postulación de forma segura y auditable
sin realizar peticiones externas reales.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Dict, List, Optional

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

    @classmethod
    def record_portal_call(
        cls,
        tenant_id: str,
        target_url: str,
        idempotency_key: Optional[str] = None,
        event: str = "submit_application",
    ) -> None:
        """
        Registra la llamada en un archivo compartido inter-proceso protegido por file lock.
        Cuando MAQUINACHAMBA_PORTAL_LOG_FILE está configurado, propaga cualquier error
        de escritura o permisos (fail-closed) para evitar registros falsos u ocultar fallos.
        """
        log_path_str = os.environ.get("MAQUINACHAMBA_PORTAL_LOG_FILE")
        if not log_path_str:
            return
        log_path = Path(log_path_str)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "tenant_id": tenant_id,
            "target_url": target_url,
            "idempotency_key": idempotency_key,
            "event": event,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            if HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                if HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    @classmethod
    def get_recorded_calls(cls, log_path: Path) -> List[Dict[str, Any]]:
        """
        Lee el registro inter-proceso de llamadas con file lock compartido.
        Falla explícitamente ante errores de lectura o líneas corruptas de JSON (fail-closed).
        """
        if not log_path.exists():
            return []
        calls = []
        with open(log_path, "r", encoding="utf-8") as f:
            if HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        calls.append(record)
                    except json.JSONDecodeError as err:
                        raise ValueError(
                            f"Línea corrupta en log de portal {log_path}:{line_num}: '{line}'. Error: {err}"
                        ) from err
            finally:
                if HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return calls

    @classmethod
    def get_recorded_call_count(cls, log_path: Path) -> int:
        """Retorna el conteo real inter-proceso de llamadas al portal."""
        return len(cls.get_recorded_calls(log_path))

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
        self.record_portal_call(
            tenant_id=tenant_id,
            target_url=target_url,
            idempotency_key=idempotency_key,
            event="submit_attempt",
        )
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
            result_type="simulated_success",
        )
