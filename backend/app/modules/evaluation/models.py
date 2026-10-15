"""
Modelos ORM para la evaluación de vacantes.
Compatibilidad explicada con reglas deterministas antes de IA.
"""

from typing import Optional
from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class Evaluation(Base):
    """Evaluación de compatibilidad entre un perfil y una vacante."""

    __tablename__ = "evaluations"

    vacancy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vacancies.id"), nullable=False, index=True
    )
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=False, index=True
    )
    cv_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("master_cvs.id"), nullable=False, index=True
    )
    compatibility_score: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0 a 1.0
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    matched_skills: Mapped[dict] = mapped_column(JSON, default=list)
    missing_skills: Mapped[dict] = mapped_column(JSON, default=list)
    recommendation: Mapped[str] = mapped_column(
        String(20), default="good_match"
    )  # strong_match, good_match, partial, weak
    rules_applied: Mapped[dict] = mapped_column(JSON, default=list)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
