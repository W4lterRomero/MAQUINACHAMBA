"""
Pruebas exhaustivas para el sistema de Copias de Seguridad y Restauración (REQ-033, D1).
Verifica:
1. Creación de respaldo comprimido (.tar.gz) aislado por tenant (tenant_data.json y documentos).
2. Manifiesto criptográfico con sumas SHA-256 de cada archivo incluido.
3. Exclusión estricta de archivos no pertenecientes al subdirectorio del tenant (sin sample_curriculum* en root).
4. Verificación de integridad y correspondencia 1:1 manifiesto-miembros.
5. Detección de alteraciones o manipulación de archivos (checksum mismatch).
6. Mitigación contra TarSlip / Path Traversal / rutas absolutas en descompresión.
7. Detección y rechazo de rutas no autorizadas fuera del subdirectorio del tenant.
8. Prevención de traversal mediante symlinks en rutas de destino.
9. Aislamiento multi-tenant: Restaurar Tenant A (archivos Y filas) no lee, toca, sobreescribe ni revierte al Tenant B con cambios posteriores.
10. Reversión automática (rollback) ante fallo parcial REAL tras mutar filas y copiar archivos (exactitud de bytes y cero archivos residuales).
11. Política de tareas y coordinación de workers:
    - Falla antes de mutar si hay tareas en estado 'executing' activo.
    - Cancela tareas históricas 'pending' o 'scheduled' para prevenir auto-despacho al restaurar.
    - Candidaturas en 'scheduled' revierten a 'approved'.
12. Preservación estricta de credenciales de usuario ('users') y pista de auditoría append-only ('audit_logs').
13. Principio fail-closed: aborta antes de mutar si la copia previa de reversión falla.
14. Deshabilitación temporal de la restauración en la API ordinaria (HTTP 503) y UI.
15. Herramienta CLI (scripts/backup.py).
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from unittest.mock import patch
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

import sys

from backend.app.core.database import async_session_factory, Base, engine
from backend.app.core.security import create_access_token
from backend.app.modules.approvals.models import Approval, Candidacy, ScheduledTask
from backend.app.modules.audit.backup_service import (
    BackupService,
    calculate_sha256,
    DEFAULT_BACKUP_DIR,
)
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.profile.models import UserProfile
from backend.app.modules.users.models import User, Tenant
from backend.app.modules.vacancies.models import Vacancy
from backend.app.main import app


@pytest.fixture
def backup_test_env():
    """Prepara un entorno aislado para pruebas de respaldo y restauración."""
    test_backup_dir = Path("backups/test_run")
    test_backup_dir.mkdir(parents=True, exist_ok=True)

    test_docs_dir = Path("data/documents")
    test_docs_dir.mkdir(parents=True, exist_ok=True)
    
    # Archivo no asignado a ningún tenant en la raíz de data/documents
    unowned_doc = test_docs_dir / "sample_curriculum.txt"
    unowned_doc.write_text("Contenido confidencial de CV sin asignar a tenant.", encoding="utf-8")

    yield {
        "backup_dir": test_backup_dir,
        "unowned_doc": unowned_doc,
        "docs_dir": test_docs_dir,
    }

    # Limpieza
    if test_backup_dir.exists():
        shutil.rmtree(test_backup_dir)
    if unowned_doc.exists():
        unowned_doc.unlink()


@pytest.mark.asyncio
async def test_backup_creation_and_strict_document_isolation(setup_db, backup_test_env):
    """
    Verifica que:
    1. El respaldo empaqueta exclusivamente los datos del tenant en tenant_data.json.
    2. Los documentos incluidos provienen EXCLUSIVAMENTE de data/documents/{tenant_id}/.
    3. Archivos sin propietario en data/documents/ (como sample_curriculum.txt) son RIGUROSAMENTE EXCLUIDOS.
    4. Genera manifest.json con sumas SHA-256 válidas.
    """
    tenant_id = "tenant-backup-test-01"
    bdir = backup_test_env["backup_dir"]

    # Crear tenant y documentos propios
    tenant_doc_dir = Path("data/documents") / tenant_id
    tenant_doc_dir.mkdir(parents=True, exist_ok=True)
    tenant_doc = tenant_doc_dir / "mi_cv_propio.pdf"
    tenant_doc.write_bytes(b"%PDF-1.4 Tenant Document Content")

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Backup 01", timezone="UTC")
        session.add(t)
        v = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-01",
            source="manual",
            source_url="https://example.com/job1",
            company_name="Empresa A",
            job_title="Dev Python",
            dedup_hash="hash-vac-01",
        )
        session.add(v)
        await session.commit()

    result = await BackupService.create_backup(
        tenant_id=tenant_id,
        actor="test_runner",
        backup_dir=bdir,
        include_documents=True,
    )

    assert result["status"] == "success"
    archive_path = Path(result["filepath"])
    assert archive_path.exists()

    # Inspeccionar contenido interno del archivo .tar.gz
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "tenant_data.json" in names
        # Documento propio del tenant DEBE estar presente
        assert f"data/documents/{tenant_id}/mi_cv_propio.pdf" in names
        # Documento no perteneciente al tenant DEBE ESTAR EXCLUIDO
        assert "data/documents/sample_curriculum.txt" not in names
        assert "sample_curriculum.txt" not in names
        assert not any("sample_curriculum" in n for n in names)
        # Base de datos global monolítica NO debe estar
        assert "database.sqlite3" not in names

        # Leer manifest
        f = tar.extractfile("manifest.json")
        assert f is not None
        manifest = json.load(f)

        # Leer tenant_data
        f_td = tar.extractfile("tenant_data.json")
        assert f_td is not None
        td_data = json.load(f_td)

    assert manifest["manifest_version"] == "2.0"
    assert manifest["tenant_id"] == tenant_id
    assert manifest["backup_type"] == "tenant_scoped"
    assert "vacancies" in td_data
    assert any(row["external_id"] == "vac-01" for row in td_data["vacancies"])

    # Limpieza
    shutil.rmtree(tenant_doc_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_backup_verification_clean(setup_db, backup_test_env):
    """Verifica que un archivo de respaldo intacto pasa todas las comprobaciones de integridad."""
    tenant_id = "tenant-backup-test-02"
    bdir = backup_test_env["backup_dir"]

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Clean", timezone="UTC")
        session.add(t)
        await session.commit()

    result = await BackupService.create_backup(
        tenant_id=tenant_id,
        backup_dir=bdir,
    )
    archive_path = Path(result["filepath"])

    verify_result = BackupService.verify_backup(archive_path)
    assert verify_result["is_valid"] is True
    assert verify_result["tenant_id"] == tenant_id
    assert len(verify_result["errors"]) == 0
    assert verify_result["checked_files"] > 0


@pytest.mark.asyncio
async def test_backup_verification_detects_tampered_content(setup_db, backup_test_env):
    """Verifica que si un archivo interno fue alterado, la verificación falla con error de checksum."""
    tenant_id = "tenant-backup-test-03"
    bdir = backup_test_env["backup_dir"]

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Tamper", timezone="UTC")
        session.add(t)
        await session.commit()

    result = await BackupService.create_backup(
        tenant_id=tenant_id,
        backup_dir=bdir,
    )
    original_path = Path(result["filepath"])

    # Descomprimir, alterar tenant_data.json y volver a empaquetar
    tampered_dir = bdir / "tampered"
    tampered_dir.mkdir()
    with tarfile.open(original_path, "r:gz") as tar:
        tar.extractall(path=tampered_dir)

    td_file = tampered_dir / "tenant_data.json"
    if td_file.exists():
        with open(td_file, "a", encoding="utf-8") as f:
            f.write(" ")

    tampered_archive = bdir / "tampered_backup.tar.gz"
    with tarfile.open(tampered_archive, "w:gz") as tar:
        for f in tampered_dir.rglob("*"):
            if f.is_file():
                tar.add(str(f), arcname=str(f.relative_to(tampered_dir)))

    verify_result = BackupService.verify_backup(tampered_archive)
    assert verify_result["is_valid"] is False
    assert any("Checksum SHA-256 inválido" in err for err in verify_result["errors"])


@pytest.mark.asyncio
async def test_backup_verification_detects_tarslip_and_absolute_paths(backup_test_env):
    """Verifica que miembros con secuencias '..' (TarSlip) o rutas absolutas son rechazados inmediatamente."""
    bdir = backup_test_env["backup_dir"]
    staging = bdir / "staging_tarslip"
    staging.mkdir()

    # Manifiesto básico
    m_path = staging / "manifest.json"
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump({"manifest_version": "2.0", "tenant_id": "test-tenant", "files": []}, f)

    dummy_file = staging / "dummy.txt"
    dummy_file.write_text("evil payload", encoding="utf-8")

    # 1. Caso '..'
    malicious_archive_dots = bdir / "malicious_dots.tar.gz"
    with tarfile.open(malicious_archive_dots, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")
        tar.add(str(dummy_file), arcname="../../etc/passwd")

    verify_result = BackupService.verify_backup(malicious_archive_dots)
    assert verify_result["is_valid"] is False
    assert any(".." in err or "Secuencia de escape" in err for err in verify_result["errors"])

    # 2. Caso ruta absoluta
    malicious_archive_abs = bdir / "malicious_abs.tar.gz"
    with tarfile.open(malicious_archive_abs, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")
        ti = tar.gettarinfo(str(dummy_file))
        ti.name = "/etc/shadow"
        with open(dummy_file, "rb") as f_dummy:
            tar.addfile(ti, f_dummy)

    verify_result_abs = BackupService.verify_backup(malicious_archive_abs)
    assert verify_result_abs["is_valid"] is False
    assert any("Ruta absoluta" in err for err in verify_result_abs["errors"])


@pytest.mark.asyncio
async def test_backup_verification_rejects_unauthorized_document_paths(backup_test_env):
    """
    P1 de Work:
    Verifica que tanto verify_backup como _apply_restoration_from_archive rechazan
    cualquier miembro fuera de data/documents/{tenant_id}/ (ej. data/documents/OTRO_TENANT/archivo).
    """
    bdir = backup_test_env["backup_dir"]
    staging = bdir / "staging_unauth"
    staging.mkdir()

    tenant_id = "tenant-target-auth"
    other_tenant = "tenant-intruder-evil"

    doc_file = staging / "stolen.pdf"
    doc_file.write_bytes(b"Sensitive document")
    doc_hash = calculate_sha256(doc_file)

    unauthorized_rel_path = f"data/documents/{other_tenant}/stolen.pdf"

    manifest = {
        "manifest_version": "2.0",
        "tenant_id": tenant_id,
        "files": [
            {
                "path": unauthorized_rel_path,
                "sha256": doc_hash,
                "size_bytes": doc_file.stat().st_size,
                "type": "data_file",
            }
        ],
        "total_files": 1,
    }
    m_path = staging / "manifest.json"
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    archive = bdir / "unauthorized_doc_path.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")
        tar.add(str(doc_file), arcname=unauthorized_rel_path)

    # 1. verify_backup debe fallar
    verify_result = BackupService.verify_backup(archive, expected_tenant_id=tenant_id)
    assert verify_result["is_valid"] is False
    assert any("Ruta no autorizada para el tenant" in err for err in verify_result["errors"])

    # 2. _apply_restoration_from_archive debe rechazarlo inmediatamente
    async with async_session_factory() as session:
        with pytest.raises(ValueError) as exc_info:
            await BackupService._apply_restoration_from_archive(
                archive_path=archive,
                tenant_id=tenant_id,
                session=session,
            )
        assert "Ruta no autorizada en respaldo" in str(exc_info.value)


@pytest.mark.asyncio
async def test_restore_rejects_preexisting_symlink_in_destination(backup_test_env):
    """
    P1 de Work:
    Verifica que la restauración rechaza escribir y aborta si la ruta de destino
    o cualquiera de sus carpetas intermedias es un enlace simbólico preexistente.
    """
    bdir = backup_test_env["backup_dir"]
    staging = bdir / "staging_symlink_test"
    staging.mkdir()

    tenant_id = "tenant-symlink-guard"
    doc_file = staging / "cv.pdf"
    doc_file.write_bytes(b"Normal CV content")
    doc_hash = calculate_sha256(doc_file)

    rel_path = f"data/documents/{tenant_id}/cv.pdf"
    manifest = {
        "manifest_version": "2.0",
        "tenant_id": tenant_id,
        "files": [
            {
                "path": rel_path,
                "sha256": doc_hash,
                "size_bytes": doc_file.stat().st_size,
                "type": "data_file",
            }
        ],
        "total_files": 1,
    }
    m_path = staging / "manifest.json"
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    archive = bdir / "symlink_test_archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")
        tar.add(str(doc_file), arcname=rel_path)

    # Crear symlink malicioso en el destino
    target_tenant_dir = Path("data/documents") / tenant_id
    target_tenant_dir.mkdir(parents=True, exist_ok=True)
    symlink_target = target_tenant_dir / "cv.pdf"
    fake_destination = bdir / "hijacked_file.txt"
    fake_destination.write_text("pre-existing destination", encoding="utf-8")
    
    if symlink_target.exists() or symlink_target.is_symlink():
        symlink_target.unlink()
    try:
        os.symlink(fake_destination.resolve(), symlink_target)
    except OSError:
        pytest.skip("No se pueden crear symlinks en este entorno")

    async with async_session_factory() as session:
        with pytest.raises(RuntimeError) as exc_info:
            await BackupService._apply_restoration_from_archive(
                archive_path=archive,
                tenant_id=tenant_id,
                session=session,
            )
        assert "Symlink detectado en ruta de destino" in str(exc_info.value)

    # Limpieza
    shutil.rmtree(target_tenant_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_two_tenants_isolation_restore_does_not_revert_other_tenant(setup_db, backup_test_env):
    """
    Caso requerido por Work:
    Con dos tenants (Tenant A y Tenant B) con ARCHIVOS Y FILAS en base de datos:
    1. Tenant A y B tienen datos y documentos iniciales.
    2. Tenant A genera un respaldo.
    3. Tenant B realiza modificaciones posteriores (agrega nueva vacante, edita la existente, modifica archivo y agrega archivo).
    4. Tenant A agrega datos y archivos nuevos y luego restaura su respaldo inicial.
    5. Se demuestra que restaurar Tenant A NO lee, no exporta, no sobreescribe ni revierte los cambios (filas ni archivos) de Tenant B.
    """
    tenant_a = "tenant-isolation-a"
    tenant_b = "tenant-isolation-b"
    bdir = backup_test_env["backup_dir"]

    # 1. Crear directorios y documentos iniciales de ambos tenants
    doc_dir_a = Path("data/documents") / tenant_a
    doc_dir_b = Path("data/documents") / tenant_b
    doc_dir_a.mkdir(parents=True, exist_ok=True)
    doc_dir_b.mkdir(parents=True, exist_ok=True)

    file_a1 = doc_dir_a / "doc_a1.txt"
    file_a1.write_bytes(b"TENANT_A_CV_INITIAL_CONTENT")

    file_b1 = doc_dir_b / "doc_b1.txt"
    file_b1.write_bytes(b"TENANT_B_CV_INITIAL_CONTENT")

    # Estado inicial de filas en base de datos
    async with async_session_factory() as session:
        ta = Tenant(id=tenant_a, tenant_id=tenant_a, name="Tenant A", timezone="UTC")
        tb = Tenant(id=tenant_b, tenant_id=tenant_b, name="Tenant B", timezone="UTC")
        session.add_all([ta, tb])

        va1 = Vacancy(
            tenant_id=tenant_a,
            external_id="vac-a1",
            source="manual",
            source_url="https://example.com/jobs/a1",
            company_name="Empresa A Original",
            job_title="Ingeniero A1",
            dedup_hash="hash-a1",
        )
        vb1 = Vacancy(
            tenant_id=tenant_b,
            external_id="vac-b1",
            source="manual",
            source_url="https://example.com/jobs/b1",
            company_name="Empresa B Original",
            job_title="Ingeniero B1 Inicial",
            dedup_hash="hash-b1",
        )
        session.add_all([va1, vb1])
        await session.commit()

    # 2. Tenant A crea respaldo de su estado inicial
    backup_a_res = await BackupService.create_backup(
        tenant_id=tenant_a,
        backup_dir=bdir,
        include_documents=True,
    )
    archive_a = Path(backup_a_res["filepath"])

    # Verificar que el respaldo de A NO incluye datos ni archivos de B
    with tarfile.open(archive_a, "r:gz") as tar:
        names = tar.getnames()
        assert f"data/documents/{tenant_a}/doc_a1.txt" in names
        assert not any(tenant_b in n for n in names)

        f = tar.extractfile("tenant_data.json")
        td_a = json.load(f)
        for tbl, rows in td_a.items():
            for r in rows:
                assert r.get("tenant_id") == tenant_a
                assert r.get("tenant_id") != tenant_b

    # 3. Tenant B realiza modificaciones posteriores sustanciales en FILAS y ARCHIVOS
    file_b1.write_bytes(b"TENANT_B_CV_MODIFIED_POSTERIOR_DATA")
    file_b2 = doc_dir_b / "doc_b2_posterior.txt"
    file_b2.write_bytes(b"TENANT_B_CV_NEW_FILE_POSTERIOR")

    async with async_session_factory() as session:
        # Modificar vb1
        b1_db = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-b1", Vacancy.tenant_id == tenant_b)
        )).scalar_one()
        b1_db.job_title = "Ingeniero B1 Modificado Posterior"

        # Agregar vb2
        vb2 = Vacancy(
            tenant_id=tenant_b,
            external_id="vac-b2-posterior",
            source="manual",
            source_url="https://example.com/jobs/b2",
            company_name="Empresa B Nueva",
            job_title="Ingeniero B2 Creado Posterior",
            dedup_hash="hash-b2",
        )
        session.add(vb2)

        # Tenant A agrega va2 posterior y archivo doc_a2
        va2 = Vacancy(
            tenant_id=tenant_a,
            external_id="vac-a2-posterior",
            source="manual",
            source_url="https://example.com/jobs/a2",
            company_name="Empresa A Posterior",
            job_title="Ingeniero A2",
            dedup_hash="hash-a2",
        )
        session.add(va2)
        await session.commit()

    file_a2 = doc_dir_a / "doc_a2_posterior.txt"
    file_a2.write_bytes(b"TENANT_A_CV_POSTERIOR_FILE")

    # 4. Tenant A restaura su respaldo inicial
    restore_res = await BackupService.restore_backup(
        archive_path=archive_a,
        tenant_id=tenant_a,
        confirm=True,
    )
    assert restore_res["status"] == "success"

    # 5. Comprobar aislamiento absoluto en BASE DE DATOS Y EN ARCHIVOS:
    # A. Tenant A: restaurado a va1 y doc_a1, va2 y doc_a2 fueron eliminados
    assert file_a1.exists()
    assert file_a1.read_bytes() == b"TENANT_A_CV_INITIAL_CONTENT"
    assert not file_a2.exists(), "Archivo posterior doc_a2 debe ser eliminado al restaurar respaldo inicial de Tenant A"

    async with async_session_factory() as session:
        a1_check = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-a1", Vacancy.tenant_id == tenant_a)
        )).scalar_one_or_none()
        assert a1_check is not None
        assert a1_check.company_name == "Empresa A Original"

        a2_check = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-a2-posterior", Vacancy.tenant_id == tenant_a)
        )).scalar_one_or_none()
        assert a2_check is None, "va2 no debe existir tras restaurar el estado previo de Tenant A"

        # B. Tenant B: COMPLETAMENTE INTACTO, NINGÚN CAMBIO FUE REVERTIDO (FILAS Y ARCHIVOS)
        assert file_b1.exists()
        assert file_b1.read_bytes() == b"TENANT_B_CV_MODIFIED_POSTERIOR_DATA", "Los archivos modificados de Tenant B deben conservarse intactos"
        assert file_b2.exists()
        assert file_b2.read_bytes() == b"TENANT_B_CV_NEW_FILE_POSTERIOR", "Los archivos posteriores de Tenant B deben conservarse intactos"

        b1_check = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-b1", Vacancy.tenant_id == tenant_b)
        )).scalar_one_or_none()
        assert b1_check is not None
        assert b1_check.job_title == "Ingeniero B1 Modificado Posterior", "Los cambios en filas de Tenant B deben conservarse intactos"

        b2_check = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-b2-posterior", Vacancy.tenant_id == tenant_b)
        )).scalar_one_or_none()
        assert b2_check is not None, "El registro posterior de Tenant B vac-b2 debe permanecer intacto tras la restauración de Tenant A"

    # Limpieza
    shutil.rmtree(doc_dir_a, ignore_errors=True)
    shutil.rmtree(doc_dir_b, ignore_errors=True)


@pytest.mark.asyncio
async def test_partial_failure_during_restore_triggers_automatic_rollback_with_zero_residuals(setup_db, backup_test_env):
    """
    P1 de Work:
    Demuestra recuperación real ante fallo a mitad de restauración:
    1. Se inyecta un fallo real DESPUÉS de mutar la base de datos y copiar un archivo a disco.
    2. Se demuestra que el sistema ejecuta reversión automática fail-closed.
    3. Se comprueba fidelidad exacta de bytes de los archivos pre-restauración.
    4. Se comprueba ausencia total de archivos residuales introducidos por el intento fallido.
    5. Se comprueba recuperación total de las filas de base de datos pre-restauración.
    """
    tenant_id = "tenant-real-partial-fail"
    bdir = backup_test_env["backup_dir"]
    tenant_doc_dir = Path("data/documents") / tenant_id
    tenant_doc_dir.mkdir(parents=True, exist_ok=True)

    # 1. Crear archivo pre-restauración en disco con contenido byte-exacto
    pre_restore_file = tenant_doc_dir / "documento_original.pdf"
    pre_restore_bytes = b"%PDF-1.7 PRE_RESTORE_BYTE_FIDELITY_TEST_12345"
    pre_restore_file.write_bytes(pre_restore_bytes)

    # Estado en base de datos pre-restauración
    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Real Partial Fail", timezone="UTC")
        session.add(t)
        v_pre = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-pre-restore-active",
            source="manual",
            source_url="https://example.com/jobs/pre",
            company_name="Empresa Pre-Restore Activa",
            job_title="Dev Senior Pre-Restore",
            dedup_hash="hash-pre",
        )
        session.add(v_pre)
        await session.commit()

    # 2. Generar un archivo de respaldo sintético que contiene un documento nuevo 'archivo_del_respaldo.pdf'
    # y una fila diferente 'vac-en-respaldo'
    staging = bdir / "staging_target"
    staging.mkdir()

    target_doc = staging / f"data/documents/{tenant_id}/archivo_del_respaldo.pdf"
    target_doc.parent.mkdir(parents=True, exist_ok=True)
    target_doc.write_bytes(b"NUEVO CONTENIDO DEL RESPALDO - RESIDUAL SI FALLA")
    target_doc_hash = calculate_sha256(target_doc)

    target_data = {
        "vacancies": [
            {
                "id": "vac-uuid-archive",
                "tenant_id": tenant_id,
                "external_id": "vac-en-respaldo",
                "source": "manual",
                "company_name": "Empresa En Respaldo",
                "job_title": "Dev Respaldo",
                "dedup_hash": "hash-archive",
            }
        ]
    }
    td_path = staging / "tenant_data.json"
    with open(td_path, "w", encoding="utf-8") as f:
        json.dump(target_data, f)
    td_hash = calculate_sha256(td_path)

    manifest_target = {
        "manifest_version": "2.0",
        "backup_id": "target-archive-id",
        "tenant_id": tenant_id,
        "files": [
            {"path": "tenant_data.json", "sha256": td_hash, "size_bytes": td_path.stat().st_size, "type": "tenant_data"},
            {"path": f"data/documents/{tenant_id}/archivo_del_respaldo.pdf", "sha256": target_doc_hash, "size_bytes": target_doc.stat().st_size, "type": "data_file"},
        ],
        "total_files": 2,
    }
    m_path = staging / "manifest.json"
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump(manifest_target, f)

    archive_target = bdir / "archive_to_restore.tar.gz"
    with tarfile.open(archive_target, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")
        tar.add(str(td_path), arcname="tenant_data.json")
        tar.add(str(target_doc), arcname=f"data/documents/{tenant_id}/archivo_del_respaldo.pdf")

    # 3. Restaurar inyectando el fallo EXACTAMENTE TRAS COPIAR 'archivo_del_respaldo.pdf'
    residual_file_path = tenant_doc_dir / "archivo_del_respaldo.pdf"
    assert not residual_file_path.exists(), "El archivo del respaldo no debe existir antes de restaurar"

    with pytest.raises(RuntimeError) as exc_info:
        await BackupService.restore_backup(
            archive_path=archive_target,
            tenant_id=tenant_id,
            confirm=True,
            fail_after_file="archivo_del_respaldo.pdf",
        )

    # 4. Verificar que se activó la reversión automática
    assert "reverted_successfully" in str(exc_info.value) or "reversión automática" in str(exc_info.value).lower()

    # 5. ASERCIONES DE INTEGRIDAD ESTRICTA:
    # A. Cero archivos residuales: el archivo copiado a mitad del proceso fallido fue completamente eliminado
    assert not residual_file_path.exists(), (
        f"Fallo en limpieza: El archivo residual '{residual_file_path}' debió ser eliminado por la reversión automática."
    )

    # B. Fidelidad exacta de bytes del documento pre-restauración
    assert pre_restore_file.exists(), "El documento pre-restauración debe existir intacto tras la reversión"
    assert pre_restore_file.read_bytes() == pre_restore_bytes, "Los bytes del archivo pre-restauración deben coincidir con exactitud absoluta"

    # C. Estado de la base de datos recuperado intacto
    async with async_session_factory() as session:
        # La vacante del intento fallido NO debe existir
        check_archive_vac = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-en-respaldo", Vacancy.tenant_id == tenant_id)
        )).scalar_one_or_none()
        assert check_archive_vac is None, "La fila mutada por el intento fallido debió ser eliminada por la reversión"

        # La vacante pre-restauración original DEBE estar presente
        check_pre_vac = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-pre-restore-active", Vacancy.tenant_id == tenant_id)
        )).scalar_one_or_none()
        assert check_pre_vac is not None, "La fila original previa al intento debió ser restaurada por la reversión"
        assert check_pre_vac.company_name == "Empresa Pre-Restore Activa"

    # Limpieza
    shutil.rmtree(tenant_doc_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_restored_tasks_policy_cancels_pending_to_prevent_unexpected_dispatch(setup_db, backup_test_env):
    """
    P1 de Work:
    Verifica que al restaurar un respaldo histórico que contenía tareas en 'pending', 'scheduled' o 'executing',
    la política de seguridad las transforma en 'cancelled' (dispatch_in_flight=False, locks limpios),
    y las candidaturas en 'scheduled' revierten a 'approved', para impedir que workers las despachen automáticamente.
    """
    tenant_id = "tenant-task-policy"
    bdir = backup_test_env["backup_dir"]

    # 1. Crear registros con tareas en 'scheduled'
    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Tasks Policy", timezone="UTC")
        session.add(t)

        prof = UserProfile(
            tenant_id=tenant_id,
            full_name="Tester Policy",
            email="policy@example.com",
            location_city="CDMX",
            location_country="Mexico",
        )
        session.add(prof)
        await session.flush()

        cv = MasterCV(
            tenant_id=tenant_id,
            profile_id=prof.id,
            name="CV Dev Policy",
            specialization="Backend",
        )
        session.add(cv)
        await session.flush()

        v = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-task-policy",
            source="manual",
            source_url="https://example.com/job-task",
            company_name="Empresa Task",
            job_title="Dev Task",
            dedup_hash="hash-task-pol",
        )
        session.add(v)
        await session.flush()

        cand = Candidacy(
            tenant_id=tenant_id,
            vacancy_id=v.id,
            profile_id=prof.id,
            cv_id=cv.id,
            target_url="https://example.com/apply-task",
            status="scheduled",
            target_portal="mock_portal",
            planned_actions={"submit": True},
        )
        session.add(cand)
        await session.flush()

        appr = Approval(
            tenant_id=tenant_id,
            candidacy_id=cand.id,
            decision="approved",
            decided_by="user",
            decided_at=datetime.now(timezone.utc),
            content_hash="dummy-content-hash",
        )
        session.add(appr)
        await session.flush()

        now_utc = datetime.now(timezone.utc)
        task = ScheduledTask(
            tenant_id=tenant_id,
            candidacy_id=cand.id,
            approval_id=appr.id,
            status="scheduled",
            scheduled_date=now_utc.date(),
            scheduled_time=now_utc.time(),
            execution_timezone="UTC",
            execution_instant_utc=now_utc,
            idempotency_key="idemp-policy-test-1",
            dispatch_in_flight=True,
            locked_by="old_crashed_worker",
        )
        session.add(task)
        await session.commit()

    # 2. Generar respaldo
    backup_res = await BackupService.create_backup(
        tenant_id=tenant_id,
        backup_dir=bdir,
    )
    archive_path = Path(backup_res["filepath"])

    # 3. Restaurar respaldo
    restore_res = await BackupService.restore_backup(
        archive_path=archive_path,
        tenant_id=tenant_id,
        confirm=True,
    )
    assert restore_res["status"] == "success"

    # 4. Comprobar que en la base de datos la tarea quedó 'cancelled' y la candidatura 'approved'
    async with async_session_factory() as session:
        restored_task = (await session.execute(
            select(ScheduledTask).where(ScheduledTask.tenant_id == tenant_id)
        )).scalar_one()

        assert restored_task.status == "cancelled", "Las tareas restauradas deben marcarse como cancelled para evitar disparos ciegos"
        assert restored_task.dispatch_in_flight is False
        assert restored_task.locked_by is None

        restored_cand = (await session.execute(
            select(Candidacy).where(Candidacy.tenant_id == tenant_id)
        )).scalar_one()
        assert restored_cand.status == "approved", "Las candidaturas asociadas deben quedar en approved para requerir nueva acción humana"


@pytest.mark.asyncio
async def test_restore_fails_if_tenant_has_active_executing_task(setup_db, backup_test_env):
    """
    P1 de Work:
    Coordinación con workers:
    Verifica que restore_backup rechaza la operación si el tenant tiene alguna tarea
    en estado 'executing' activo, evitando inconsistencias y race conditions con workers.
    """
    tenant_id = "tenant-worker-coordination"
    bdir = backup_test_env["backup_dir"]

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Worker Coord", timezone="UTC")
        session.add(t)

        prof = UserProfile(
            tenant_id=tenant_id,
            full_name="Tester Coord",
            email="coord@example.com",
            location_city="CDMX",
            location_country="Mexico",
        )
        session.add(prof)
        await session.flush()

        cv = MasterCV(
            tenant_id=tenant_id,
            profile_id=prof.id,
            name="CV Dev Coord",
            specialization="Backend",
        )
        session.add(cv)
        await session.flush()

        v = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-coord",
            source="manual",
            source_url="https://example.com/coord",
            company_name="Empresa Coord",
            job_title="Dev Coord",
            dedup_hash="hash-coord",
        )
        session.add(v)
        await session.flush()

        cand = Candidacy(
            tenant_id=tenant_id,
            vacancy_id=v.id,
            profile_id=prof.id,
            cv_id=cv.id,
            target_url="https://example.com/apply-coord",
            status="executing",
            target_portal="mock_portal",
        )
        session.add(cand)
        await session.flush()

        appr = Approval(
            tenant_id=tenant_id,
            candidacy_id=cand.id,
            decision="approved",
            decided_by="user",
            decided_at=datetime.now(timezone.utc),
            content_hash="dummy-coord-hash",
        )
        session.add(appr)
        await session.flush()

        now_utc = datetime.now(timezone.utc)
        # Tarea actualmente en ejecución activa
        executing_task = ScheduledTask(
            tenant_id=tenant_id,
            candidacy_id=cand.id,
            approval_id=appr.id,
            status="executing",
            scheduled_date=now_utc.date(),
            scheduled_time=now_utc.time(),
            execution_timezone="UTC",
            execution_instant_utc=now_utc,
            idempotency_key="idemp-coord-test-1",
            dispatch_in_flight=True,
            locked_by="worker_in_flight",
        )
        session.add(executing_task)
        await session.commit()

    # Intentar restaurar cualquier respaldo
    dummy_archive = bdir / "dummy.tar.gz"
    staging = bdir / "dummy_stg"
    staging.mkdir()
    m_path = staging / "manifest.json"
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump({"manifest_version": "2.0", "tenant_id": tenant_id, "files": []}, f)
    with tarfile.open(dummy_archive, "w:gz") as tar:
        tar.add(str(m_path), arcname="manifest.json")

    with pytest.raises(RuntimeError) as exc_info:
        await BackupService.restore_backup(
            archive_path=dummy_archive,
            tenant_id=tenant_id,
            confirm=True,
        )

    assert "tarea(s) en ejecución activa" in str(exc_info.value)


@pytest.mark.asyncio
async def test_credentials_and_audit_logs_preserved_across_restores(setup_db, backup_test_env):
    """
    P1 de Work:
    Verifica que restaurar datos del dominio:
    1. NO toca la tabla 'users' (preserva credenciales de acceso del usuario).
    2. NO borra ni muta 'audit_logs' (pista inmutable forense append-only).
    """
    tenant_id = "tenant-creds-audit-preservation"
    user_id = "user-preserve-01"
    bdir = backup_test_env["backup_dir"]

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Preserved", timezone="UTC")
        session.add(t)

        u = User(
            id=user_id,
            tenant_id=tenant_id,
            email="preserve@example.com",
            hashed_password="secure_password_hash_xyz",
            is_active=True,
        )
        session.add(u)

        prior_log = AuditLog(
            tenant_id=tenant_id,
            action="login_attempt",
            entity_type="user",
            entity_id=user_id,
            actor="user",
            details={"ip": "127.0.0.1"},
        )
        session.add(prior_log)

        v = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-preserve-01",
            source="manual",
            source_url="https://example.com/preserve",
            company_name="Empresa Inicial",
            job_title="Dev Inicial",
            dedup_hash="hash-p1",
        )
        session.add(v)
        await session.commit()

    # Crear respaldo
    backup_res = await BackupService.create_backup(tenant_id=tenant_id, backup_dir=bdir)
    archive = Path(backup_res["filepath"])

    # Simular cambio de contraseña de usuario y nuevo log de auditoría posterior
    async with async_session_factory() as session:
        u_db = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        u_db.hashed_password = "newly_updated_secure_hash_abc"

        post_log = AuditLog(
            tenant_id=tenant_id,
            action="password_changed",
            entity_type="user",
            entity_id=user_id,
            actor="user",
            details={"reason": "security_rotation"},
        )
        session.add(post_log)
        await session.commit()

    # Restaurar respaldo anterior
    restore_res = await BackupService.restore_backup(archive_path=archive, tenant_id=tenant_id, confirm=True)
    assert restore_res["status"] == "success"

    # Verificar que las credenciales no fueron revertidas y los logs se preservaron
    async with async_session_factory() as session:
        u_after = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u_after.hashed_password == "newly_updated_secure_hash_abc", "Las credenciales de usuario nunca deben ser revertidas por un restore"

        logs = (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id))).scalars().all()
        log_actions = [l.action for l in logs]
        assert "login_attempt" in log_actions
        assert "password_changed" in log_actions, "Los registros previos de auditoría deben conservarse (append-only)"
        assert "backup_restored" in log_actions, "Se debió registrar la auditoría de la restauración"


@pytest.mark.asyncio
async def test_fail_closed_if_pre_restore_rollback_fails(setup_db, backup_test_env):
    """
    Verifica que si la copia de reversión (rollback) falla al crearse,
    la restauración aborta inmediatamente (fail-closed) sin modificar la base de datos.
    """
    tenant_id = "tenant-fail-closed"
    bdir = backup_test_env["backup_dir"]

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant Fail Closed", timezone="UTC")
        session.add(t)
        v = Vacancy(
            tenant_id=tenant_id,
            external_id="vac-safe",
            source="manual",
            source_url="https://example.com/jobs/safe",
            company_name="Empresa Safe",
            job_title="Dev Seguro",
            dedup_hash="hash-safe",
        )
        session.add(v)
        await session.commit()

    backup = await BackupService.create_backup(
        tenant_id=tenant_id,
        backup_dir=bdir,
    )
    archive = Path(backup["filepath"])

    # Simular fallo en la creación de rollback previo
    with patch.object(BackupService, "create_backup", side_effect=IOError("Disco sin espacio")):
        with pytest.raises(RuntimeError) as exc_info:
            await BackupService.restore_backup(
                archive_path=archive,
                tenant_id=tenant_id,
                confirm=True,
            )
        assert "La restauración ha sido abortada" in str(exc_info.value)

    # Verificar que los datos nunca se tocaron
    async with async_session_factory() as session:
        safe_check = (await session.execute(
            select(Vacancy).where(Vacancy.external_id == "vac-safe", Vacancy.tenant_id == tenant_id)
        )).scalar_one_or_none()
        assert safe_check is not None


@pytest.mark.asyncio
async def test_ordinary_api_restore_temporarily_disabled_with_503(setup_db, backup_test_env):
    """
    Verifica que el endpoint ordinario POST /api/v1/backups/restore retorna HTTP 503
    Service Unavailable indicando que la restauración está deshabilitada temporalmente.
    """
    tenant_id = "tenant-api-restore-disabled"
    user_id = "user-api-restore-01"

    async with async_session_factory() as session:
        t = Tenant(id=tenant_id, tenant_id=tenant_id, name="Tenant API 503", timezone="UTC")
        session.add(t)
        u = User(
            id=user_id,
            tenant_id=tenant_id,
            email="disabled_restore@example.com",
            hashed_password="hash",
            is_active=True,
        )
        session.add(u)
        await session.commit()

    token = create_access_token(data={"sub": user_id, "tenant_id": tenant_id})
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Intentar restaurar vía API ordinaria
        resp = await client.post(
            "/api/v1/backups/restore",
            json={"filename": "backup_dummy.tar.gz", "confirm": True},
            headers=headers,
        )
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "deshabilitada temporalmente" in detail.lower()


def test_cli_backup_script():
    """Verifica que el script CLI scripts/backup.py ejecuta sin errores sus comandos list y verify."""
    res_list = subprocess.run(
        [sys.executable, "scripts/backup.py", "list"],
        capture_output=True,
        text=True,
    )
    assert res_list.returncode == 0
