"""
Conector para la API pública de empleo remoto de Remotive (REQ-007, REQ-008).
Fuente pública gratuita sin necesidad de claves de API ni registro de cuentas.
API Endpoint: https://remotive.com/api/remote-jobs
"""

from datetime import date, datetime, timezone
import html
import logging
import re
from typing import Any, Dict, List, Optional
import httpx

from backend.app.modules.vacancies.connectors.base import BaseJobSearchConnector, RawJobResult
from backend.app.modules.vacancies.models import JobSearchPreferences

logger = logging.getLogger("maquinachamba.vacancies.remotive")

REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"


def clean_html_to_text(raw_html: str) -> str:
    """Convierte HTML de descripción a texto plano limpio y legible."""
    if not raw_html:
        return ""
    # Eliminar scripts y estilos
    text = re.sub(r"<(script|style).*?>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Reemplazar saltos de línea y listas
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li.*?>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    # Eliminar etiquetas restantes
    text = re.sub(r"<.*?>", "", text)
    # Decodificar entidades HTML
    text = html.unescape(text)
    # Normalizar espacios
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_salary_string(salary_str: Optional[str]) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Intenta extraer montos mínimo, máximo y moneda de cadenas de salario como:
    '$120,000 - $150,000', '100k - 130k USD', '€60.000 - €80.000', etc.
    """
    if not salary_str or not salary_str.strip():
        return None, None, None

    currency = "USD"
    if "€" in salary_str or "eur" in salary_str.lower():
        currency = "EUR"
    elif "£" in salary_str or "gbp" in salary_str.lower():
        currency = "GBP"

    # Buscar patrones tipo 120k o 120,000
    cleaned = salary_str.replace(",", "")
    numbers = []
    # Buscar patrones con k (ej: 120k)
    k_matches = re.findall(r"(\d+(?:\.\d+)?)\s*k", cleaned, re.IGNORECASE)
    if k_matches:
        numbers = [float(n) * 1000 for n in k_matches]
    else:
        num_matches = re.findall(r"\b(\d{4,7})\b", cleaned)
        if num_matches:
            numbers = [float(n) for n in num_matches]

    if not numbers:
        return None, None, currency

    if len(numbers) == 1:
        return numbers[0], numbers[0], currency
    return min(numbers), max(numbers), currency


class RemotiveJobConnector(BaseJobSearchConnector):
    """
    Conector de lectura para Remotive Public Remote Jobs API.
    Respeta el principio de autorización previa de red y transparencia de campos.
    """
    clean_html_to_text = staticmethod(clean_html_to_text)
    parse_salary_string = staticmethod(parse_salary_string)

    @property
    def source_name(self) -> str:
        return "remotive"

    @property
    def base_domain(self) -> str:
        return "https://remotive.com"

    @property
    def data_sent_description(self) -> str:
        return (
            "Parámetros de consulta HTTP GET exclusivamente: categoría tecnológica ('software-dev'), "
            "términos de búsqueda extraídos de puestos/habilidades deseadas ('search') y límite de resultados. "
            "Cero datos personales, cero información confidencial y cero credenciales del usuario."
        )

    def _get_verifiable_sample_data(self, query: str = "") -> List[RawJobResult]:
        """Datos de muestra estructurados para pruebas y modo sin autorización de red."""
        now = datetime.now(timezone.utc)
        today = now.date()

        samples = [
            RawJobResult(
                external_id="remotive-sample-101",
                source=self.source_name,
                source_url="https://remotive.com/remote-jobs/software-dev/senior-backend-engineer-sample-101",
                company_name="OctoFlow Technologies",
                job_title="Senior Python Backend Engineer",
                location="Worldwide (Remote)",
                remote_type="remote",
                salary_min=110000.0,
                salary_max=135000.0,
                salary_currency="USD",
                requirements=["Python", "FastAPI", "SQLAlchemy", "PostgreSQL", "Docker", "Git"],
                description=(
                    "Estamos buscando un Senior Python Backend Engineer con sólida experiencia en arquitecturas "
                    "asíncronas, FastAPI y bases de datos relacionales. Responsable de diseñar APIs escalables, "
                    "procesamiento en segundo plano y automatización."
                ),
                posted_date=today,
                closing_date=None,
                fetched_at=now,
                raw_payload={"id": "remotive-sample-101", "is_synthetic_sample": True},
                unknown_fields=["closing_date"],
                is_real_search_result=False,
            ),
            RawJobResult(
                external_id="remotive-sample-102",
                source=self.source_name,
                source_url="https://remotive.com/remote-jobs/software-dev/full-stack-python-developer-sample-102",
                company_name="CloudVenture Labs",
                job_title="Full Stack Python & TypeScript Developer",
                location="Remote - LATAM / US Timezones",
                remote_type="remote",
                salary_min=95000.0,
                salary_max=120000.0,
                salary_currency="USD",
                requirements=["Python", "TypeScript", "React", "REST API", "Testing"],
                description=(
                    "Buscamos desarrollador Full Stack para trabajar con Python y TypeScript/React. "
                    "Enfoque en herramientas de productividad y sistemas distribuidos con cobertura de tests."
                ),
                posted_date=today,
                closing_date=None,
                fetched_at=now,
                raw_payload={"id": "remotive-sample-102", "is_synthetic_sample": True},
                unknown_fields=["closing_date"],
                is_real_search_result=False,
            ),
            RawJobResult(
                external_id="remotive-sample-103",
                source=self.source_name,
                source_url="https://remotive.com/remote-jobs/software-dev/qa-automation-engineer-sample-103",
                company_name="QualityCore Systems",
                job_title="QA Automation Engineer (Python / Playwright)",
                location="Worldwide (Remote)",
                remote_type="remote",
                salary_min=80000.0,
                salary_max=100000.0,
                salary_currency="USD",
                requirements=["Python", "Playwright", "pytest", "CI/CD", "Docker"],
                description=(
                    "Buscamos un Ingeniero de Automatización QA para liderar estrategias de testing E2E "
                    "e integración continua utilizando Python, pytest y Playwright."
                ),
                posted_date=today,
                closing_date=None,
                fetched_at=now,
                raw_payload={"id": "remotive-sample-103", "is_synthetic_sample": True},
                unknown_fields=["closing_date"],
                is_real_search_result=False,
            ),
        ]

        if query:
            q_lower = query.lower()
            filtered = [
                s for s in samples
                if q_lower in s.job_title.lower() or any(q_lower in r.lower() for r in s.requirements)
            ]
            return filtered if filtered else samples

        return samples

    async def search_jobs(
        self,
        preferences: JobSearchPreferences,
        limit: int = 20,
        dry_run: bool = False,
    ) -> List[RawJobResult]:
        """
        Consulta la API pública de Remotive si el usuario ha autorizado el acceso a red.
        De lo contrario, devuelve datos de muestra estructurados sin emitir tráfico de red.
        """
        # 1. Verificación obligatoria de autorización de acceso a red
        if not preferences.source_access_authorized or dry_run:
            logger.info(
                f"[Remotive] Consulta en modo dry_run/no autorizado (source_access_authorized={preferences.source_access_authorized}). "
                f"Retornando datos de muestra estructurados sin llamadas de red."
            )
            return self._get_verifiable_sample_data()

        # 2. Construir parámetros para la API pública de Remotive
        search_terms = []
        if preferences.desired_job_titles:
            search_terms.extend(preferences.desired_job_titles[:2])
        if preferences.skills:
            search_terms.extend(preferences.skills[:2])

        search_query = " ".join(search_terms).strip()
        params: Dict[str, Any] = {
            "category": "software-dev",
            "limit": min(limit, 50),
        }
        if search_query:
            params["search"] = search_query

        results: List[RawJobResult] = []
        now = datetime.now(timezone.utc)

        logger.info(f"[Remotive] Ejecutando consulta externa autorizada a {REMOTIVE_API_URL} con params: {params}")

        try:
            async with httpx.AsyncClient(
                timeout=12.0,
                headers={
                    "User-Agent": "MaquinaDeChamba-JobFinder/1.0 (Local-First; Educational/Open-Source Job Assistant)"
                },
                follow_redirects=True,
            ) as client:
                response = await client.get(REMOTIVE_API_URL, params=params)
                if response.status_code != 200:
                    logger.warning(
                        f"[Remotive] Respuesta anómala de API: HTTP {response.status_code}. "
                        f"Retornando datos de muestra seguros."
                    )
                    return self._get_verifiable_sample_data(search_query)

                data = response.json()
                raw_jobs = data.get("jobs", [])

                for item in raw_jobs[:limit]:
                    job_id = str(item.get("id", ""))
                    job_url = item.get("url", "")
                    title = item.get("title", "").strip()
                    company = item.get("company_name", "").strip()
                    raw_desc = item.get("description", "")
                    clean_desc = clean_html_to_text(raw_desc)
                    pub_date_str = item.get("publication_date")
                    tags = item.get("tags", [])
                    raw_salary = item.get("salary")
                    req_location = item.get("candidate_required_location", "Remote")

                    unknown_fields: List[str] = []

                    # Parsear fecha de publicación
                    posted_date = None
                    if pub_date_str:
                        try:
                            posted_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00")).date()
                        except Exception:
                            unknown_fields.append("posted_date")
                    else:
                        unknown_fields.append("posted_date")

                    # Remotive no provee closing_date para ofertas abiertas
                    unknown_fields.append("closing_date")

                    # Parsear salario
                    sal_min, sal_max, sal_curr = parse_salary_string(raw_salary)
                    if sal_min is None and sal_max is None:
                        unknown_fields.append("salary")

                    # Extraer requerimientos a partir de tags y descripción
                    requirements_set = {str(t).strip() for t in tags if str(t).strip()}
                    # Si no hay tags suficientes, buscar habilidades comunes en la descripción
                    if len(requirements_set) < 2:
                        common_tech = ["Python", "JavaScript", "TypeScript", "SQL", "Docker", "AWS", "FastAPI", "React", "Linux", "Git"]
                        for tech in common_tech:
                            if re.search(rf"\b{tech}\b", clean_desc, re.IGNORECASE):
                                requirements_set.add(tech)

                    res_obj = RawJobResult(
                        external_id=f"remotive-{job_id}" if job_id else None,
                        source=self.source_name,
                        source_url=job_url,
                        company_name=company or "Empresa No Especificada",
                        job_title=title or "Puesto No Especificado",
                        location=req_location,
                        remote_type="remote",
                        salary_min=sal_min,
                        salary_max=sal_max,
                        salary_currency=sal_curr or "USD",
                        requirements=sorted(list(requirements_set)),
                        description=clean_desc,
                        posted_date=posted_date,
                        closing_date=None,
                        fetched_at=now,
                        raw_payload={
                            "remotive_id": job_id,
                            "category": item.get("category"),
                            "job_type": item.get("job_type"),
                            "tags": tags,
                            "raw_salary": raw_salary,
                        },
                        unknown_fields=unknown_fields,
                        is_real_search_result=True,
                    )
                    results.append(res_obj)

        except Exception as e:
            logger.error(f"[Remotive] Error durante la consulta externa: {e}")
            raise RuntimeError(f"Error al consultar fuente externa Remotive: {e}") from e

        return results
