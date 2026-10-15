"""
Modelos ORM para el módulo de aprobaciones y candidaturas.
Implementa el ciclo obligatorio de preparación, aprobación y ejecución.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Candidacy(Base):
    """
    Candidatura preparada para una vacante específica.
    Contiene la versión congelada del CV adaptado, carta y acciones.
    """

    __tablename__ = "candidacies"

    vacancy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vacancies.id"), nullable=False, index=True
    )
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    cv_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("master_cvs.id"), nullable=False, index=True
    )
    evaluation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("evaluations.id"), nullable=True
    )
    adapted_cv_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    form_responses: Mapped[dict] = mapped_column(JSON, default=dict)
    target_portal: Mapped[str] = mapped_column(String(50), default="test_portal")
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    planned_actions: Mapped[dict] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(30), default="prepared"
    )  # prepared, pending_approval, approved, rejected, cancelled, scheduled, executing, sent_confirmed, failed
    version: Mapped[int] = mapped_column(Integer, default=1)


class Approval(Base):
    """Registro inmutable de aprobación vinculado a versiones de documentos."""

    __tablename__ = "approvals"

    candidacy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidacies.id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # approved, rejected, postponed, cancelled
    decided_by: Mapped[str] = mapped_column(String(50), nullable=False, default="user")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Hash del paquete completo: CV + carta + formulario + destino + acciones
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Versiones exactas de documentos al momento de aprobar
    document_versions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Si la aprobación fue invalidada por un cambio posterior
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ScheduledTask(Base):
    """Tarea programada para ejecución en el ciclo del día siguiente."""

    __tablename__ = "scheduled_tasks"

    approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approvals.id"), nullable=False, index=True
    )
    candidacy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidacies.id"), nullable=False, index=True
    )
    # Fecha y hora calculadas según zona horaria del usuario
    scheduled_date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    scheduled_time: Mapped[Optional[datetime]] = mapped_column(Time, nullable=True)
    execution_timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_instant_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )  # pending, executing, completed, failed, missed_window, cancelled
    # Prevención de duplicados
    idempotency_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # Locking para evitar que dos workers ejecuten la misma tarea
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionLog(Base):
    """Registro de evidencia de ejecución."""

    __tablename__ = "execution_logs"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scheduled_tasks.id"), nullable=False, index=True
    )
    candidacy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidacies.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    result: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # success, failure, uncertain, timeout
    evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
