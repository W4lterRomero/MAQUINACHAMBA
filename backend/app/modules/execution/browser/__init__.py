"""
Módulo de Navegación Web Autónoma con Playwright (FASE B).
Componentes modulares para navegación, búsqueda, autenticación,
llenado de formularios, despacho supervisado y recolección de evidencias.
"""

from backend.app.modules.execution.browser.browser_agent import BrowserAgent
from backend.app.modules.execution.browser.evidence_collector import EvidenceCollector
from backend.app.modules.execution.browser.auth_adapter import AuthenticationAdapter
from backend.app.modules.execution.browser.account_adapter import AccountRegistrationAdapter
from backend.app.modules.execution.browser.form_adapter import ApplicationFormAdapter
from backend.app.modules.execution.browser.submission_adapter import SubmissionAdapter
from backend.app.modules.execution.browser.search_adapter import JobSearchBrowserAdapter

__all__ = [
    "BrowserAgent",
    "EvidenceCollector",
    "AuthenticationAdapter",
    "AccountRegistrationAdapter",
    "ApplicationFormAdapter",
    "SubmissionAdapter",
    "JobSearchBrowserAdapter",
]
