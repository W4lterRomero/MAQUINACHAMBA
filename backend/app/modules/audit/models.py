"""
Modelos ORM para el módulo de auditoría y trazabilidad (audit).
"""

from typing import Optional
from sqlalchemy import String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class AuditLog(Base):
    """
    Registro inmutable de auditoría para operaciones críticas del sistema
    (backups, restauraciones, cambios de configuración, ejecuciones).
    Cumple con SEC-006: no almacena contraseñas, secretos ni datos personales innecesarios.
    """

    __tablename__ = "audit_logs"

    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(50), default="user", nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
