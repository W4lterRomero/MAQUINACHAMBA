"""
Módulo de seguridad: autenticación, autorización y política central.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Optional, Any
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
            # En modo simulación, se permiten adaptadores de simulación o asistidos (cero llamadas externas)
            if not is_simulation_adapter and adapter_mode not in ("simulation_only", "assisted_only"):
                return False, (
                    "Bloqueo de seguridad: Modo simulación activo. "
                    "El adaptador no es de simulación ni asistido y generaría efectos externos reales no autorizados."
                )
            return True, "Modo seguro verificado: el adaptador opera en modo simulado o asistido."

        return True, "Modo de ejecución habilitado."

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

    @classmethod
    def validate_portal_action_authorization(
        cls,
        auth_record: Optional[Any],
        portal_name: str,
        requested_action: str,
        simulation_mode: bool = False,
    ) -> tuple[bool, str]:
        """
        P0 - Bloqueador 4 y 6: Valida un registro de autorización persistido.
        - Requiere registro no nulo.
        - Comprueba que el portal y la acción coincidan exactamente.
        - Comprueba vigencia y no-revocación.
        - 'read_email' o 'search_jobs' NUNCA autorizan 'register_account' ni 'submit_application'.
        - Si requested_action == 'submit_application' y simulation_mode=True, rechaza.
        """
        if auth_record is None:
            return False, (
                f"No existe autorización persistida para '{requested_action}' en el portal '{portal_name}'. "
                "Un parámetro booleano suministrado por el llamador no es evidencia suficiente."
            )

        p_name = getattr(auth_record, "portal_name", "")
        if p_name.lower() != portal_name.lower():
            return False, f"La autorización pertenece al portal '{p_name}', no a '{portal_name}'."

        rec_action = getattr(auth_record, "action", "")
        if rec_action != requested_action:
            return False, (
                f"Permiso insuficiente: la autorización es para '{rec_action}', la cual "
                f"NO otorga permiso para '{requested_action}'. Los permisos están estrictamente separados."
            )

        if not getattr(auth_record, "authorized", False):
            return False, f"La autorización para '{requested_action}' en '{portal_name}' está inactiva."

        if getattr(auth_record, "revoked_at", None) is not None:
            return False, f"La autorización para '{requested_action}' en '{portal_name}' fue revocada."

        expires_at = getattr(auth_record, "expires_at", None)
        if expires_at is not None:
            now = datetime.now(timezone.utc)
            if now > expires_at:
                return False, f"La autorización para '{requested_action}' en '{portal_name}' ha expirado."

        if requested_action == "submit_application" and simulation_mode:
            return False, "Modo simulación activo. Envíos externos reales se mantienen estrictamente desactivados."

        return True, f"Acción '{requested_action}' en '{portal_name}' autorizada por registro persistido."

    @classmethod
    def can_create_account(
        cls,
        portal_name: str,
        auth_record: Optional[Any] = None,
        creation_authorized: Optional[bool] = None,
    ) -> tuple[bool, str]:
        """
        P0 - Bloqueador 4 y 6: Verifica si se puede crear una cuenta en un portal.
        Exige autorización persistida específica para 'register_account'.
        Rechaza banderas booleanas en memoria si no hay registro persistido que las respalde.
        """
        if auth_record is not None:
            return cls.validate_portal_action_authorization(
                auth_record=auth_record,
                portal_name=portal_name,
                requested_action="register_account",
            )
        # Si no se suministra registro persistido, un booleano solo es insuficiente
        if creation_authorized is True:
            return False, (
                f"Autorización rechazada para crear cuenta en {portal_name}: "
                "un booleano en memoria suministrado por el llamador no es evidencia suficiente. "
                "Se requiere registro persistido en base de datos."
            )
        return False, f"No hay autorización persistida para crear cuenta en {portal_name}."

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


def validate_url(url: str, allow_localhost: bool = False) -> tuple[bool, str]:
    """
    Validación preliminar sin red; no autoriza por sí sola una conexión.
    El transporte debe validar DNS, fijar la IP pública validada conservando
    Host/TLS y repetir la política para cada redirección.
    Si allow_localhost=True (modo desarrollo/testing controlado), se permite loopback local.
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
    is_loopback = (hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost"))
    if not (allow_localhost and is_loopback):
        if (hostname in BLOCKED_HOSTS or hostname == "localhost"
                or hostname.endswith((".localhost", ".local", ".internal"))
                or "%" in hostname):
            return False, f"Host bloqueado: {hostname}"

    # Check if hostname is an IP address in blocked range
    try:
        ip = ip_address(hostname)
        effective_ip = getattr(ip, "ipv4_mapped", None) or ip
        if allow_localhost and effective_ip.is_loopback:
            return True, "Loopback local permitido para testing."
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


# =============================================================================
# Autenticación y gestión de tokens (JWT y Bcrypt)
# =============================================================================

import bcrypt
from jose import jwt, JWTError
from backend.app.core.config import get_settings


def hash_password(plain_password: str) -> str:
    """Hashea una contraseña usando bcrypt con salt seguro."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica si una contraseña en texto plano coincide con el hash almacenado."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Genera un JWT firmado con expiración para autenticar solicitudes API."""
    settings = get_settings()
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Decodifica y valida un JWT firmado.
    Lanza JWTError si el token ha expirado, tiene firma inválida o formato corrupto.
    """
    settings = get_settings()
    return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])

