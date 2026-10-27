"""
Pruebas de configuración y seguridad estricta de APP_SECRET_KEY.
"""

import os
import stat
from pathlib import Path
import pytest
from pydantic import ValidationError

from backend.app.core.config import (
    Settings,
    InsecureConfigurationError,
    resolve_app_secret_key,
    verify_secret_file_security,
)


def _worker_resolve_secret(secret_path_str: str, queue):
    """Función de nivel superior para probar concurrencia de procesos independientes sin imprimir claves."""
    try:
        from pathlib import Path
        from backend.app.core.config import resolve_app_secret_key
        key = resolve_app_secret_key(None, secret_path=Path(secret_path_str))
        queue.put(("SUCCESS", key))
    except Exception as e:
        queue.put(("ERROR", str(e)))


def test_simulation_mode_default_true():
    """SEC-007: Simulación activada por defecto."""
    os.environ.pop("SIMULATION_MODE", None)
    settings = Settings()
    assert settings.simulation_mode is True


def test_settings_load_from_env():
    """Configuración carga desde variables de entorno."""
    os.environ["APP_PORT"] = "9000"
    os.environ["APP_TIMEZONE"] = "America/Bogota"
    settings = Settings()
    assert settings.app_port == 9000
    assert settings.app_timezone == "America/Bogota"
    os.environ.pop("APP_PORT", None)
    os.environ.pop("APP_TIMEZONE", None)


def test_localhost_binding_default():
    """La app se vincula a localhost por defecto."""
    settings = Settings()
    assert settings.app_host == "127.0.0.1"


def test_insecure_default_secret_key_rejected():
    """Dictamen Work 0473d01: el valor público por defecto 'CHANGE-ME-IN-PRODUCTION' no puede firmar sesiones."""
    old_val = os.environ.get("APP_SECRET_KEY")
    try:
        os.environ["APP_SECRET_KEY"] = "CHANGE-ME-IN-PRODUCTION"
        with pytest.raises((InsecureConfigurationError, ValidationError)) as exc_info:
            Settings()
        assert "CHANGE-ME-IN-PRODUCTION" in str(exc_info.value)
    finally:
        if old_val is not None:
            os.environ["APP_SECRET_KEY"] = old_val
        else:
            os.environ.pop("APP_SECRET_KEY", None)


def test_too_short_secret_key_rejected():
    """Dictamen Work 0473d01: claves con longitud insuficiente (< 32 caracteres) son rechazadas."""
    old_val = os.environ.get("APP_SECRET_KEY")
    try:
        os.environ["APP_SECRET_KEY"] = "clave-corta-insegura"
        with pytest.raises((InsecureConfigurationError, ValidationError)) as exc_info:
            Settings()
        assert "al menos 32 caracteres" in str(exc_info.value)
    finally:
        if old_val is not None:
            os.environ["APP_SECRET_KEY"] = old_val
        else:
            os.environ.pop("APP_SECRET_KEY", None)


@pytest.mark.parametrize("weak_key", [
    "secret",
    "password",
    "default",
    "admin",
    "insecure",
    "change-me",
])
def test_known_weak_secret_keys_rejected(weak_key: str):
    """Rechazo explícito de claves débiles y marcadores de posición conocidos."""
    old_val = os.environ.get("APP_SECRET_KEY")
    try:
        os.environ["APP_SECRET_KEY"] = weak_key
        with pytest.raises((InsecureConfigurationError, ValidationError)):
            Settings()
    finally:
        if old_val is not None:
            os.environ["APP_SECRET_KEY"] = old_val
        else:
            os.environ.pop("APP_SECRET_KEY", None)


