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


class Skill(Base):
    """Habilidades técnicas y profesionales asociadas al perfil."""

    __tablename__ = "skills"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), default="technical")
    proficiency: Mapped[str] = mapped_column(String(20), default="intermediate")
    years_experience: Mapped[float] = mapped_column(Float, default=1.0)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="extracted"
    )  # extracted, verified, rejected


class Experience(Base):
    """Experiencia laboral verificada."""

    __tablename__ = "experiences"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    skills_used: Mapped[dict] = mapped_column(JSON, default=list)
    verification_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, verified, rejected
