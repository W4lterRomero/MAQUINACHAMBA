"""
Adaptador de Autenticación y Sesiones de Navegador Cifradas (P0 - Bloqueador 5).
Almacena cookies y estados de sesión por portal con cifrado criptográfico robusto
AES-256-GCM con derivación PBKDF2 y aislamiento estricto por tenant.
Protege las sesiones del navegador en Windows y Linux sin exponer cookies en texto plano.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any, Dict, Optional, Union

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.async_api import BrowserContext, Page

from backend.app.core.config import get_settings

logger = logging.getLogger("maquinachamba.browser.auth")

VAULT_MAGIC = b"MCHS"  # Maquina Chamba Session
VAULT_VERSION = b"\x01"
SALT_LEN = 16
NONCE_LEN = 12


class AuthenticationAdapter:
    """
    Gestiona la persistencia segura y cifrada de estados de sesión de navegador.
    Asegura protección en reposo (AES-256-GCM) sin importar el sistema operativo anfitrión.
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def get_sessions_dir(self) -> Path:
        """Directorio aislado de sesiones por tenant."""
        path = Path("data") / "tenants" / self.tenant_id / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        return path

    def get_session_file(self, portal_name: str) -> Path:
        """Ruta al archivo cifrado de sesión del portal."""
        safe_name = "".join(c for c in portal_name.lower() if c.isalnum() or c in ("-", "_"))
        return self.get_sessions_dir() / f"{safe_name}_session.vault"

    def _get_legacy_session_file(self, portal_name: str) -> Path:
        """Ruta a archivos legados no cifrados (para migración transparente)."""
        safe_name = "".join(c for c in portal_name.lower() if c.isalnum() or c in ("-", "_"))
        return self.get_sessions_dir() / f"{safe_name}_session.json"

    def _derive_key(self, salt: bytes) -> bytes:
        """Deriva una clave AES-256 de 32 bytes usando PBKDF2 HMAC-SHA256."""
        settings = get_settings()
        secret_seed = settings.app_secret_key.encode("utf-8")
        salt_context = salt + self.tenant_id.encode("utf-8")
        return hashlib.pbkdf2_hmac("sha256", secret_seed, salt_context, 100_000)

    def _encrypt_state(self, state: Dict[str, Any]) -> bytes:
        """Cifra el estado de sesión en un paquete binario autenticado con AES-256-GCM."""
        plaintext = json.dumps(state).encode("utf-8")
        salt = secrets.token_bytes(SALT_LEN)
        nonce = secrets.token_bytes(NONCE_LEN)
        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=self.tenant_id.encode("utf-8"))
        return VAULT_MAGIC + VAULT_VERSION + salt + nonce + ciphertext

    def _decrypt_state(self, data: bytes) -> Dict[str, Any]:
        """Descifra y valida el paquete autenticado de sesión."""
        if len(data) < len(VAULT_MAGIC) + 1 + SALT_LEN + NONCE_LEN:
            raise ValueError("Datos de sesión corruptos o truncados.")

        if not data.startswith(VAULT_MAGIC):
            # Posible sesión en JSON plano (migración)
            return json.loads(data.decode("utf-8"))

        offset = len(VAULT_MAGIC)
        version = data[offset : offset + 1]
        offset += 1
        if version != VAULT_VERSION:
            raise ValueError(f"Versión de vault de sesión no soportada: {version.hex()}")

        salt = data[offset : offset + SALT_LEN]
        offset += SALT_LEN
        nonce = data[offset : offset + NONCE_LEN]
        offset += NONCE_LEN
        ciphertext = data[offset:]

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=self.tenant_id.encode("utf-8"))
        return json.loads(plaintext.decode("utf-8"))

    def has_saved_session(self, portal_name: str) -> bool:
        """Comprueba si existe un archivo de sesión guardado (cifrado o legado)."""
        sf = self.get_session_file(portal_name)
        if sf.exists() and sf.stat().st_size > 10:
            return True
        legacy = self._get_legacy_session_file(portal_name)
        return legacy.exists() and legacy.stat().st_size > 10

    def load_session_state(self, portal_name: str) -> Optional[Dict[str, Any]]:
        """
        Descifra y carga el estado de sesión directamente en memoria (dict).
        Evita escribir archivos temporales en texto plano.
        """
        sf = self.get_session_file(portal_name)
        if sf.exists() and sf.stat().st_size > 10:
            try:
                encrypted_bytes = sf.read_bytes()
                return self._decrypt_state(encrypted_bytes)
            except Exception as exc:
                logger.error(f"Error descifrando sesión para '{portal_name}': {exc}")
                return None

        # Comprobar si existe archivo legado
        legacy = self._get_legacy_session_file(portal_name)
        if legacy.exists() and legacy.stat().st_size > 10:
            try:
                state = json.loads(legacy.read_text(encoding="utf-8"))
                # Migrar automáticamente a cifrado
                encrypted_bytes = self._encrypt_state(state)
                sf.write_bytes(encrypted_bytes)
                if os.name == "posix":
                    os.chmod(sf, 0o600)
                legacy.unlink(missing_ok=True)
                return state
            except Exception as exc:
                logger.error(f"Error migrando sesión legada para '{portal_name}': {exc}")
                return None

        return None

    async def save_session_state(self, context: BrowserContext, portal_name: str) -> Path:
        """
        Exporta cookies y almacenamiento local del navegador y los guarda
        fuertemente cifrados con AES-256-GCM y permisos estrictos.
        """
        sf = self.get_session_file(portal_name)
        state = await context.storage_state()
        encrypted_bytes = self._encrypt_state(state)

        # Escritura atómica con permisos 0600 en POSIX
        temp_file = sf.parent / f".tmp_{sf.name}_{os.getpid()}"
        temp_file.write_bytes(encrypted_bytes)
        if os.name == "posix":
            try:
                os.chmod(temp_file, 0o600)
            except OSError:
                pass

        temp_file.replace(sf)
        if os.name == "posix":
            try:
                os.chmod(sf, 0o600)
            except OSError:
                pass

        # Eliminar archivo legado si existía
        legacy = self._get_legacy_session_file(portal_name)
        legacy.unlink(missing_ok=True)

        logger.info(f"Sesión cifrada guardada exitosamente para '{portal_name}' (tenant: {self.tenant_id}).")
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
