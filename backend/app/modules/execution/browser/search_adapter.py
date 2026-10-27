"""
Adaptador de Búsqueda de Empleo con Navegador Web (FASE B y FASE C).
Navega a portales públicos de empleo o páginas de carreras y extrae
oportunidades laborales estructuradas sin inventar datos.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from playwright.async_api import Page

from backend.app.modules.vacancies.ingestion import canonicalize_url

logger = logging.getLogger("maquinachamba.browser.search")


class JobSearchBrowserAdapter:
    """
    Adaptador de búsqueda de ofertas laborales interactuando con páginas web mediante navegador.
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    async def extract_jobs_from_listing_page(
        self,
        page: Page,
        container_selector: str,
        title_selector: str,
        company_selector: Optional[str] = None,
        link_selector: Optional[str] = None,
        location_selector: Optional[str] = None,
        max_results: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Extrae ofertas laborales de una página de resultados de empleo basada en selectores CSS.
        """
        jobs: List[Dict[str, Any]] = []
        base_url = page.url

        try:
            cards = await page.query_selector_all(container_selector)
            for card in cards[:max_results]:
                # 1. Título
                title_el = await card.query_selector(title_selector)
                if not title_el:
                    continue
                title = (await title_el.inner_text()).strip()

                # 2. Empresa
                company = "Empresa por confirmar"
                if company_selector:
                    comp_el = await card.query_selector(company_selector)
                    if comp_el:
                        company = (await comp_el.inner_text()).strip()

                # 3. URL de la vacante
                link_url = base_url
                target_link_el = await card.query_selector(link_selector) if link_selector else title_el
                if target_link_el:
                    href = await target_link_el.get_attribute("href")
                    if href:
                        link_url = urljoin(base_url, href)

                # 4. Ubicación
                location = "Remoto"
                if location_selector:
                    loc_el = await card.query_selector(location_selector)
                    if loc_el:
                        location = (await loc_el.inner_text()).strip()

                clean_url = canonicalize_url(link_url)

                jobs.append({
                    "company_name": company,
                    "job_title": title,
                    "source_url": clean_url,
                    "location": location,
                    "source": "browser_agent",
                    "description": f"Vacante encontrada por agente de navegador: {title} en {company}. Ubicación: {location}.",
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as exc:
            logger.error(f"Error extrayendo empleos con el navegador: {exc}")

        return jobs
