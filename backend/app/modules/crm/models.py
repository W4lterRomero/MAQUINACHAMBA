"""
Modelos ORM para el CRM de seguimiento de candidaturas.
Vistas de tabla, Kanban y detalle con histórico de eventos.
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CRMEntry(Base):
    """Entrada principal de seguimiento para una candidatura."""

    __tablename__ = "crm_entries"

    candidacy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidacies.id"), nullable=False, unique=True, index=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vacancies.id"), nullable=False, index=True
    )
    current_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="prepared", index=True
    )
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # high, medium, low
    next_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_action_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CRMEvent(Base):
    """Registro inmutable de cambio de estado o evento de seguimiento."""

    __tablename__ = "crm_events"

    crm_entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("crm_entries.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # status_change, note, submission, response_received, interview
    old_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
