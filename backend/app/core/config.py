"""
Configuración central de Máquina de Chamba.
Carga valores desde .env con validación Pydantic.
"""

import os
import secrets
import stat
import time
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

INSECURE_SECRET_KEYS = {
    "change-me-in-production",
    "change-me",
    "secret",
    "password",
    "default",
    "admin",
    "12345678",
    "insecure",
    "jwt-secret",
    "changemeinproduction",
}


class InsecureConfigurationError(ValueError):
    """Lanzada cuando se detecta una configuración criptográficamente insegura para la sesión."""
    pass


def verify_secret_file_security(path: Path) -> None:
    """
    Verifica que el archivo de clave secreta cumpla con los estándares estrictos de seguridad:
    1. No sea enlace simbólico.
    2. Sea un archivo regular.
    3. Pertenezca al usuario actual (POSIX).
    4. Tenga permisos estrictos (0600 o 0400: sin permisos para grupo ni otros).
    Lanza InsecureConfigurationError si cualquier condición falla.
    """
    # 1. Rechazar enlaces simbólicos
    if os.path.islink(path):
        raise InsecureConfigurationError(
            f"Configuración insegura detectada: el archivo de clave secreta '{path}' es un enlace simbólico."
        )

    try:
        st = os.lstat(path)
    except OSError as e:
        raise InsecureConfigurationError(
            f"No se pudo acceder al archivo de clave secreta '{path}': {e}"
        ) from e

    # 2. Debe ser archivo regular
    if not stat.S_ISREG(st.st_mode):
        raise InsecureConfigurationError(
            f"Configuración insegura detectada: '{path}' no es un archivo regular."
        )

    # 3. Propietario correcto en POSIX
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise InsecureConfigurationError(
            f"Configuración insegura detectada: el archivo de clave secreta '{path}' pertenece a otro usuario (uid={st.st_uid}, actual={os.getuid()})."
        )

    # 4. Permisos estrictos en POSIX (solo propietario, nada para grupo ni otros)
    mode = stat.S_IMODE(st.st_mode)
    if (mode & 0o077) != 0:
        raise InsecureConfigurationError(
            f"Configuración insegura detectada: permisos inseguros {oct(mode)} en '{path}'. "
            f"Debe tener permisos restrictivos 0600 (solo lectura/escritura para el propietario)."
        )
    if (mode & 0o400) == 0:
        raise InsecureConfigurationError(
            f"Configuración insegura detectada: el archivo '{path}' no tiene permisos de lectura para el propietario ({oct(mode)})."
        )


def _read_secret_file_locked(path: Path, max_retries: int = 50, retry_delay: float = 0.05) -> str:
    """
    Lee la clave secreta desde el archivo coordinando con lock compartido.
    Si otro proceso está escribiendo en el instante de creación, espera con reintentos hasta que esté completa.
    """
    for attempt in range(max_retries):
        verify_secret_file_security(path)
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            raise InsecureConfigurationError(
                f"No se pudo abrir el archivo de clave secreta '{path}': {e}"
            ) from e

        try:
            if HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_SH)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if len(content) >= 32 and content.lower() not in INSECURE_SECRET_KEYS:
                return content
        except InsecureConfigurationError:
            raise
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            raise InsecureConfigurationError(
                f"Error al leer clave secreta de '{path}': {e}"
            ) from e

        time.sleep(retry_delay)

    raise InsecureConfigurationError(
        f"El archivo de clave secreta '{path}' está vacío o contiene un valor inválido tras múltiples intentos."
    )


