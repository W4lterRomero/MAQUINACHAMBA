"""
Módulo de seguridad: autenticación, autorización y política central.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel


# =============================================================================
# Política central de autorización para acciones externas
# =============================================================================

class AuthorizationPolicy:
    """
    Política central de autorización independiente del modelo de IA.
    Todas las acciones externas deben pasar por esta política.
    """

    @staticmethod
    def validate_approval_package(
        approval_decision: str,
        approval_content_hash: str,
        current_content_hash: str,
        approval_invalidated: bool,
    ) -> tuple[bool, str]:
        """
        Verifica la validez e integridad del paquete aprobado por el usuario.
        Independiente del entorno de ejecución (simulación o real).
        """
        if approval_decision != "approved":
            return False, f"Candidatura no aprobada. Estado: {approval_decision}"

        if approval_invalidated:
            return False, "Aprobación invalidada por cambio posterior."

        if approval_content_hash != current_content_hash:
            return False, (
                "El contenido ha cambiado desde la aprobación. "
                "Requiere nueva revisión."
            )

        return True, "Paquete de candidatura íntegro y debidamente aprobado."

    @staticmethod
    def can_perform_live_effects(
        simulation_mode: bool,
        adapter_mode: Optional[str] = None,
        is_simulation_adapter: bool = False,
    ) -> tuple[bool, str]:
        """
        Determina si se permiten efectos externos reales o si deben permanecer simulados.
        Un adaptador sin capacidades de simulación queda estrictamente bloqueado si simulation_mode=True.
        """
        if simulation_mode:
            # En modo simulación, SOLO se permiten adaptadores explícitamente simulados
            if not is_simulation_adapter and adapter_mode != "simulation_only":
                return False, (
                    "Bloqueo de seguridad: Modo simulación activo. "
                    "El adaptador no es de simulación y generaría efectos externos reales no autorizados."
                )
            return True, "Modo simulación verificado: el adaptador opera en modo simulado seguro."

        return True, "Modo real habilitado."

    @classmethod
    def can_execute_candidacy(
        cls,
        approval_decision: str,
        approval_content_hash: str,
        current_content_hash: str,
        approval_invalidated: bool,
        simulation_mode: bool,
    ) -> tuple[bool, str]:
        """
        Verifica si una candidatura puede ejecutarse con efectos externos reales.
        Combina validación del paquete y verificación de modo no simulado.
        """
        pkg_valid, pkg_reason = cls.validate_approval_package(
            approval_decision=approval_decision,
            approval_content_hash=approval_content_hash,
            current_content_hash=current_content_hash,
            approval_invalidated=approval_invalidated,
        )
        if not pkg_valid:
            return False, pkg_reason

        if simulation_mode:
            return False, "Modo simulación activo. No se ejecutan acciones reales."

        return True, "Autorizada para ejecución."

    @staticmethod
    def can_create_account(
        portal_name: str,
        creation_authorized: bool,
    ) -> tuple[bool, str]:
        """
        Verifica si se puede crear una cuenta en un portal.
        La aprobación de una candidatura NO autoriza crear cuenta.
        """
        if not creation_authorized:
            return False, (
                f"No hay autorización para crear cuenta en {portal_name}. "
                "Requiere autorización específica del usuario."
            )
        return True, f"Creación de cuenta autorizada en {portal_name}."

    @staticmethod
    def can_send_follow_up(
        follow_up_status: str,
    ) -> tuple[bool, str]:
        """Verifica si un seguimiento puede enviarse."""
        if follow_up_status != "approved":
            return False, f"Seguimiento no aprobado. Estado: {follow_up_status}"
        return True, "Seguimiento aprobado para envío."


# =============================================================================
# Validación de URLs (anti-SSRF)
# =============================================================================

# Rangos de IP internos que no se deben acceder
BLOCKED_IP_RANGES = [
    ip_network("127.0.0.0/8"),       # Loopback
    ip_network("10.0.0.0/8"),        # RFC 1918
    ip_network("172.16.0.0/12"),     # RFC 1918
    ip_network("192.168.0.0/16"),    # RFC 1918
    ip_network("169.254.0.0/16"),    # Link-local
    ip_network("::1/128"),           # IPv6 loopback
    ip_network("fc00::/7"),          # IPv6 private
    ip_network("fe80::/10"),         # IPv6 link-local
]

# Cloud metadata endpoints
BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.google.com",
    "169.254.169.254",
}


def validate_url(url: str) -> tuple[bool, str]:
    """
    Validación preliminar sin red; no autoriza por sí sola una conexión.
    El transporte debe validar DNS, fijar la IP pública validada conservando
    Host/TLS y repetir la política para cada redirección.
    """
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            return False, "URL con caracteres ambiguos."
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port  # Force validation even when no connection is made.
        if port == 0 or parsed.username is not None or parsed.password is not None:
            return False, "Puerto o credenciales en URL no permitidos."
    except Exception:
        return False, "URL inválida."

    if parsed.scheme not in ("http", "https"):
        return False, f"Esquema no permitido: {parsed.scheme}"

    if not hostname:
        return False, "URL sin hostname."

    # Check blocked hosts
    hostname = hostname.lower().rstrip(".")
    if (hostname in BLOCKED_HOSTS or hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal"))
            or "%" in hostname):
        return False, f"Host bloqueado: {hostname}"

    # Check if hostname is an IP address in blocked range
    try:
        ip = ip_address(hostname)
        effective_ip = getattr(ip, "ipv4_mapped", None) or ip
        if not effective_ip.is_global or effective_ip.is_multicast:
            return False, "IP no pública."
        for network in BLOCKED_IP_RANGES:
            if ip in network:
                return False, f"IP en rango bloqueado: {hostname}"
    except ValueError:
        # Reject ambiguous numeric/legacy IP forms and single-label hosts.
        if "." not in hostname or all(
            re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part)
            for part in hostname.split(".")
        ):
            return False, "Hostname local o numérico ambiguo."

    return True, "Formato permitido; requiere validación DNS y de transporte."


# =============================================================================
# Hashing de contenido para integridad
# =============================================================================

def hash_content(*parts: str) -> str:
    """
    Genera un hash SHA-256 determinista del contenido.
    Usado para verificar integridad de aprobaciones.
    """
    # Versioned framing preserves field boundaries, order and empty fields.
    # Legacy hashes deliberately fail verification and require fresh approval.
    payload = json.dumps(
        ["approval-content-v2", list(parts)], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_content_hash(expected: str, *parts: str) -> bool:
    """Verifica que el hash del contenido coincida."""
    actual = hash_content(*parts)
    return hmac.compare_digest(expected, actual)


# =============================================================================
# Sanitización de entrada
# =============================================================================

# Patterns que podrían indicar prompt injection
SUSPICIOUS_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions",
    r"you\s+are\s+now\s+(?:a|an)\s+",
    r"system\s*:\s*",
    r"<\s*(?:script|iframe|object|embed)",
    r"javascript\s*:",
    r"data\s*:\s*text/html",
]

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in SUSPICIOUS_PATTERNS]


def detect_injection_signals(text: str) -> list[str]:
    """
    Detecta señales de posible prompt injection en texto importado.
    No bloquea, solo señala para revisión.
    Returns: lista de señales encontradas.
    """
    signals = []
    for pattern in _compiled_patterns:
        if pattern.search(text):
            signals.append(f"Patrón sospechoso detectado: {pattern.pattern}")
    return signals


# =============================================================================
# Redacción de secretos en logs
# =============================================================================

SECRET_PATTERNS = [
    (re.compile(r"(sk-[a-zA-Z0-9]{20,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(key-[a-zA-Z0-9]{20,})"), "[REDACTED_KEY]"),
    (re.compile(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"), "[REDACTED_EMAIL]"),
    (re.compile(r"(Bearer\s+[a-zA-Z0-9._-]{20,})"), "[REDACTED_TOKEN]"),
    (re.compile(r"(password[\"']?\s*[:=]\s*[\"']?[^\s\"',]+)"), "[REDACTED_PASSWORD]"),
]


def redact_secrets(text: str) -> str:
    """Redacta secretos y datos sensibles del texto para logs."""
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
