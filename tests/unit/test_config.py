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
