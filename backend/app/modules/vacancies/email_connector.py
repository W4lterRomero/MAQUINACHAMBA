"""
Conector seguro de lectura de correo para reportes de ChatGPT y Gemini (TAREA 2.1 B).
Utiliza configuración segura de credenciales con permisos mínimos de solo lectura.
Detecta correos etiquetados y falla de forma segura ante notificaciones sin contenido.
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.modules.vacancies.ingestion import JobIngestionService, InsufficientContentError
from backend.app.modules.vacancies.models import ImportBatch, Vacancy

logger = logging.getLogger("maquinachamba.email_connector")


class EmailJobSearchConnector:
    """
    Conector de lectura de correo seguro para reportes automatizados de búsqueda de empleo.
    Reconoce remitentes y asuntos de ChatGPT y Gemini.
    """

    CHATGPT_KEYWORDS = ["chatgpt", "openai job search", "chatgpt daily job search"]
    GEMINI_KEYWORDS = ["gemini", "google job search", "gemini daily job search"]

    @classmethod
    def identify_provider_from_message(cls, sender: str, subject: str) -> Optional[str]:
        """Identifica el proveedor a partir del remitente o asunto del correo."""
        text = f"{sender} {subject}".lower()
        if any(kw in text for kw in cls.CHATGPT_KEYWORDS):
            return "chatgpt"
        if any(kw in text for kw in cls.GEMINI_KEYWORDS):
            return "gemini"
        return None

    @classmethod
    async def process_email_message(
        cls,
        session: AsyncSession,
        tenant_id: str,
        sender: str,
        subject: str,
        body: str,
    ) -> Tuple[Optional[ImportBatch], List[Vacancy], str]:
        """
        Procesa un correo recibido:
        1. Comprueba si es un reporte reconocido de ChatGPT o Gemini.
        2. Verifica que contenga datos de ofertas y no sea una simple notificación de 'resultados listos'.
        3. Importa y normaliza las vacantes encontradas.
        """
        provider = cls.identify_provider_from_message(sender, subject)
        if not provider:
            return None, [], "El correo no fue reconocido como un reporte de ChatGPT ni Gemini."

        try:
            batch, vacancies = await JobIngestionService.process_and_import(
                session=session,
                tenant_id=tenant_id,
                provider=provider,
                raw_content=body,
                import_type="email_sync",
            )
            if batch.status == "insufficient_content":
                return batch, [], f"Correo marcado como contenido insuficiente: {batch.notes}"
            return batch, vacancies, f"Se importaron {batch.vacancies_imported} vacantes desde correo ({provider.upper()})."
        except InsufficientContentError as ice:
            return None, [], f"Contenido insuficiente en correo: {ice}"
        except Exception as exc:
            logger.error(f"Error procesando correo de vacantes: {exc}")
            return None, [], f"Error procesando correo: {exc}"
