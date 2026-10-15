"""
Configuración central de Máquina de Chamba.
Carga valores desde .env con validación Pydantic.
"""

from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuración de la aplicación con validación estricta."""

    # === Application ===
    app_env: str = Field(default="development", pattern=r"^(development|staging|production)$")
    app_secret_key: str = Field(default="CHANGE-ME-IN-PRODUCTION")
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

    @field_validator("app_secret_key")
    @classmethod
    def secret_key_must_be_set(cls, v: str) -> str:
        if v == "CHANGE-ME-IN-PRODUCTION":
            import warnings
            warnings.warn(
                "APP_SECRET_KEY no está configurada. "
                "Genera una con: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
                UserWarning,
                stacklevel=2,
            )
        return v


def get_settings() -> Settings:
    """Retorna la instancia de configuración. Cacheable con lru_cache si se desea."""
    return Settings()
