"""
Interfaces base y modelos para conectores de búsqueda de vacantes de empleo (REQ-007, REQ-008).
Garantiza trazabilidad de origen, campos desconocidos explícitos y normalización rigurosa.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from backend.app.modules.vacancies.models import JobSearchPreferences


@dataclass
class RawJobResult:
    """
    Resultado de vacante normalizado extraído de una fuente externa.
    Registra datos reales y documenta campos desconocidos de forma explícita.
    """

    external_id: Optional[str]
    source: str
    source_url: str
    company_name: str
    job_title: str
    location: str = "Remoto"
    remote_type: str = "remote"  # remote, onsite, hybrid, unknown
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = "USD"
    requirements: List[str] = field(default_factory=list)
    description: str = ""
    posted_date: Optional[date] = None
    closing_date: Optional[date] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    # Lista explícita de campos que la fuente no proporciona o son desconocidos
    unknown_fields: List[str] = field(default_factory=list)
    is_real_search_result: bool = True


class BaseJobSearchConnector(ABC):
    """Interfaz abstracta para conectores de búsqueda de ofertas laborales."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Identificador de la fuente (e.g. 'remotive')."""
        pass

    @property
    @abstractmethod
    def base_domain(self) -> str:
        """Dominio base de la fuente externa (para presentación y autorización)."""
        pass

    @property
    @abstractmethod
    def data_sent_description(self) -> str:
        """Descripción transparente de los datos enviados a la fuente."""
        pass

    @abstractmethod
    async def search_jobs(
        self,
        preferences: JobSearchPreferences,
        limit: int = 20,
        dry_run: bool = False,
    ) -> List[RawJobResult]:
        """
        Ejecuta la búsqueda y normalización de ofertas.
        Si dry_run es True o source_access_authorized es False, no realiza peticiones de red
        y devuelve resultados de muestra verificables.
        """
        pass
