"""
Modelos ORM para vacantes de empleo descubiertas o importadas.
Deduplicación por hash y trazabilidad de origen.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import Boolean, Date, DateTime, Numeric, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class Vacancy(Base):
    """Vacante de empleo registrada en el sistema."""

    __tablename__ = "vacancies"

    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="manual")  # manual, csv, test_portal, linkedin
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), default="Remoto")
    remote_type: Mapped[str] = mapped_column(String(20), default="remote")  # remote, onsite, hybrid, unknown
    salary_min: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    salary_max: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, default="USD")
    requirements: Mapped[dict] = mapped_column(JSON, default=list)  # lista de requerimientos
    description: Mapped[str] = mapped_column(Text, default="")
    posted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
