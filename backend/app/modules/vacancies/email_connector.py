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

    @classmethod
    async def sync_from_mailbox(
        cls,
        session: AsyncSession,
        tenant_id: str,
    ) -> Tuple[Optional[ImportBatch], List[Vacancy], str]:
        """
        Sincroniza reportes desde el buzón IMAP configurado (permisos de solo lectura).
        Si no está configurado o está en simulación, reporta el estado sin fallar.
        """
        settings = get_settings()
        if not settings.email_imap_host or not settings.email_address or not settings.email_app_password:
            return (
                None,
                [],
                "Servicio de correo no configurado (requiere EMAIL_IMAP_HOST, EMAIL_ADDRESS y EMAIL_APP_PASSWORD en .env).",
            )

        if settings.simulation_mode:
            return (
                None,
                [],
                "Modo simulación activo: no se establecen conexiones IMAP de red sin autorización explícita.",
            )

        import email
        import imaplib
        from email.header import decode_header

        imported_all: List[Vacancy] = []
        last_batch: Optional[ImportBatch] = None

        try:
            mail = imaplib.IMAP4_SSL(settings.email_imap_host)
            mail.login(settings.email_address, settings.email_app_password)
            mail.select("INBOX", readonly=True)

            status_search, messages = mail.search(None, '(UNSEEN)')
            if status_search != "OK" or not messages[0]:
                mail.logout()
                return None, [], "No se encontraron correos nuevos sin leer en la bandeja de entrada."

            mail_ids = messages[0].split()
            # Limitar a los 10 más recientes para evitar sobrecargas
            for mid in mail_ids[-10:]:
                res, data = mail.fetch(mid, "(RFC822)")
                if res != "OK":
                    continue
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Decodificar asunto y remitente
                subject_header = msg.get("Subject", "")
                decoded_parts = decode_header(subject_header)
                subject = ""
                for part, encoding in decoded_parts:
                    if isinstance(part, bytes):
                        subject += part.decode(encoding or "utf-8", errors="replace")
                    else:
                        subject += str(part)

                sender = msg.get("From", "")

                # Extraer cuerpo de texto
                body = ""
                if msg.is_multipart():
                    for subpart in msg.walk():
                        if subpart.get_content_type() == "text/plain":
                            payload = subpart.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="replace")
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")

                if body:
                    batch, vacs, _ = await cls.process_email_message(
                        session=session,
                        tenant_id=tenant_id,
                        sender=sender,
                        subject=subject,
                        body=body,
                    )
                    if batch:
                        last_batch = batch
                        imported_all.extend(vacs)

            mail.logout()
            count = len(imported_all)
            return (
                last_batch,
                imported_all,
                f"Sincronización IMAP completada: {count} vacantes procesadas desde correos no leídos.",
            )
        except Exception as exc:
            logger.error(f"Error sincronizando buzón IMAP: {exc}")
            return None, [], f"Fallo al conectar con el servidor IMAP: {exc}"

