"""
Modelos ORM para el módulo de usuarios y configuración de tenants.
Aislamiento por tenant desde la base de datos.
"""

from datetime import time
from typing import Optional
from sqlalchemy import Boolean, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class Tenant(Base):
    """Entidad principal de tenant para aislamiento de datos."""

    __tablename__ = "tenants"

    # En el propio registro de Tenant, tenant_id es opcional
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, default=None
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="America/Mexico_City")
    simulation_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    execution_window_start: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0))
    execution_window_end: Mapped[time] = mapped_column(Time, nullable=False, default=time(18, 0))
