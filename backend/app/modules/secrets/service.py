"""
Servicio de almacenamiento seguro de secretos (SecretsVaultService) (ADR-004, REQ-029).
Cifra secretos en reposo usando AES-256-GCM con derivación PBKDF2 y permisos estrictos POSIX 0600.
Garantiza:
1. Sin claves en texto plano en la base de datos, logs o respuestas API.
2. Claves vinculadas criptográficamente al tenant_id (associated data).
3. Permisos de archivo 0600 (lectura/escritura exclusiva del usuario del proceso).
4. Eliminación y reemplazo seguro de claves.
5. Máscara segura (preview) para presentación en UI.
"""

import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import hashlib

from backend.app.core.config import get_settings


VAULT_MAGIC = b"MCHV"  # Maquina Chamba Vault
VAULT_VERSION = b"\x01"
SALT_LEN = 16
NONCE_LEN = 12


class SecretsVaultService:
    """
    Gestiona el almacenamiento seguro y cifrado en disco de credenciales y API keys por tenant.
    """

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path("data/secrets")

    def _get_tenant_dir(self, tenant_id: str) -> Path:
        tenant_dir = self.base_dir / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(tenant_dir, 0o700)
        except OSError:
            pass
        return tenant_dir

    def _get_vault_path(self, tenant_id: str, provider: str) -> Path:
        safe_provider = "".join(c for c in provider.lower() if c.isalnum() or c in ("-", "_"))
        return self._get_tenant_dir(tenant_id) / f"{safe_provider}.vault"

    def _derive_key(self, tenant_id: str, salt: bytes) -> bytes:
        settings = get_settings()
        secret_seed = settings.app_secret_key.encode("utf-8")
        # Derivación PBKDF2 HMAC-SHA256 con 100,000 iteraciones
        return hashlib.pbkdf2_hmac("sha256", secret_seed, salt + tenant_id.encode("utf-8"), 100_000)

    @staticmethod
    def mask_key(raw_key: str) -> str:
        """
        Produce una máscara segura para visualización sin revelar el secreto.
        Ejemplo: 'sk-abcdefgh12345678' -> 'sk-...5678'
        """
        if not raw_key or len(raw_key) < 8:
            return "***"
        prefix = raw_key[:3]
        suffix = raw_key[-4:]
        return f"{prefix}...{suffix}"

    def store_key(self, tenant_id: str, provider: str, raw_key: str) -> str:
        """
        Cifra y persiste la clave en el vault del tenant con permisos 0600.
        Retorna la referencia interna del vault.
        """
        if not raw_key or not raw_key.strip():
            raise ValueError("La clave no puede estar vacía.")

        salt = secrets.token_bytes(SALT_LEN)
        nonce = secrets.token_bytes(NONCE_LEN)
        derived_key = self._derive_key(tenant_id, salt)

        aesgcm = AESGCM(derived_key)
        # Associated data vincula criptográficamente el ciphertext al tenant_id y provider
        associated_data = f"{tenant_id}:{provider}".encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, raw_key.strip().encode("utf-8"), associated_data)

        vault_path = self._get_vault_path(tenant_id, provider)
        temp_path = vault_path.with_suffix(".tmp")

        payload = VAULT_MAGIC + VAULT_VERSION + salt + nonce + ciphertext

        # Escribir con permisos atómicos 0600
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600
        fd = os.open(str(temp_path), flags, mode)
        try:
            with open(fd, "wb", closefd=True) as f:
                f.write(payload)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass

        os.replace(temp_path, vault_path)
        return f"vault://{tenant_id}/{provider}.vault"

    def get_key(self, tenant_id: str, provider: str) -> Optional[str]:
        """
        Descifra y recupera la clave desde el vault.
        Retorna None si no existe o si falla la autenticación de datos asociados.
        """
        vault_path = self._get_vault_path(tenant_id, provider)
        if not vault_path.exists():
            return None

        try:
            payload = vault_path.read_bytes()
            if len(payload) < len(VAULT_MAGIC) + 1 + SALT_LEN + NONCE_LEN + 16:
                return None
            if not payload.startswith(VAULT_MAGIC + VAULT_VERSION):
                return None

            idx = len(VAULT_MAGIC) + len(VAULT_VERSION)
            salt = payload[idx : idx + SALT_LEN]
            idx += SALT_LEN
            nonce = payload[idx : idx + NONCE_LEN]
            idx += NONCE_LEN
            ciphertext = payload[idx:]

            derived_key = self._derive_key(tenant_id, salt)
            aesgcm = AESGCM(derived_key)
            associated_data = f"{tenant_id}:{provider}".encode("utf-8")
            plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, associated_data)
            return plaintext_bytes.decode("utf-8")
        except Exception:
            return None

    def has_key(self, tenant_id: str, provider: str) -> bool:
        """Indica si existe una clave almacenada para el tenant y proveedor."""
        vault_path = self._get_vault_path(tenant_id, provider)
        return vault_path.exists() and vault_path.stat().st_size > 0

    def delete_key(self, tenant_id: str, provider: str) -> bool:
        """
        Elimina de forma segura la clave sobrescribiendo el archivo antes de removerlo.
        """
        vault_path = self._get_vault_path(tenant_id, provider)
        if not vault_path.exists():
            return False

        try:
            size = vault_path.stat().st_size
            if size > 0:
                with open(vault_path, "wb") as f:
                    f.write(secrets.token_bytes(size))
            vault_path.unlink(missing_ok=True)
            return True
        except Exception:
            vault_path.unlink(missing_ok=True)
            return True
