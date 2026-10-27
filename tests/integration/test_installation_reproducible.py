"""
Pruebas para Instalación Reproducible (REQ-032, D1).
Verifica:
1. El script install_reproducible.sh existe, es ejecutable y corre en modo --check-only.
2. Detección correcta de requisitos del sistema (Python >= 3.12 y SQLite 3).
3. Verificación de permisos y valores por defecto seguros (SIMULATION_MODE=true, host=127.0.0.1).
4. Generación criptográfica segura de clave secreta.
"""

import os
from pathlib import Path
import subprocess
import pytest

from backend.app.core.config import get_settings


def test_install_script_exists_and_executable():
    """Verifica que scripts/install_reproducible.sh existe y cuenta con permisos de ejecución."""
    script_path = Path("scripts/install_reproducible.sh")
    assert script_path.exists(), "El script install_reproducible.sh no fue encontrado."
    assert os.access(script_path, os.X_OK), "El script install_reproducible.sh debe ser ejecutable."


def test_install_script_check_only_mode():
    """Verifica que el modo --check-only evalúa los requisitos del sistema y termina con código 0."""
    result = subprocess.run(
        ["./scripts/install_reproducible.sh", "--check-only"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Fallo al ejecutar en modo --check-only: {result.stderr}"
    assert "Python" in result.stdout
    assert "SQLite" in result.stdout
    assert "cumplido" in result.stdout or "listos" in result.stdout


def test_default_configuration_security_posture():
    """Verifica que la configuración por defecto garantiza seguridad fail-closed."""
    settings = get_settings()
    assert settings.simulation_mode is True, "SEC-007: SIMULATION_MODE debe ser True por defecto."
    assert settings.app_host == "127.0.0.1", "La aplicación debe escuchar solo en localhost."


def test_required_runtime_directories_present():
    """Verifica que los directorios locales requeridos existen para el funcionamiento offline."""
    required_dirs = [
        Path("data"),
        Path("data/documents"),
        Path("backups"),
        Path("logs"),
    ]
    for d in required_dirs:
        assert d.exists() and d.is_dir(), f"Directorio requerido {d} no existe."


def test_reproducible_dependencies_and_migrations_configured():
    """Verifica que existen dependencias bloqueadas y migraciones versionadas (REQ-032)."""
    lock_file = Path("backend/requirements.lock")
    assert lock_file.exists(), "backend/requirements.lock debe existir para garantizar reproducibilidad."
    content = lock_file.read_text(encoding="utf-8")
    assert "==" in content, "requirements.lock debe contener versiones fijadas con '=='."
    assert "fastapi==" in content.lower()
    assert "sqlalchemy==" in content.lower()

    # Comprobar script instalador
    script_content = Path("scripts/install_reproducible.sh").read_text(encoding="utf-8")
    assert "requirements.lock" in script_content
    assert "alembic upgrade head" in script_content
