"""
Modelos ORM para vacantes de empleo descubiertas o importadas y preferencias persistentes de búsqueda.
Deduplicación por hash, trazabilidad de origen multifuente (ChatGPT, Gemini, Remotive, manual) y descubrimiento periódico.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class JobSearchPreferences(Base):
    """
    Preferencias persistentes de búsqueda laboral por tenant (REQ-007, REQ-030).
    Permite configurar criterios de descubrimiento, exclusiones, fuentes autorizadas
    y programación de búsquedas automáticas independientes del IDE.
    """

    __tablename__ = "job_search_preferences"

    desired_job_titles: Mapped[list] = mapped_column(JSON, default=list)  # ["Backend Developer", "Python Engineer"]
    skills: Mapped[list] = mapped_column(JSON, default=list)              # ["Python", "FastAPI", "SQLAlchemy"]
    locations: Mapped[list] = mapped_column(JSON, default=list)           # ["Remoto", "El Salvador", "LATAM", "Worldwide"]
    remote_modalities: Mapped[list] = mapped_column(JSON, default=lambda: ["remote"])  # ["remote", "hybrid", "onsite"]
    experience_level: Mapped[str] = mapped_column(String(50), default="any")  # junior, mid, senior, lead, any
    salary_min: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(3), default="USD")
    excluded_keywords: Mapped[list] = mapped_column(JSON, default=list)   # ["crypto", "ventas", "comisiones", "apuestas"]
    excluded_companies: Mapped[list] = mapped_column(JSON, default=list)  # ["Empresa X", "Consultora Y"]

    # Control de búsqueda periódica y estado operativo
    is_periodic_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(30), default="idle")  # idle, running, completed, error
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_results_count: Mapped[int] = mapped_column(Integer, default=0)
    last_new_jobs_count: Mapped[int] = mapped_column(Integer, default=0)

    # Protocolo de autorización explícita para fuentes externas de red
    source_access_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_sources: Mapped[list] = mapped_column(JSON, default=lambda: ["remotive"])


class Vacancy(Base):
    """Vacante de empleo registrada en el sistema con soporte multifuente y trazabilidad."""

    __tablename__ = "vacancies"

    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="manual")  # manual, csv, test_portal, linkedin, chatgpt, gemini, remotive, both
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), default="Remoto")
    remote_type: Mapped[str] = mapped_column(String(20), default="remote")  # remote, onsite, hybrid, unknown
    allowed_countries: Mapped[dict] = mapped_column(JSON, default=list)  # Lista de países autorizados para remoto
    salary_min: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    salary_max: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, default="USD")
    requirements: Mapped[dict] = mapped_column(JSON, default=list)  # lista de requerimientos
    technical_requirements: Mapped[dict] = mapped_column(JSON, default=list)  # habilidades técnicas específicas
    experience_level: Mapped[str] = mapped_column(String(50), default="unknown")  # junior, mid, senior, lead, unknown
    description: Mapped[str] = mapped_column(Text, default="")
    original_snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Fragmento del reporte original
    posted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    discovered_date: Mapped[Optional[date]] = mapped_column(Date, default=lambda: datetime.now(timezone.utc).date())
    closing_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verification_status: Mapped[str] = mapped_column(String(50), default="pending_verification")  # verified, pending_verification, unavailable
    unknown_fields: Mapped[dict] = mapped_column(JSON, default=list)  # Campos desconocidos o no provistos
    sources_detail: Mapped[dict] = mapped_column(JSON, default=list)  # Proveniencia multifuente: [{"source": "chatgpt", "discovered_at": "...", "snippet": "..."}]
    import_batch_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    fraud_signals: Mapped[dict] = mapped_column(JSON, default=list)
    closure_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    is_real_search_result: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_source_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class ImportBatch(Base):
    """Lote de importación de vacantes (ChatGPT, Gemini, manual, correo, etc.)."""

    __tablename__ = "import_batches"

    provider: Mapped[str] = mapped_column(String(50), default="manual")  # chatgpt, gemini, manual, email, api
    import_type: Mapped[str] = mapped_column(String(50), default="manual_paste")  # manual_paste, file_upload, email_sync, api
    status: Mapped[str] = mapped_column(String(50), default="completed")  # completed, insufficient_content, failed
    vacancies_found: Mapped[int] = mapped_column(Integer, default=0)
    vacancies_imported: Mapped[int] = mapped_column(Integer, default=0)
    vacancies_deduplicated: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_content_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
