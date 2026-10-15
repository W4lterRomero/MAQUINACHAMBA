"""
Módulo de seguridad: autenticación, autorización y política central.
"""

import hashlib
import hmac
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
    def can_execute_candidacy(
        approval_decision: str,
        approval_content_hash: str,
        current_content_hash: str,
        approval_invalidated: bool,
        simulation_mode: bool,
    ) -> tuple[bool, str]:
        """
        Verifica si una candidatura puede ejecutarse.
        Returns: (allowed, reason)
        """
        if simulation_mode:
            return False, "Modo simulación activo. No se ejecutan acciones reales."

        if approval_decision != "approved":
            return False, f"Candidatura no aprobada. Estado: {approval_decision}"

        if approval_invalidated:
            return False, "Aprobación invalidada por cambio posterior."

        if approval_content_hash != current_content_hash:
            return False, (
                "El contenido ha cambiado desde la aprobación. "
                "Requiere nueva revisión."
            )

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
    Valida que una URL sea segura para acceder.
    Returns: (safe, reason)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL inválida."

    if parsed.scheme not in ("http", "https"):
        return False, f"Esquema no permitido: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL sin hostname."

    # Check blocked hosts
    if hostname.lower() in BLOCKED_HOSTS:
        return False, f"Host bloqueado: {hostname}"

    # Check if hostname is an IP address in blocked range
    try:
        ip = ip_address(hostname)
        for network in BLOCKED_IP_RANGES:
            if ip in network:
                return False, f"IP en rango bloqueado: {hostname}"
    except ValueError:
        # Not an IP address, that's fine
        pass

    return True, "URL válida."


# =============================================================================
# Hashing de contenido para integridad
# =============================================================================

def hash_content(*parts: str) -> str:
    """
    Genera un hash SHA-256 determinista del contenido.
    Usado para verificar integridad de aprobaciones.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
    return hasher.hexdigest()


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
