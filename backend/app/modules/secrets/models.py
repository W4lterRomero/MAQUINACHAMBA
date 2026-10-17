"""
Modelos ORM para gestión de secretos, claves BYOK y registro de costos (ADR-004, REQ-029).
Almacena configuraciones de proveedor de IA, presupuesto mensual y consumo registrado.
NUNCA almacena claves en texto plano.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Boolean,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class APIKeyConfig(Base):
    """
    Configuración de proveedor de IA y límites presupuestarios por tenant (BYOK).
    La clave secreta NUNCA se almacena aquí en texto plano: key_reference apunta
    al almacén cifrado seguro en disco (vault POSIX 0600) y key_preview almacena
    únicamente una máscara segura como 'sk-...1234'.
    """

    __tablename__ = "api_key_configs"

    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="openai"
    )  # openai, anthropic, google
    model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="gpt-4o-mini"
    )
    key_reference: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )  # e.g., vault://{tenant_id}/{provider}.vault
    key_preview: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # e.g., sk-...a1b2
    monthly_budget_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=10.0
    )
    current_month_usage_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    reserved_budget_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )  # Reserva atómica en vuelo para evitar exceder presupuesto bajo concurrencia
    max_tokens_per_request: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4096
    )
    max_concurrent_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    billing_period_month: Mapped[str] = mapped_column(
        String(7), nullable=False, default=lambda: datetime.now(timezone.utc).strftime("%Y-%m")
    )


class CostLog(Base):
    """
    Registro histórico de consumo y estimación de costos de IA.
    Registra operaciones simuladas y estimaciones locales sin costos reales.
    """

    __tablename__ = "cost_logs"

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_used: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # evaluation, cv_adaptation, cover_letter, simulate_call, etc.
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_simulation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
