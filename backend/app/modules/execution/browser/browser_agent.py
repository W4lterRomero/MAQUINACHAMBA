"""
Agente Principal de Navegación Web Autónoma con Playwright (FASE B).
Orquesta sesiones de navegación, respeta políticas anti-bot y detecta
desafíos de seguridad (CAPTCHA/2FA) para solicitar intervención humana.
"""

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import random
from typing import Any, Dict, List, Optional, Tuple, Union

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from backend.app.core.config import get_settings

logger = logging.getLogger("maquinachamba.browser.agent")


class HumanInterventionRequiredError(Exception):
    """
    Lanzada cuando se detecta un CAPTCHA, verificación biométrica o 2FA.
    El agente se detiene de forma segura sin intentar evadir protecciones (Regla Anti-Bot).
    """
    def __init__(self, message: str, portal_name: str, page_url: str, screenshot_path: Optional[str] = None):
        super().__init__(message)
        self.portal_name = portal_name
        self.page_url = page_url
        self.screenshot_path = screenshot_path


class BrowserAgent:
    """
    Agente de navegador modular para automatización web ética y supervisada.
    """

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    CAPTCHA_SELECTORS = [
        "iframe[src*='recaptcha']",
        "iframe[src*='turnstile']",
        "iframe[src*='hcaptcha']",
        "#cf-challenge-running",
        ".cf-turnstile-wrapper",
        ".g-recaptcha",
        "[data-testid*='captcha']",
        "#challenge-form",
    ]

    def __init__(
        self,
        tenant_id: str,
        headless: bool = True,
        simulation_mode: Optional[bool] = None,
        slow_mo_ms: int = 50,
    ):
        settings = get_settings()
        self.tenant_id = tenant_id
        self.headless = headless
        self.simulation_mode = simulation_mode if simulation_mode is not None else settings.simulation_mode
        self.slow_mo_ms = slow_mo_ms

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.current_page: Optional[Page] = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def start(self, storage_state: Optional[Union[Path, str, Dict[str, Any]]] = None):
        """Inicia el motor Playwright y configura el contexto del navegador."""
        if self._playwright is not None:
            return

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": self.DEFAULT_USER_AGENT,
            "locale": "es-ES",
            "timezone_id": "America/El_Salvador",
        }

        if storage_state is not None:
            if isinstance(storage_state, dict):
                context_kwargs["storage_state"] = storage_state
            elif isinstance(storage_state, (Path, str)) and Path(storage_state).exists():
                context_kwargs["storage_state"] = str(storage_state)

        self._context = await self._browser.new_context(**context_kwargs)
        self.current_page = await self._context.new_page()

    async def stop(self):
        """Cierra ordenadamente páginas, contexto, navegador y Playwright."""
        try:
            if self.current_page and not self.current_page.is_closed():
                await self.current_page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning(f"Error menor al cerrar navegador: {exc}")
        finally:
            self.current_page = None
            self._context = None
            self._browser = None
            self._playwright = None

    async def navigate_to(self, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 30000):
        """
        Navega a una URL con comprobación proactiva de desafíos anti-bot.
        """
        if not self.current_page:
            raise RuntimeError("El navegador no ha sido iniciado. Llama a agent.start() primero.")

        # Retardo aleatorio de cortesía para comportamiento humano
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await self.current_page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        await self.check_for_bot_challenge()
        return self.current_page

    async def check_for_bot_challenge(self):
        """
        Inspecciona el DOM en busca de retos CAPTCHA o Cloudflare.
        Si se detecta alguno, genera captura y detiene la ejecución inmediatamente.
        """
        if not self.current_page:
            return

        for sel in self.CAPTCHA_SELECTORS:
            try:
                el = await self.current_page.query_selector(sel)
                if el and await el.is_visible():
                    screenshot_bytes = await self.current_page.screenshot()
                    from backend.app.modules.execution.browser.evidence_collector import EvidenceCollector
                    ev_dir = EvidenceCollector.get_evidence_dir(self.tenant_id)
                    s_path = ev_dir / f"captcha_challenge_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
                    s_path.write_bytes(screenshot_bytes)

                    raise HumanInterventionRequiredError(
                        message="Desafío anti-bot / CAPTCHA detectado. Requiere intervención manual del usuario.",
                        portal_name="unknown",
                        page_url=self.current_page.url,
                        screenshot_path=str(s_path),
                    )
            except HumanInterventionRequiredError:
                raise
            except Exception:
                continue

    async def human_type(self, selector: str, text: str, delay_min_ms: int = 25, delay_max_ms: int = 70):
        """
        Escribe texto con intervalos aleatorios entre teclas simulando digitación humana.
        """
        if not self.current_page:
            return
        await self.current_page.click(selector)
        for char in text:
            await self.current_page.type(selector, char, delay=random.randint(delay_min_ms, delay_max_ms))
