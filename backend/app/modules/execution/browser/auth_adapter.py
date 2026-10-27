"""
Adaptador de Autenticación y Sesiones de Navegador (FASE B y FASE D).
Almacena cookies y estados de sesión por portal con aislamiento estricto por tenant.
Permite verificar la validez de sesiones antes de intentar postulaciones.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("maquinachamba.browser.auth")


class AuthenticationAdapter:
    """
    Gestiona la persistencia segura y reutilización de estados de sesión de navegador.
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def get_sessions_dir(self) -> Path:
        """Directorio aislado de sesiones por tenant."""
        path = Path("data") / "tenants" / self.tenant_id / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_session_file(self, portal_name: str) -> Path:
        """Ruta al archivo storage_state del portal."""
        safe_name = "".join(c for c in portal_name.lower() if c.isalnum() or c in ("-", "_"))
        return self.get_sessions_dir() / f"{safe_name}_session.json"

    def has_saved_session(self, portal_name: str) -> bool:
        """Comprueba si existe un archivo de sesión guardado."""
        sf = self.get_session_file(portal_name)
        return sf.exists() and sf.stat().st_size > 10

    async def save_session_state(self, context: BrowserContext, portal_name: str) -> Path:
        """
        Exporta cookies y almacenamiento local del navegador y los guarda con permisos estrictos.
        """
        sf = self.get_session_file(portal_name)
        state = await context.storage_state()

        # Escritura atómica con permisos 0600 en POSIX
        temp_file = sf.parent / f".tmp_{sf.name}_{os.getpid()}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        if os.name == "posix":
            os.chmod(temp_file, 0o600)

        temp_file.replace(sf)
        logger.info(f"Sesión guardada exitosamente para '{portal_name}' (tenant: {self.tenant_id}).")
        return sf

    async def check_session_active(
        self,
        page: Page,
        portal_name: str,
        test_url: str,
        logged_in_selector: Optional[str] = None,
        login_url_marker: str = "login",
    ) -> bool:
        """
        Verifica si la sesión sigue activa navegando a una URL de prueba del portal.
        """
        try:
            await page.goto(test_url, wait_until="domcontentloaded", timeout=15000)
            current_url = page.url.lower()

            # Si fuimos redirigidos a la página de login, la sesión expiró
            if login_url_marker in current_url:
                logger.info(f"Sesión de '{portal_name}' expirada (redirigido a {current_url}).")
                return False

            if logged_in_selector:
                el = await page.query_selector(logged_in_selector)
                return el is not None and await el.is_visible()

            return True
        except Exception as exc:
            logger.warning(f"Error comprobando sesión en '{portal_name}': {exc}")
            return False
