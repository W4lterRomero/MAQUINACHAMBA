"""
Pruebas automatizadas de migraciones Alembic (P0 - Bloqueador 1).
Verifica:
1. Instalación desde cero (fresh install: alembic upgrade head en base vacía).
2. Actualización desde base existente (upgrade desde a4b7c8d9e0f1 con datos previos, sin pérdida de datos).
"""

import os
from pathlib import Path
import sqlite3
import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture
def temp_alembic_config(tmp_path):
    """Crea una configuración temporal de Alembic apuntando a una base SQLite en tmp_path."""
    db_file = tmp_path / "migration_test.db"
    db_url = f"sqlite:///{db_file}"

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg, db_file


def test_fresh_install_from_scratch(temp_alembic_config):
    """
    Prueba una instalación desde cero:
    Ejecuta 'alembic upgrade head' sobre una base de datos vacía y comprueba
    que todas las tablas del modelo existan, incluyendo las nuevas:
    - job_search_preferences
    - audit_logs
    - portal_authorizations
    - vacancies con sus columnas nuevas (fetched_at, is_real_search_result, raw_source_payload).
    """
    cfg, db_file = temp_alembic_config
    assert not db_file.exists()

    # Ejecutar migración completa hasta head
    command.upgrade(cfg, "head")
    assert db_file.exists()

    # Inspeccionar tablas resultantes
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}

    required_tables = [
        "tenants",
        "user_profiles",
        "vacancies",
        "master_cvs",
        "skills",
        "evaluations",
        "candidacies",
        "approvals",
        "crm_entries",
        "crm_events",
        "scheduled_tasks",
        "execution_logs",
        "import_batches",
        "job_search_preferences",
        "audit_logs",
        "portal_authorizations",
    ]
    for table in required_tables:
        assert table in tables, f"Falta la tabla requerida: {table}"

    # Inspeccionar columnas de vacancies
    cursor.execute("PRAGMA table_info(vacancies);")
    vac_cols = {row[1] for row in cursor.fetchall()}
    assert "fetched_at" in vac_cols
    assert "is_real_search_result" in vac_cols
    assert "raw_source_payload" in vac_cols

    # Inspeccionar columnas de job_search_preferences
    cursor.execute("PRAGMA table_info(job_search_preferences);")
    pref_cols = {row[1] for row in cursor.fetchall()}
    assert "desired_job_titles" in pref_cols
    assert "skills" in pref_cols
    assert "is_periodic_enabled" in pref_cols
    assert "source_access_authorized" in pref_cols

    # Inspeccionar columnas de portal_authorizations
    cursor.execute("PRAGMA table_info(portal_authorizations);")
    auth_cols = {row[1] for row in cursor.fetchall()}
    assert "portal_name" in auth_cols
    assert "action" in auth_cols
    assert "authorized" in auth_cols
    assert "revoked_at" in auth_cols

    conn.close()


def test_upgrade_from_existing_database_without_data_loss(temp_alembic_config):
    """
    Prueba una actualización de una base existente:
    1. Migra hasta a4b7c8d9e0f1.
    2. Inserta datos reales en vacancies e import_batches.
    3. Ejecuta 'alembic upgrade head'.
    4. Comprueba que los datos previos siguen intactos y que las nuevas columnas tienen valores por defecto.
    """
    cfg, db_file = temp_alembic_config

    # 1. Migrar hasta la revisión previa
    command.upgrade(cfg, "a4b7c8d9e0f1")

    # 2. Insertar registros de prueba previos a la nueva migración
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO vacancies (
            id, tenant_id, source, source_url, company_name, job_title, location, remote_type,
            requirements, description, dedup_hash, is_active, last_verified_at, created_at, updated_at
        ) VALUES (
            'vac-preexisting-001', 'tenant-test-1', 'remotive', 'https://example.com/job-1',
            'Acme Corp', 'Lead Backend Engineer', 'Remoto', 'remote',
            '["Python", "FastAPI"]', 'Job description',
            'deduphash1234567890', 1, '2026-10-20 12:00:00', '2026-10-20 12:00:00', '2026-10-20 12:00:00'
        );
    """)
    cursor.execute("""
        INSERT INTO import_batches (
            id, tenant_id, provider, import_type, status, vacancies_found, vacancies_imported,
            created_at, updated_at
        ) VALUES (
            'batch-001', 'tenant-test-1', 'chatgpt', 'manual_paste', 'completed', 1, 1,
            '2026-10-20 12:00:00', '2026-10-20 12:00:00'
        );
    """)
    conn.commit()
    conn.close()

    # 3. Aplicar upgrade a head (ejecuta b1c2d3e4f5a6)
    command.upgrade(cfg, "head")

    # 4. Verificar integridad y retención de datos
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute("SELECT id, company_name, is_real_search_result, fetched_at FROM vacancies WHERE id='vac-preexisting-001';")
    row = cursor.fetchone()
    assert row is not None, "El registro de vacante previo fue destruido durante la migración"
    assert row[0] == "vac-preexisting-001"
    assert row[1] == "Acme Corp"
    # Columnas nuevas añadidas aditivamente
    assert row[2] == 0  # server_default '0' para is_real_search_result

    # Verificar que las nuevas tablas están listas para operar
    cursor.execute("SELECT COUNT(*) FROM job_search_preferences;")
    assert cursor.fetchone()[0] == 0

    cursor.execute("SELECT COUNT(*) FROM audit_logs;")
    assert cursor.fetchone()[0] == 0

    cursor.execute("SELECT COUNT(*) FROM portal_authorizations;")
    assert cursor.fetchone()[0] == 0

    conn.close()
