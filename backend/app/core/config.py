"""
Configuración central de Máquina de Chamba.
Carga valores desde .env con validación Pydantic.
"""

import os
import secrets
import stat
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

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


def resolve_app_secret_key(provided_key: Optional[str] = None) -> str:
    """
    Resuelve la clave secreta de la aplicación de forma segura:
    1. Si se provee explícitamente en el entorno o parámetros:
       - Rechaza claves por defecto inseguras ('CHANGE-ME-IN-PRODUCTION', etc.).
       - Exige longitud mínima de 32 caracteres.
    2. Si no se provee (o es None/vacía):
       - Genera y persiste una clave local protegida en 'data/.secret_key' con permisos 0600.
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

    # Generar o leer clave local protegida persistente
    secret_file = Path("data/.secret_key")
    if secret_file.is_file():
        try:
            content = secret_file.read_text(encoding="utf-8").strip()
            if len(content) >= 32 and content.lower() not in INSECURE_SECRET_KEYS:
                return content
        except Exception:
            pass

    # Generar nueva clave protegida
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = stat.S_IRUSR | stat.S_IWUSR  # 0600
        fd = os.open(str(secret_file), flags, mode)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(generated)
        try:
            os.chmod(str(secret_file), 0o600)
        except Exception:
            pass
    except Exception:
        secret_file.write_text(generated, encoding="utf-8")

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