def resolve_app_secret_key(
    provided_key: Optional[str] = None,
    secret_path: Optional[Path] = None,
) -> str:
    """
    Resuelve la clave secreta de la aplicación de forma segura:
    1. Si se provee explícitamente en el entorno o parámetros:
       - Rechaza claves por defecto inseguras ('CHANGE-ME-IN-PRODUCTION', etc.).
       - Exige longitud mínima de 32 caracteres.
    2. Si no se provee (o es None/vacía):
       - Genera y persiste una clave local protegida con permisos 0600.
       - Usa creación atómica exclusiva (O_CREAT | O_EXCL) y coordinación inter-proceso.
       - Rechaza enlaces simbólicos, permisos inseguros (ej. 0644) o propietarios ajenos.
       - Elimina cualquier fallback inseguro y falla cerrado ante errores de protección.
    """
    if provided_key is not None and str(provided_key).strip():
        stripped = str(provided_key).strip()
        normalized = stripped.lower().replace("-", "").replace("_", "")
        if (
            stripped.lower() in INSECURE_SECRET_KEYS
            or normalized in INSECURE_SECRET_KEYS
            or "changeme" in normalized
            or "insecure" in normalized
        ):
            raise InsecureConfigurationError(
                "Configuración insegura detectada: APP_SECRET_KEY no puede ser el valor "
                "público por defecto ('CHANGE-ME-IN-PRODUCTION') ni una clave débil conocida. "
                "Configure una clave secreta segura de al menos 32 caracteres o no especifique "
                "la variable para permitir la generación automática de una clave local protegida."
            )
        if len(stripped) < 32:
            raise InsecureConfigurationError(
                f"Configuración insegura detectada: APP_SECRET_KEY debe tener al menos 32 caracteres "
                f"(actual: {len(stripped)}). Genere una con: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return stripped

    target_path = Path(secret_path) if secret_path else Path(os.environ.get("APP_SECRET_PATH", "data/.secret_key"))

    # Si ya existe o es enlace simbólico, verificar seguridad estrictamente y leer
    if os.path.islink(target_path) or target_path.exists():
        verify_secret_file_security(target_path)
        return _read_secret_file_locked(target_path)

    # El archivo no existe: intentar creación atómica exclusiva con permisos 0600
    target_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)

    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        mode = stat.S_IRUSR | stat.S_IWUSR  # 0600
        fd = os.open(str(target_path), flags, mode)
    except FileExistsError:
        # Carrera de procesos: otro proceso (ej. daemon o API) creó el archivo primero.
        verify_secret_file_security(target_path)
        return _read_secret_file_locked(target_path)
    except OSError as e:
        raise InsecureConfigurationError(
            f"Fallo al crear atómicamente el archivo de clave secreta '{target_path}': {e}"
        ) from e

    # Este proceso ganó la carrera de creación: escribir y sincronizar
    try:
        if HAS_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, generated.encode("utf-8"))
        os.fsync(fd)
        if HAS_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    # Verificar que el archivo recién creado cumple estrictamente los requisitos de seguridad
    verify_secret_file_security(target_path)
    return generated


class Settings(BaseSettings):
    """Configuración de la aplicación con validación estricta."""

    # === Application ===
    app_env: str = Field(default="development", pattern=r"^(development|staging|production)$")
    app_secret_key: Optional[str] = Field(default=None)
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000, ge=1024, le=65535)
    app_timezone: str = Field(default="America/Mexico_City")

    # === Database ===
    database_url: str = Field(default="sqlite+aiosqlite:///./data/maquinachamba.db")

    # === AI Provider (BYOK) ===
    ai_provider: Optional[str] = Field(default=None, pattern=r"^(openai|anthropic|google)?$")
    openai_api_key: Optional[str] = Field(default=None)
    anthropic_api_key: Optional[str] = Field(default=None)
    google_api_key: Optional[str] = Field(default=None)

    # === AI Budget ===
    ai_monthly_budget_usd: float = Field(default=10.0, ge=0)
    ai_max_tokens_per_request: int = Field(default=4096, ge=100)
    ai_max_concurrent_requests: int = Field(default=2, ge=1, le=10)

    # === Email (optional) ===
    email_provider: Optional[str] = Field(default=None)
    email_imap_host: Optional[str] = Field(default=None)
    email_smtp_host: Optional[str] = Field(default=None)
    email_address: Optional[str] = Field(default=None)
    email_app_password: Optional[str] = Field(default=None)

    # === Execution Schedule ===
    execution_window_start: str = Field(default="09:00")
    execution_window_end: str = Field(default="18:00")
    execution_timezone: str = Field(default="America/Mexico_City")

    # === Security ===
    simulation_mode: bool = Field(default=True)
    session_timeout_minutes: int = Field(default=30, ge=5)
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=60, ge=1)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }

    @field_validator("app_secret_key", mode="before")
    @classmethod
    def secret_key_must_be_set_and_secure(cls, v: Optional[str]) -> str:
        return resolve_app_secret_key(v)


def get_settings() -> Settings:
    """Retorna la instancia de configuración. Cacheable con lru_cache si se desea."""
    return Settings()
