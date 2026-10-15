"""
Pruebas de configuración.
"""

import os
import pytest


def test_simulation_mode_default_true():
    """SEC-007: Simulación activada por defecto."""
    os.environ.pop("SIMULATION_MODE", None)
    # Need to reimport to get fresh settings
    from backend.app.core.config import Settings
    settings = Settings()
    assert settings.simulation_mode is True


def test_settings_load_from_env():
    """Configuración carga desde variables de entorno."""
    os.environ["APP_PORT"] = "9000"
    os.environ["APP_TIMEZONE"] = "America/Bogota"
    from backend.app.core.config import Settings
    settings = Settings()
    assert settings.app_port == 9000
    assert settings.app_timezone == "America/Bogota"
    # Cleanup
    os.environ.pop("APP_PORT", None)
    os.environ.pop("APP_TIMEZONE", None)


def test_localhost_binding_default():
    """La app se vincula a localhost por defecto."""
    from backend.app.core.config import Settings
    settings = Settings()
    assert settings.app_host == "127.0.0.1"


def test_insecure_default_secret_key_rejected():
    """Dictamen Work 0473d01: el valor público por defecto 'CHANGE-ME-IN-PRODUCTION' no puede firmar sesiones."""
    from pydantic import ValidationError
    from backend.app.core.config import Settings, InsecureConfigurationError
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
    from pydantic import ValidationError
    from backend.app.core.config import Settings, InsecureConfigurationError
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
    from pydantic import ValidationError
    from backend.app.core.config import Settings, InsecureConfigurationError
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
    Dictamen Work 0473d01: si no se especifica APP_SECRET_KEY, genera y persiste
    una clave local protegida con permisos restrictivos (0600).
    """
    import stat
    from backend.app.core.config import Settings, resolve_app_secret_key
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)

    fake_data_dir = tmp_path / "data"
    fake_secret_file = fake_data_dir / ".secret_key"
    monkeypatch.setattr("backend.app.core.config.Path", lambda p: fake_secret_file if ".secret_key" in str(p) else tmp_path / p)

    key1 = resolve_app_secret_key(None)
    assert len(key1) >= 32
    assert fake_secret_file.exists()

    # Comprobar permisos 0600 en Linux/Unix
    file_stat = fake_secret_file.stat()
    mode = stat.S_IMODE(file_stat.st_mode)
    assert mode == 0o600

    # Segunda invocación sin env var lee la misma clave persistida
    key2 = resolve_app_secret_key(None)
    assert key2 == key1


def test_insecure_configuration_blocks_application_startup(monkeypatch):
    """Verifica que el arranque de FastAPI falla de inmediato ante configuración insegura de APP_SECRET_KEY."""
    from pydantic import ValidationError
    from backend.app.core.config import InsecureConfigurationError
    from backend.app.main import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
    with pytest.raises((InsecureConfigurationError, ValidationError)):
        create_app()

