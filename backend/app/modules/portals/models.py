"""
Modelos ORM para el módulo de portales y autorizaciones persistentes (REQ-025, D15).
Cumple con la separación estricta de privilegios:
- search_jobs: Búsqueda y lectura de ofertas
- read_email: Lectura de bandeja para sincronización de alertas
- register_account: Creación de cuentas en portales externos
- submit_application: Despacho final de postulaciones
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PortalAuthorization(Base):
    """
    Autorización persistida, específica por portal y por acción.
    Evita que llamadas con flags booleanos en memoria autoricen operaciones críticas.
    Garantiza trazabilidad y revocabilidad atómica.
    """

    __tablename__ = "portal_authorizations"

    portal_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Acciones permitidas: "search_jobs", "read_email", "register_account", "submit_application"
    authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorized_by: Mapped[str] = mapped_column(String(50), default="user", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """Comprueba si la autorización sigue vigente y no ha sido revocada."""
        if not self.authorized or self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            current = now or datetime.now(timezone.utc)
            if current > self.expires_at:
                return False
        return True
