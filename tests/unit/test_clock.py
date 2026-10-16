"""
Pruebas unitarias de la abstracción de reloj FileBackedClock y defensas fail-closed.
Verifica:
- Fallo explícito ante archivo ausente (FileNotFoundError).
- Fallo explícito ante archivo vacío o con solo espacios (ValueError).
- Fallo explícito ante contenido inválido no parseable (ValueError).
- Lectura correcta de tiempo UTC válido.
- Escritura atómica con reemplazo seguro.
- Métodos de avance temporal.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from backend.app.core.clock import FileBackedClock


def test_clock_missing_file_raises_filenotfound(tmp_path: Path):
    """Verifica que si el archivo de reloj no existe, now_utc() falla explícitamente sin saltos silenciosos."""
    missing_file = tmp_path / "nonexistent_clock.txt"
    clock = FileBackedClock(missing_file)
    with pytest.raises(FileNotFoundError, match="Clock file does not exist"):
        clock.now_utc()


def test_clock_empty_file_raises_valueerror(tmp_path: Path):
    """Verifica que un archivo vacío produce ValueError explícito en lugar de volver a la hora real."""
    empty_file = tmp_path / "empty_clock.txt"
    empty_file.write_text("   \n", encoding="utf-8")
    clock = FileBackedClock(empty_file)
    with pytest.raises(ValueError, match="Clock file is empty"):
        clock.now_utc()


def test_clock_invalid_content_raises_valueerror(tmp_path: Path):
    """Verifica que contenido corrupto produce ValueError explícito."""
    corrupt_file = tmp_path / "corrupt_clock.txt"
    corrupt_file.write_text("NOT_A_VALID_ISO_TIMESTAMP", encoding="utf-8")
    clock = FileBackedClock(corrupt_file)
    with pytest.raises(ValueError, match="Invalid timestamp format"):
        clock.now_utc()


def test_clock_valid_utc_read(tmp_path: Path):
    """Verifica la lectura correcta de un timestamp ISO válido."""
    clock_file = tmp_path / "valid_clock.txt"
    expected_dt = datetime(2026, 10, 16, 15, 30, 0, tzinfo=timezone.utc)
    clock_file.write_text(expected_dt.isoformat(), encoding="utf-8")

    clock = FileBackedClock(clock_file)
    read_dt = clock.now_utc()
    assert read_dt == expected_dt
    assert read_dt.tzinfo == timezone.utc


def test_clock_fallback_initial_creates_file(tmp_path: Path):
    """Verifica que fallback_initial crea el archivo si no existe."""
    clock_file = tmp_path / "init_clock.txt"
    assert not clock_file.exists()
    init_dt = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)
    clock = FileBackedClock(clock_file, fallback_initial=init_dt)
    assert clock_file.exists()
    assert clock.now_utc() == init_dt


def test_clock_atomic_set_time_and_advance(tmp_path: Path):
    """Verifica la escritura atómica y los métodos de avance temporal."""
    clock_file = tmp_path / "atomic_clock.txt"
    init_dt = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    clock = FileBackedClock(clock_file, fallback_initial=init_dt)

    # set_time
    new_dt = datetime(2026, 10, 15, 14, 0, 0, tzinfo=timezone.utc)
    clock.set_time(new_dt)
    assert clock.now_utc() == new_dt

    # advance_seconds
    clock.advance_seconds(30)
    assert clock.now_utc() == new_dt + timedelta(seconds=30)

    # advance_hours
    clock.advance_hours(2)
    assert clock.now_utc() == new_dt + timedelta(seconds=30, hours=2)

    # advance_days
    clock.advance_days(1)
    assert clock.now_utc() == new_dt + timedelta(seconds=30, hours=2, days=1)
