"""
Adaptador de Registro de Cuentas en Portales de Empleo (FASE B y FASE D, REQ-025).
Regla inviolable: NUNCA crear cuentas externas sin autorización explícita previa por portal.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from playwright.async_api import Page

from backend.app.core.security import AuthorizationPolicy

logger = logging.getLogger("maquinachamba.browser.account")


class AccountRegistrationAdapter:
    """
    Gestiona el registro asistido/supervisado de cuentas en portales de empleo.
    Aplica compuerta de seguridad estricta para garantizar consentimiento informado.
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def verify_registration_authorization(
        self,
        portal_name: str,
        auth_record: Optional[Any] = None,
        creation_authorized: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """
        Verifica mediante la política central si el usuario autorizó crear una cuenta en este portal.
        Exige un registro persistido específico de autorización en lugar de un booleano en memoria.
        """
        allowed, reason = AuthorizationPolicy.can_create_account(
            portal_name=portal_name,
            auth_record=auth_record,
            creation_authorized=creation_authorized,
        )
        return allowed, reason

    async def fill_registration_form(
        self,
        page: Page,
        portal_name: str,
        auth_record: Optional[Any] = None,
        creation_authorized: Optional[bool] = None,
        user_profile: Optional[Dict[str, Any]] = None,
        password: str = "",
    ) -> Dict[str, Any]:
        """
        Rellena el formulario de registro sólo si la autorización previa persistida fue otorgada.
        """
        allowed, reason = self.verify_registration_authorization(
            portal_name=portal_name,
            auth_record=auth_record,
            creation_authorized=creation_authorized,
        )
        if not allowed:
            raise PermissionError(
                f"Bloqueo de seguridad: No se puede registrar cuenta en '{portal_name}'. Motivo: {reason}"
            )

        prof = user_profile or {}
        # Mapa de selectores genéricos para formularios de registro
        name = prof.get("full_name", "")
        email = prof.get("email", "")

        filled_fields = []

        # Intentar llenar nombre
        name_sel = "input[name*='name' i], input[id*='name' i], input[autocomplete='name']"
        el_name = await page.query_selector(name_sel)
        if el_name and await el_name.is_visible():
            await el_name.fill(name)
            filled_fields.append("full_name")

        # Intentar llenar email
        email_sel = "input[type='email'], input[name*='email' i], input[id*='email' i]"
        el_email = await page.query_selector(email_sel)
        if el_email and await el_email.is_visible():
            await el_email.fill(email)
            filled_fields.append("email")

        # Intentar llenar contraseña
        pass_sel = "input[type='password']"
        el_pass = await page.query_selector(pass_sel)
        if el_pass and await el_pass.is_visible():
            await el_pass.fill(password)
            filled_fields.append("password")

        return {
            "status": "form_filled",
            "portal": portal_name,
            "fields_filled": filled_fields,
            "requires_user_submit_click": True,  # Registro siempre asistido para revisión final
        }