def test_auto_generated_local_secret_key_persisted(tmp_path, monkeypatch):
    """
    Dictamen Work: si no se especifica APP_SECRET_KEY, genera y persiste
    una clave local protegida con permisos restrictivos (0600).
    """
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    fake_secret_file = tmp_path / "data" / ".secret_key"

    key1 = resolve_app_secret_key(None, secret_path=fake_secret_file)
    assert len(key1) >= 32
    assert fake_secret_file.exists()

    # Comprobar permisos 0600 en Linux/Unix
    if os.name == "posix":
        file_stat = fake_secret_file.stat()
        assert stat.S_IMODE(file_stat.st_mode) == 0o600

    # Segunda invocación lee la misma clave persistida sin regenerar
    key2 = resolve_app_secret_key(None, secret_path=fake_secret_file)
    assert key2 == key1


def test_insecure_permissions_0644_rejected(tmp_path):
    """
    Dictamen Work sobre a1d47ed: rechazar archivos preexistentes con permisos inseguros (ej. 0644).
    Falla cerrado inmediatamente sin aceptar el archivo.
    """
    if os.name != "posix":
        pytest.skip("POSIX file permission bits not supported on Windows")
    fake_secret = tmp_path / "insecure_0644.key"
    fake_secret.write_text("dummy-secret-key-with-sufficient-length-here", encoding="utf-8")
    fake_secret.chmod(0o644)

    with pytest.raises(InsecureConfigurationError) as exc_info:
        resolve_app_secret_key(None, secret_path=fake_secret)

    assert "permisos inseguros" in str(exc_info.value).lower()


def test_symlink_secret_file_rejected(tmp_path):
    """
    Dictamen Work sobre a1d47ed: rechazar enlaces simbólicos que apunten a una clave secreta.
    Falla cerrado inmediatamente.
    """
    if os.name != "posix":
        pytest.skip("Symlink creation requires elevated privileges on Windows")
    real_file = tmp_path / "real.key"
    real_file.write_text("dummy-secret-key-with-sufficient-length-here", encoding="utf-8")
    real_file.chmod(0o600)

    symlink_file = tmp_path / "symlink.key"
    symlink_file.symlink_to(real_file)

    with pytest.raises(InsecureConfigurationError) as exc_info:
        resolve_app_secret_key(None, secret_path=symlink_file)

    assert "enlace simbólico" in str(exc_info.value).lower()


def test_concurrent_process_startup_atomic_and_shared(tmp_path):
    """
    Dictamen Work sobre a1d47ed: dos procesos arrancando simultáneamente obtienen la misma clave
    sin truncarla, con creación atómica exclusiva y coordinación de locks.
    Nunca se imprimen valores de clave.
    """
    import multiprocessing as mp

    secret_file = tmp_path / ".secret_key"
    assert not secret_file.exists()

    ctx = mp.get_context("spawn")
    q1 = ctx.Queue()
    q2 = ctx.Queue()

    p1 = ctx.Process(target=_worker_resolve_secret, args=(str(secret_file), q1))
    p2 = ctx.Process(target=_worker_resolve_secret, args=(str(secret_file), q2))

    p1.start()
    p2.start()

    p1.join(timeout=10)
    p2.join(timeout=10)

    assert p1.exitcode == 0
    assert p2.exitcode == 0

    status1, key1 = q1.get(timeout=2)
    status2, key2 = q2.get(timeout=2)

    assert status1 == "SUCCESS"
    assert status2 == "SUCCESS"

    # Ambos procesos obtuvieron la misma clave sin sobrescritura destructiva ni truncamiento
    assert key1 == key2
    assert len(key1) >= 32

    # Comprobar permisos del archivo resultante (0600) en POSIX
    if os.name == "posix":
        file_stat = secret_file.stat()
        assert stat.S_IMODE(file_stat.st_mode) == 0o600

    # Comprobar que el archivo contiene la clave exacta sin truncar
    file_content = secret_file.read_text(encoding="utf-8").strip()
    assert file_content == key1


def test_insecure_configuration_blocks_application_startup(monkeypatch):
    """Verifica que el arranque de FastAPI falla de inmediato ante configuración insegura de APP_SECRET_KEY."""
    from backend.app.main import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
    with pytest.raises((InsecureConfigurationError, ValidationError)):
        create_app()
