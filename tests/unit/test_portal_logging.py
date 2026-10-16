"""
Pruebas unitarias de instrumentación del log de portal inter-proceso (MockPortalAdapter).
Verifica defensas fail-closed:
- Fallo explícito ante error de escritura o permisos.
- Fallo explícito ante línea corrupta de JSON en el log.
- Retorno de lista vacía cuando el archivo aún no existe.
- Conteo y lectura exacta de llamadas válidas.
"""

import json
import os
from pathlib import Path
import pytest

from backend.app.modules.portals.adapters.mock_adapter import MockPortalAdapter


def test_portal_logging_valid_roundtrip(tmp_path: Path, monkeypatch):
    """Verifica que record_portal_call y get_recorded_calls registran y leen entradas estructuradas."""
    log_file = tmp_path / "portal_log.jsonl"
    monkeypatch.setenv("MAQUINACHAMBA_PORTAL_LOG_FILE", str(log_file))

    MockPortalAdapter.record_portal_call(
        tenant_id="tenant-123",
        target_url="https://example.com/job1",
        idempotency_key="idemp-1",
        event="submit_attempt",
    )
    MockPortalAdapter.record_portal_call(
        tenant_id="tenant-123",
        target_url="https://example.com/job2",
        idempotency_key="idemp-2",
        event="submit_attempt",
    )

    calls = MockPortalAdapter.get_recorded_calls(log_file)
    assert len(calls) == 2
    assert calls[0]["target_url"] == "https://example.com/job1"
    assert calls[0]["idempotency_key"] == "idemp-1"
    assert calls[1]["target_url"] == "https://example.com/job2"
    assert MockPortalAdapter.get_recorded_call_count(log_file) == 2


def test_portal_logging_missing_file_returns_empty(tmp_path: Path):
    """Verifica que un archivo ausente retorna lista vacía sin error."""
    missing_file = tmp_path / "nonexistent_log.jsonl"
    assert MockPortalAdapter.get_recorded_calls(missing_file) == []
    assert MockPortalAdapter.get_recorded_call_count(missing_file) == 0


def test_portal_logging_corrupt_line_raises_valueerror(tmp_path: Path):
    """Verifica que una línea corrupta de JSON produce ValueError explícito sin ocultar llamadas."""
    log_file = tmp_path / "corrupt_portal_log.jsonl"
    # Escribir una entrada válida seguida de una corrupta
    valid_record = {"timestamp_utc": "2026-10-15T12:00:00", "pid": 1234, "event": "submit"}
    log_file.write_text(json.dumps(valid_record) + "\n{CORRUPT_JSON_DATA_LINE\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Línea corrupta en log de portal"):
        MockPortalAdapter.get_recorded_calls(log_file)


def test_portal_logging_write_failure_propagates_exception(tmp_path: Path, monkeypatch):
    """Verifica que un fallo de escritura de log se propaga explícitamente (fail-closed) y no se silencia."""
    read_only_dir = tmp_path / "readonly_dir"
    read_only_dir.mkdir()
    log_file = read_only_dir / "blocked_log.jsonl"
    # Crear archivo con permisos de solo lectura
    log_file.write_text("", encoding="utf-8")
    log_file.chmod(0o444)

    monkeypatch.setenv("MAQUINACHAMBA_PORTAL_LOG_FILE", str(log_file))

    try:
        with pytest.raises(PermissionError):
            MockPortalAdapter.record_portal_call(
                tenant_id="tenant-fail",
                target_url="https://example.com/job",
                idempotency_key="idemp-fail",
            )
    finally:
        # Restaurar permisos para limpieza de pytest
        log_file.chmod(0o644)
