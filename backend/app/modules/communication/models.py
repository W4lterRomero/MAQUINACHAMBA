"""
Modelos ORM para el perfil de comunicación y estilo (REQ-006, D10).
Permite configurar tono, reglas de redacción, cuestionario de estilo y ejemplos
aportados por el usuario, con aislamiento estricto por tenant_id.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CommunicationProfile(Base):
    """
    Perfil de comunicación y preferencias de estilo de postulación.
    Aislamiento obligatorio por tenant_id.
    """

    __tablename__ = "communication_profiles"

    profile_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("user_profiles.id"), nullable=True, index=True
    )
    tone: Mapped[str] = mapped_column(
        String(50), default="formal", nullable=False
    )  # formal, semiformal, casual
    language_preferences: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "primary_language": "es",
            "bilingual_mode": "auto",
            "prefer_spanish_for_latam": True,
        },
        nullable=False,
    )
    writing_rules: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "conciseness": "standard",  # concise, standard, detailed
            "include_portfolio": True,
            "highlight_key_metrics": True,
            "custom_instructions": [],
        },
        nullable=False,
    )
    questionnaire_answers: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "greeting_style": "default",  # default, recipient_direct, team_direct
            "focus_area": "technical_depth",  # technical_depth, problem_solving, leadership_and_collaboration, general_match
            "mention_availability": True,
            "mention_work_mode": True,
        },
        nullable=False,
    )
    example_texts: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )  # Lista de dicts: [{"id": "...", "title": "...", "content": "..."}]
    status: Mapped[str] = mapped_column(
        String(20), default="approved", nullable=False
    )  # draft, approved
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el perfil de comunicación a diccionario limpio."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "profile_id": self.profile_id,
            "tone": self.tone,
            "language_preferences": self.language_preferences or {},
            "writing_rules": self.writing_rules or {},
            "questionnaire_answers": self.questionnaire_answers or {},
            "example_texts": self.example_texts or [],
            "status": self.status,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
