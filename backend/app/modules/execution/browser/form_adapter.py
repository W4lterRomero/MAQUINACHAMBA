"""
Adaptador de Detección y Llenado de Formularios de Postulación (FASE B y FASE E).
Mapea inteligentemente campos de formularios web (ATS como Greenhouse, Lever, Ashby, etc.)
contra datos verificados del usuario y el CV adaptado.
Principio: CERO hechos inventados (Extraer != Verificar).
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import ElementHandle, Page

logger = logging.getLogger("maquinachamba.browser.form")


class ApplicationFormAdapter:
    """
    Detecta y rellena campos comunes de postulación laboral en navegadores web.
    """

    # Selectores heurísticos comunes en plataformas ATS y portales de empleo
    FIELD_PATTERNS = {
        "first_name": ["input[name*='first_name' i]", "input[id*='first_name' i]", "input[name='first']"],
        "last_name": ["input[name*='last_name' i]", "input[id*='last_name' i]", "input[name='last']"],
        "full_name": ["input[name*='name' i]:not([name*='first']):not([name*='last'])", "input[id*='name' i]", "input[autocomplete='name']"],
        "email": ["input[type='email']", "input[name*='email' i]", "input[id*='email' i]"],
        "phone": ["input[type='tel']", "input[name*='phone' i]", "input[id*='phone' i]"],
        "location": ["input[name*='location' i]", "input[name*='city' i]", "input[id*='location' i]"],
        "linkedin": ["input[name*='linkedin' i]", "input[id*='linkedin' i]", "input[placeholder*='linkedin' i]"],
        "github": ["input[name*='github' i]", "input[id*='github' i]", "input[placeholder*='github' i]"],
        "portfolio": ["input[name*='portfolio' i]", "input[name*='website' i]", "input[id*='portfolio' i]"],
        "cover_letter": ["textarea[name*='letter' i]", "textarea[id*='letter' i]", "textarea[name*='cover' i]", "textarea[name*='comments' i]"],
        "resume_file": ["input[type='file'][name*='resume' i]", "input[type='file'][name*='cv' i]", "input[type='file']"],
    }

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    async def find_element(self, page: Page, selectors: List[str]) -> Optional[ElementHandle]:
        """Busca el primer elemento visible que coincida con la lista de selectores."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def fill_application_form(
        self,
        page: Page,
        applicant_data: Dict[str, Any],
        cover_letter: str,
        cv_file_path: Optional[str] = None,
        custom_answers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Rellena los campos detectados del formulario de postulación.
        """
        filled_fields: List[str] = []
        unmatched_fields: List[str] = []

        full_name = applicant_data.get("full_name") or applicant_data.get("name") or ""
        name_parts = full_name.split()
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        # 1. Nombre y apellidos
        el_fn = await self.find_element(page, self.FIELD_PATTERNS["first_name"])
        el_ln = await self.find_element(page, self.FIELD_PATTERNS["last_name"])
        if el_fn and el_ln:
            await el_fn.fill(first_name)
            await el_ln.fill(last_name)
            filled_fields.extend(["first_name", "last_name"])
        else:
            el_name = await self.find_element(page, self.FIELD_PATTERNS["full_name"])
            if el_name:
                await el_name.fill(full_name)
                filled_fields.append("full_name")

        # 2. Correo electrónico
        el_email = await self.find_element(page, self.FIELD_PATTERNS["email"])
        if el_email and applicant_data.get("email"):
            await el_email.fill(applicant_data["email"])
            filled_fields.append("email")

        # 3. Teléfono
        el_phone = await self.find_element(page, self.FIELD_PATTERNS["phone"])
        if el_phone and applicant_data.get("phone"):
            await el_phone.fill(applicant_data["phone"])
            filled_fields.append("phone")

        # 4. Ubicación
        el_loc = await self.find_element(page, self.FIELD_PATTERNS["location"])
        loc_val = applicant_data.get("location_city") or applicant_data.get("location")
        if el_loc and loc_val:
            await el_loc.fill(loc_val)
            filled_fields.append("location")

        # 5. LinkedIn
        el_li = await self.find_element(page, self.FIELD_PATTERNS["linkedin"])
        if el_li and applicant_data.get("linkedin_url"):
            await el_li.fill(applicant_data["linkedin_url"])
            filled_fields.append("linkedin")

        # 6. GitHub
        el_gh = await self.find_element(page, self.FIELD_PATTERNS["github"])
        if el_gh and applicant_data.get("github_url"):
            await el_gh.fill(applicant_data["github_url"])
            filled_fields.append("github")

        # 7. Carta de presentación / Mensaje
        el_letter = await self.find_element(page, self.FIELD_PATTERNS["cover_letter"])
        if el_letter and cover_letter:
            await el_letter.fill(cover_letter)
            filled_fields.append("cover_letter")

        # 8. Adjuntar archivo de CV
        if cv_file_path and Path(cv_file_path).exists():
            el_file = await self.find_element(page, self.FIELD_PATTERNS["resume_file"])
            if el_file:
                await el_file.set_input_files(str(cv_file_path))
                filled_fields.append("resume_file")

        # 9. Respuestas personalizadas confirmadas (sin inventar datos)
        if custom_answers:
            for question_key, answer_val in custom_answers.items():
                try:
                    q_input = await page.query_selector(f"input[name*='{question_key}' i], textarea[name*='{question_key}' i]")
                    if q_input and await q_input.is_visible():
                        await q_input.fill(str(answer_val))
                        filled_fields.append(f"custom_{question_key}")
                except Exception:
                    unmatched_fields.append(question_key)

        return {
            "filled_fields": filled_fields,
            "unmatched_fields": unmatched_fields,
            "success": "email" in filled_fields and ("full_name" in filled_fields or "first_name" in filled_fields),
        }
