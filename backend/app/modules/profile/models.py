"""
Modelos ORM para el perfil profesional, fuentes y verificación humana.
Extraer no equivale a verificar (verification_status obligatorio).
"""

from datetime import date, datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class UserProfile(Base):
    """Perfil central verificado del usuario."""

    __tablename__ = "user_profiles"

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    location_city: Mapped[str] = mapped_column(String(100), nullable=False)
    location_country: Mapped[str] = mapped_column(String(100), nullable=False)
    work_countries: Mapped[dict] = mapped_column(JSON, default=list)
    availability: Mapped[str] = mapped_column(String(50), default="immediate")
    job_preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    languages: Mapped[dict] = mapped_column(JSON, default=list)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, partially_verified, verified


class ProfessionalSource(Base):
    """
    Fuente profesional importada (CV en PDF/DOCX/TXT, manual, etc.).
    Principio fundamental: Extraer no equivale a verificar (verification_status obligatorio).
    Todo dato importado inicia con verification_status='pending'.
    """

    __tablename__ = "professional_sources"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(
        String(50), default="cv_import"
    )  # cv_import_pdf, cv_import_docx, cv_import_txt, manual
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, default="")
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    extracted_data: Mapped[dict] = mapped_column(JSON, default=dict)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, partially_verified, verified, rejected
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Skill(Base):
    """Habilidades técnicas y profesionales asociadas al perfil."""

    __tablename__ = "skills"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    source_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("professional_sources.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), default="technical")
    proficiency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)
    years_experience: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    source_fragment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context_type: Mapped[str] = mapped_column(String(30), default="declared")  # declared, interest, mention
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, verified, rejected


class Experience(Base):
    """Experiencia laboral verificada."""

    __tablename__ = "experiences"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    source_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("professional_sources.id"), nullable=True, index=True
    )
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(String(20), default="day")  # day, month, year, unknown
    source_fragment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    skills_used: Mapped[dict] = mapped_column(JSON, default=list)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, verified, rejected
