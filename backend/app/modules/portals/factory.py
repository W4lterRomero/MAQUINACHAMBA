"""
Fábrica y resolución de adaptadores de portales (REQ-024, REQ-025).
Permite resolver adaptadores específicos (LinkedIn asistido, portales simulados, etc.).
"""

from typing import Any, Optional
from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter
from backend.app.modules.portals.adapters.linkedin_adapter import LinkedInAssistedAdapter
from backend.app.modules.portals.adapters.browser_adapter import BrowserPortalAdapter


def get_portal_adapter(portal_name: Optional[str] = None, simulation_mode: Optional[bool] = None) -> Any:
    """
    Resuelve la instancia del adaptador de portal correspondiente según el nombre.
    - LinkedIn: LinkedInAssistedAdapter (modo asistido obligatorio).
    - Browser/ATS: BrowserPortalAdapter (Playwright, Greenhouse, Lever, Ashby, Workday).
    - Otros / test: MockPortalAdapter.
    """
    clean_name = (portal_name or "mock_test_portal").strip().lower()
    if clean_name in ("linkedin", "linkedin_assisted", "linkedin_jobs"):
        return LinkedInAssistedAdapter(simulation_mode=simulation_mode)
    if clean_name in (
        "browser",
        "browser_agent",
        "playwright",
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "generic_browser",
    ):
        return BrowserPortalAdapter(simulation_mode=simulation_mode)
    return MockPortalAdapter(simulation_mode=simulation_mode)
