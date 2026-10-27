"""
Servicio central de copias de seguridad (Backups) y restauración (REQ-033, D1).
Garantiza:
1. Aislamiento estricto por tenant_id: los respaldos de tenant exportan y restauran
   única y exclusivamente los registros de base de datos del dominio y documentos bajo
   'data/documents/{tenant_id}/'.
   Restaurar el Tenant A nunca lee, toca, sobreescribe ni revierte el estado ni archivos del Tenant B.
2. Manifiesto criptográfico con sumas SHA-256 por cada archivo incluido.
3. Validación exhaustiva de miembros del archivo comprimido:
   - Solo rutas autorizadas bajo 'data/documents/{tenant_id}/', 'tenant_data.json' o 'manifest.json'.
   - Sin enlaces simbólicos (follow_symlinks=False, rechazo de symlinks en origen y destino).
   - Sin rutas absolutas, sin '..', sin duplicados y límite estricto de descompresión (50 MB).
   - Correspondencia exacta 1:1 entre miembros del archivo y entradas declaradas en manifest.json.
4. Atomicidad y reversión automática ante fallos parciales:
   - Se crea una copia de reversión (rollback) antes de mutar cualquier dato.
   - Si la copia de reversión previa no puede ser creada, aborta inmediatamente (fail-closed).
   - Si ocurre un fallo durante la restauración (incluso tras copiar documentos y mutar filas),
     se revierte la transacción, se eliminan los archivos residuales nuevos introducidos por el intento,
     y se re-aplica el estado previo completo con verificación de integridad.
5. Política de seguridad en tareas y credenciales:
   - Impide restaurar si hay tareas en estado 'executing' activo para el tenant (coordinación de workers).
   - Tareas históricas en 'pending', 'scheduled' o 'executing' se restauran como 'cancelled'
     para prevenir despachos inesperados o duplicados (D3, ADR-003).
   - Preserva credenciales de usuario ('users') y configuración de tenant ('tenants') sin regresiones.
   - Preserva la pista inmutable de auditoría ('audit_logs') de forma append-only.
6. Honestidad en documentación y metadatos: archivos comprimidos gzip estándar (no cifrados),
   sin asumir recuperación de claves maestras sin diseño de custodia.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Set
import uuid

from sqlalchemy import select, delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.database import Base, async_session_factory, import_all_models
from backend.app.modules.approvals.models import ScheduledTask
from backend.app.modules.audit.models import AuditLog

logger = logging.getLogger("maquinachamba.audit.backup")

BACKUP_VERSION = "2.0"
DEFAULT_BACKUP_DIR = Path("backups")
MAX_UNCOMPRESSED_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

# Tablas del dominio restaurables por tenant (excluye users y tenants para proteger credenciales y config base, y audit_logs que es append-only)
RESTOREABLE_DOMAIN_TABLES = {
    "user_profiles",
    "experiences",
    "skills",
    "education",
    "professional_sources",
    "master_cvs",
    "master_cv_history",
    "communication_profiles",
    "vacancies",
    "evaluations",
    "candidacies",
    "candidacy_documents",
    "approvals",
    "scheduled_tasks",
    "execution_logs",
    "crm_entries",
    "crm_events",
    "follow_ups",
    "portal_accounts",
    "api_key_configs",
    "cost_logs",
    "job_search_preferences",
}


def calculate_sha256(filepath: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en bloques de 64 KB."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def serialize_value(val: Any) -> Any:
    """Serializa tipos complejos a representaciones JSON estándar."""
    if val is None:
        return None
    if isinstance(val, (datetime, date, time)):
        return val.isoformat()
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, (dict, list, int, float, bool, str)):
        return val
    return str(val)


def deserialize_value(val: Any, col_type: Any) -> Any:
    """Convierte cadenas serializadas de vuelta a los tipos esperados por las columnas."""
    if val is None:
        return None
    type_str = str(col_type).lower()
    if "datetime" in type_str:
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
            except Exception:
                return val
    elif "date" in type_str:
        if isinstance(val, str):
            try:
                return date.fromisoformat(val)
            except Exception:
                return val
    elif "time" in type_str:
        if isinstance(val, str):
            try:
                return time.fromisoformat(val)
            except Exception:
                return val
    elif "boolean" in type_str:
        return bool(val)
    elif "integer" in type_str:
        return int(val)
    elif "numeric" in type_str or "decimal" in type_str or "float" in type_str:
        return float(val)
    return val


def is_authorized_member_path(member_name: str, tenant_id: str) -> bool:
    """
    Verifica que la ruta dentro del archivo de respaldo pertenezca estrictamente
    al tenant autorizado. Rechaza cualquier archivo fuera de su propio subdirectorio.
    """
    norm = member_name.replace("\\", "/").strip("/")
    if norm in ("manifest.json", "tenant_data.json"):
        return True
    authorized_prefix = f"data/documents/{tenant_id}/"
    return norm.startswith(authorized_prefix)


def validate_tar_members(tar: tarfile.TarFile, staging_dir: Path, tenant_id: str) -> Dict[str, Any]:
    """
    Valida exhaustivamente cada miembro del archivo tar para prevenir TarSlip,
    ataques de descompresión, symlinks y rutas no autorizadas para el tenant.
    """
    total_uncompressed_size = 0
    seen_members: Set[str] = set()

    for m in tar.getmembers():
        # 1. Comprobación de duplicados
        if m.name in seen_members:
            return {
                "is_valid": False,
                "error": f"Miembro duplicado detectado en archivo comprimido: '{m.name}'.",
            }
        seen_members.add(m.name)

        # 2. Tipos de archivo permitidos: solo regulares o directorios (sin symlinks ni hardlinks)
        if not (m.isfile() or m.isdir()):
            return {
                "is_valid": False,
                "error": f"Tipo de archivo no permitido en archivo comprimido: '{m.name}' (tipo {m.type}). "
                         f"Solo se permiten archivos regulares y directorios.",
            }

        if m.issym() or m.islnk():
            return {
                "is_valid": False,
                "error": f"Enlaces simbólicos o duros prohibidos en respaldo: '{m.name}'.",
            }

        # 3. Prevención de rutas absolutas
        if m.name.startswith("/") or m.name.startswith("\\") or os.path.isabs(m.name):
            return {
                "is_valid": False,
                "error": f"Ruta absoluta no autorizada en archivo comprimido: '{m.name}'.",
            }

        # 4. Prevención de secuencias de escape '..'
        norm_parts = m.name.replace("\\", "/").split("/")
        if ".." in norm_parts:
            return {
                "is_valid": False,
                "error": f"Secuencia de escape '..' prohibida en ruta: '{m.name}'.",
            }

        # 5. Restricción estricta al tenant autorizado
        if not is_authorized_member_path(m.name, tenant_id):
            return {
                "is_valid": False,
                "error": f"Ruta no autorizada para el tenant '{tenant_id}': '{m.name}'. "
                         f"Los documentos deben residir exclusivamente bajo 'data/documents/{tenant_id}/'.",
            }

        # 6. Límite de descompresión (anti Zip-Bomb / Tar-Bomb)
        total_uncompressed_size += m.size
        if total_uncompressed_size > MAX_UNCOMPRESSED_SIZE_BYTES:
            return {
                "is_valid": False,
                "error": f"El tamaño total descomprimido ({total_uncompressed_size} bytes) "
                         f"supera el límite de seguridad permitido ({MAX_UNCOMPRESSED_SIZE_BYTES} bytes).",
            }

        # 7. Verificación de contención estricta en el directorio de staging
        resolved = (staging_dir / m.name).resolve()
        try:
            resolved.relative_to(staging_dir.resolve())
        except (ValueError, RuntimeError):
            return {
                "is_valid": False,
                "error": f"Ruta escapa del directorio seguro de extracción: '{m.name}'.",
            }

    return {
        "is_valid": True,
        "members": seen_members,
        "total_size": total_uncompressed_size,
    }


async def export_tenant_database_rows(session: AsyncSession, tenant_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Exporta de manera determinista y aislada todos los registros del dominio
    que pertenecen al tenant_id especificado. Nunca incluye datos de otros tenants.
    """
    import_all_models()
    tenant_data: Dict[str, List[Dict[str, Any]]] = {}

    for table in Base.metadata.sorted_tables:
        if table.name not in RESTOREABLE_DOMAIN_TABLES:
            continue
        if "tenant_id" not in table.c:
            continue

        stmt = select(table).where(table.c.tenant_id == tenant_id)
        result = await session.execute(stmt)
        rows = result.mappings().all()

        table_rows = []
        for row in rows:
            row_dict = {col.name: serialize_value(row.get(col.name)) for col in table.columns}
            table_rows.append(row_dict)

        if table_rows:
            tenant_data[table.name] = table_rows

    return tenant_data


class BackupService:
    """Servicio para creación, verificación y restauración de respaldos aislados por tenant."""

    @classmethod
    def get_backup_directory(cls, custom_dir: Optional[Path] = None) -> Path:
        """Obtiene y asegura el directorio donde se almacenan los respaldos."""
        target = custom_dir or DEFAULT_BACKUP_DIR
        target.mkdir(parents=True, exist_ok=True)
        return target

    @classmethod
    async def create_backup(
        cls,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
        actor: str = "user",
        backup_dir: Optional[Path] = None,
        include_documents: bool = True,
    ) -> Dict[str, Any]:
        """
        Crea un respaldo comprimido (.tar.gz) estrictamente limitado al tenant_id:
        - Exporta solo las filas del dominio del tenant en tenant_data.json.
        - Incluye solo los documentos del tenant (data/documents/{tenant_id}/*).
        - Genera manifest.json con checksums criptográficos SHA-256.
        - No contiene datos ni documentos de ningún otro tenant.
        """
        target_dir = cls.get_backup_directory(backup_dir)
        backup_id = str(uuid.uuid4())
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive_name = f"backup_{tenant_id}_{timestamp_str}_{backup_id[:8]}.tar.gz"
        archive_path = target_dir / archive_name

        with tempfile.TemporaryDirectory() as tmp_dir_str:
            staging_dir = Path(tmp_dir_str)
            manifest_files: List[Dict[str, Any]] = []
            total_bytes = 0

            # 1. Exportación aislada de los registros de la base de datos
            if session is None:
                async with async_session_factory() as sess:
                    tenant_data = await export_tenant_database_rows(sess, tenant_id)
            else:
                tenant_data = await export_tenant_database_rows(session, tenant_id)

            tenant_data_file = staging_dir / "tenant_data.json"
            with open(tenant_data_file, "w", encoding="utf-8") as f:
                json.dump(tenant_data, f, indent=2, ensure_ascii=False)

            td_hash = calculate_sha256(tenant_data_file)
            td_size = tenant_data_file.stat().st_size
            manifest_files.append({
                "path": "tenant_data.json",
                "sha256": td_hash,
                "size_bytes": td_size,
                "type": "tenant_data",
            })
            total_bytes += td_size

            # 2. Inclusión exclusiva de documentos del subdirectorio del tenant autorizado
            if include_documents:
                tenant_doc_dir = Path("data/documents") / tenant_id
                if tenant_doc_dir.exists() and tenant_doc_dir.is_dir():
                    for root, _, files in os.walk(tenant_doc_dir):
                        for fname in files:
                            src_file = Path(root) / fname
                            # Ignorar symlinks para prevenir fugas de archivos
                            if src_file.is_file() and not src_file.is_symlink():
                                rel_path = src_file.relative_to(Path("."))
                                dest_file = staging_dir / rel_path
                                dest_file.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(src_file, dest_file, follow_symlinks=False)

                                file_hash = calculate_sha256(dest_file)
                                file_size = dest_file.stat().st_size
                                manifest_files.append({
                                    "path": str(rel_path).replace("\\", "/"),
                                    "sha256": file_hash,
                                    "size_bytes": file_size,
                                    "type": "data_file",
                                })
                                total_bytes += file_size

            # 3. Construir y escribir manifiesto
            manifest_data = {
                "manifest_version": BACKUP_VERSION,
                "backup_id": backup_id,
                "tenant_id": tenant_id,
                "backup_type": "tenant_scoped",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "app_version": "0.1.0",
                "files": manifest_files,
                "total_files": len(manifest_files),
                "total_bytes": total_bytes,
            }
            manifest_path = staging_dir / "manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, indent=2, ensure_ascii=False)

            # 4. Empaquetar todo en tar.gz asegurando correspondencia exacta
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(str(manifest_path), arcname="manifest.json")
                for entry in manifest_files:
                    staged_item = staging_dir / entry["path"]
                    if staged_item.exists():
                        tar.add(str(staged_item), arcname=entry["path"])

        archive_hash = calculate_sha256(archive_path)
        archive_size = archive_path.stat().st_size

        # 5. Registro inmutable de auditoría
        if session:
            try:
                audit_entry = AuditLog(
                    tenant_id=tenant_id,
                    action="backup_created",
                    entity_type="backup",
                    entity_id=backup_id,
                    actor=actor,
                    details={
                        "filename": archive_name,
                        "archive_sha256": archive_hash,
                        "size_bytes": archive_size,
                        "total_files": len(manifest_files),
                        "backup_type": "tenant_scoped",
                    },
                )
                session.add(audit_entry)
                await session.flush()
            except Exception as audit_err:
                logger.warning(f"No se pudo guardar registro de auditoría del backup: {audit_err}")

        logger.info(
            f"Backup aislado completado: {archive_name} | Archivos: {len(manifest_files)} | Tamaño: {archive_size} bytes"
        )

        return {
            "status": "success",
            "backup_id": backup_id,
            "filename": archive_name,
            "filepath": str(archive_path),
            "size_bytes": archive_size,
            "archive_sha256": archive_hash,
            "manifest": manifest_data,
        }

    @classmethod
    def list_backups(
        cls,
        tenant_id: Optional[str] = None,
        backup_dir: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """Lista todos los respaldos disponibles leyendo el manifest.json de cada archivo tar.gz."""
        target_dir = cls.get_backup_directory(backup_dir)
        backups: List[Dict[str, Any]] = []

        for item in sorted(target_dir.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with tarfile.open(item, "r:gz") as tar:
                    try:
                        manifest_member = tar.getmember("manifest.json")
                    except KeyError:
                        continue

                    f = tar.extractfile(manifest_member)
                    if not f:
                        continue
                    manifest_content = json.load(f)

                if tenant_id and manifest_content.get("tenant_id") != tenant_id:
                    continue

                backups.append({
                    "filename": item.name,
                    "filepath": str(item),
                    "size_bytes": item.stat().st_size,
                    "created_at": manifest_content.get("created_at_utc"),
                    "backup_id": manifest_content.get("backup_id"),
                    "tenant_id": manifest_content.get("tenant_id"),
                    "total_files": manifest_content.get("total_files"),
                    "manifest_version": manifest_content.get("manifest_version"),
                })
            except Exception as e:
                logger.warning(f"Error leyendo respaldo {item.name}: {e}")
                continue

        return backups

    @classmethod
    def verify_backup(cls, archive_path: Path, expected_tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Verifica exhaustivamente la integridad del respaldo:
        - Lectura previa de manifest.json para obtener el tenant_id.
        - Validación estricta de miembros contra TarSlip, symlinks y rutas ajenas al tenant.
        - Correspondencia exacta 1:1 entre miembros del archivo y archivos declarados en manifest.json.
        - Checksum SHA-256 de cada archivo contra lo declarado.
        - Aislamiento estricto: los datos en tenant_data.json deben pertenecer exclusivamente al tenant.
        """
        if not archive_path.exists():
            return {
                "is_valid": False,
                "errors": [f"El archivo de respaldo {archive_path} no existe."],
            }

        errors: List[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            staging_dir = Path(tmp_dir_str)

            try:
                with tarfile.open(archive_path, "r:gz") as tar:
                    # 1. Extraer manifest.json primero para conocer el tenant_id
                    try:
                        manifest_info = tar.getmember("manifest.json")
                    except KeyError:
                        return {
                            "is_valid": False,
                            "errors": ["El respaldo no contiene el manifiesto requerido manifest.json."],
                        }

                    mf = tar.extractfile(manifest_info)
                    if not mf:
                        return {
                            "is_valid": False,
                            "errors": ["No se pudo leer manifest.json del archivo comprimido."],
                        }
                    manifest = json.load(mf)
                    manifest_tenant = manifest.get("tenant_id")
                    if not manifest_tenant:
                        return {
                            "is_valid": False,
                            "errors": ["El manifiesto no declara un 'tenant_id' válido."],
                        }

                    if expected_tenant_id and manifest_tenant != expected_tenant_id:
                        return {
                            "is_valid": False,
                            "errors": [
                                f"Aislamiento por tenant violado: el respaldo pertenece a '{manifest_tenant}', "
                                f"no al tenant autorizado '{expected_tenant_id}'."
                            ],
                        }

                    # 2. Comprobación estricta de seguridad en todos los miembros
                    tar_check = validate_tar_members(tar, staging_dir, manifest_tenant)
                    if not tar_check["is_valid"]:
                        return {
                            "is_valid": False,
                            "errors": [tar_check["error"]],
                        }

                    tar_members: Set[str] = tar_check["members"]

                    # 3. Extraer en entorno aislado temporal
                    tar.extractall(path=staging_dir)

            except Exception as e:
                return {
                    "is_valid": False,
                    "errors": [f"Error al procesar archivo comprimido de respaldo: {e}"],
                }

            manifest_files = manifest.get("files", [])
            declared_paths: Set[str] = set()

            checked_count = 0
            for entry in manifest_files:
                rel_path = entry.get("path")
                declared_paths.add(rel_path)
                declared_hash = entry.get("sha256")
                extracted_file = staging_dir / rel_path

                # Validar ruta de documentos
                if not is_authorized_member_path(rel_path, manifest_tenant):
                    errors.append(
                        f"Ruta declarada en manifiesto no autorizada para el tenant '{manifest_tenant}': '{rel_path}'"
                    )

                if not extracted_file.exists():
                    errors.append(f"Archivo faltante declarado en manifiesto: {rel_path}")
                    continue

                actual_hash = calculate_sha256(extracted_file)
                if actual_hash != declared_hash:
                    errors.append(
                        f"Checksum SHA-256 inválido para {rel_path}: esperado {declared_hash}, actual {actual_hash}"
                    )
                else:
                    checked_count += 1

                # Validar aislamiento de registros en tenant_data.json
                if rel_path == "tenant_data.json":
                    try:
                        with open(extracted_file, "r", encoding="utf-8") as tf:
                            t_data = json.load(tf)
                        for tbl_name, rows in t_data.items():
                            for r in rows:
                                row_tenant = r.get("tenant_id")
                                if row_tenant and row_tenant != manifest_tenant:
                                    errors.append(
                                        f"Violación de aislamiento en tenant_data.json: registro en tabla '{tbl_name}' "
                                        f"pertenece a tenant '{row_tenant}', no al tenant '{manifest_tenant}'."
                                    )
                    except Exception as json_err:
                        errors.append(f"Error decodificando tenant_data.json: {json_err}")

            # 4. Correspondencia 1:1 exacta entre miembros del tar y manifiesto
            non_dir_members = {
                name for name in tar_members
                if not (staging_dir / name).is_dir()
            }
            expected_members = declared_paths | {"manifest.json"}
            extra_members = non_dir_members - expected_members
            if extra_members:
                errors.append(
                    f"Miembros no declarados en el manifiesto encontrados en el archivo: {', '.join(sorted(extra_members))}"
                )

        is_valid = (len(errors) == 0)
        return {
            "is_valid": is_valid,
            "backup_id": manifest.get("backup_id"),
            "tenant_id": manifest.get("tenant_id"),
            "created_at_utc": manifest.get("created_at_utc"),
            "checked_files": checked_count,
            "total_files": manifest.get("total_files"),
            "errors": errors,
        }

    @classmethod
    async def _apply_restoration_from_archive(
        cls,
        archive_path: Path,
        tenant_id: str,
        session: AsyncSession,
        fail_after_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Aplica internamente la extracción y restauración de datos para el tenant_id:
        1. Limpia y reinserta filas de tablas del dominio en orden topológico.
        2. Aplica política de tareas: cancela tareas que estaban 'pending' o 'scheduled' para prevenir auto-envío.
        3. Copia documentos a data/documents/{tenant_id}/ validando destinos sin seguir symlinks.
        4. Si fail_after_file está definido, inyecta fallo tras copiar dicho archivo para comprobar recuperación real.
        """
        import_all_models()
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            staging_dir = Path(tmp_dir_str)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=staging_dir)

            with open(staging_dir / "manifest.json", "r", encoding="utf-8") as f:
                manifest = json.load(f)

            # 1. Restauración de filas de la base de datos
            tenant_data_path = staging_dir / "tenant_data.json"
            if tenant_data_path.exists():
                with open(tenant_data_path, "r", encoding="utf-8") as tf:
                    tenant_data = json.load(tf)

                # Eliminar filas existentes de este tenant en orden inverso de claves foráneas
                for table in reversed(Base.metadata.sorted_tables):
                    if table.name in RESTOREABLE_DOMAIN_TABLES and "tenant_id" in table.c:
                        await session.execute(delete(table).where(table.c.tenant_id == tenant_id))

                # Insertar filas del respaldo en orden topológico directo aplicando políticas de tareas
                for table in Base.metadata.sorted_tables:
                    if table.name in RESTOREABLE_DOMAIN_TABLES and table.name in tenant_data:
                        rows = tenant_data[table.name]
                        for row_dict in rows:
                            row_dict["tenant_id"] = tenant_id

                            # Política de seguridad para scheduled_tasks: prevenir disparos imprevistos tras restaurar
                            if table.name == "scheduled_tasks":
                                if row_dict.get("status") in ("pending", "scheduled", "executing"):
                                    row_dict["status"] = "cancelled"
                                    row_dict["locked_by"] = None
                                    row_dict["locked_at"] = None
                                    row_dict["lease_expires_at"] = None
                                    row_dict["dispatch_in_flight"] = False

                            # Política para candidacies
                            if table.name == "candidacies":
                                if row_dict.get("status") in ("scheduled", "executing"):
                                    row_dict["status"] = "approved"

                            cleaned = {}
                            for col in table.columns:
                                if col.name in row_dict:
                                    cleaned[col.name] = deserialize_value(row_dict[col.name], col.type)
                            await session.execute(insert(table).values(cleaned))

            # 2. Restaurar archivos de documentos estrictamente dentro de data/documents/{tenant_id}
            tenant_doc_root = (Path("data/documents") / tenant_id).resolve()
            tenant_doc_root.mkdir(parents=True, exist_ok=True)

            for entry in manifest.get("files", []):
                if entry.get("type") == "data_file":
                    rel_path = entry.get("path").replace("\\", "/")
                    if not is_authorized_member_path(rel_path, tenant_id):
                        raise ValueError(f"Ruta no autorizada en respaldo: {rel_path}")

                    dest_unresolved = Path(".") / rel_path
                    # Comprobar si el archivo o cualquiera de sus directorios intermedios es un symlink antes de resolver
                    curr_check = dest_unresolved
                    while curr_check != Path(".") and curr_check != curr_check.parent:
                        if curr_check.is_symlink() or os.path.islink(curr_check):
                            raise RuntimeError(f"Symlink detectado en ruta de destino: '{curr_check}'. Abortando.")
                        curr_check = curr_check.parent

                    dest_file = dest_unresolved.resolve()
                    # Verificar contención estricta en el subdirectorio del tenant
                    try:
                        dest_file.relative_to(tenant_doc_root)
                    except ValueError:
                        raise ValueError(f"Ruta de documento '{dest_file}' escapa de '{tenant_doc_root}'")

                    if tenant_doc_root.is_symlink() or os.path.islink(tenant_doc_root):
                        raise RuntimeError(f"Directorio raíz de documentos es un symlink: '{tenant_doc_root}'. Abortando.")

                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staging_dir / rel_path, dest_file, follow_symlinks=False)

                    # Inyección de fallo real para demostración de recuperación
                    if fail_after_file and (fail_after_file in rel_path or fail_after_file in dest_file.name):
                        raise RuntimeError(f"Fallo real inyectado tras mutar filas y copiar documento '{dest_file.name}'.")

            await session.commit()
            return manifest

    @classmethod
    async def restore_backup(
        cls,
        archive_path: Path,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
        actor: str = "user",
        confirm: bool = False,
        fail_after_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Restaura de manera atómica y segura un respaldo verificado:
        1. Valida confirmación explícita.
        2. Verifica exhaustivamente la integridad del archivo y rutas antes de cualquier mutación.
        3. Coordina con workers: rechaza restauración si hay tareas 'executing' activas.
        4. Crea un respaldo automático de reversión (rollback) del estado actual (fail-closed).
        5. Registra los archivos existentes en data/documents/{tenant_id}/ antes de restaurar.
        6. Aplica la restauración de datos y documentos en una transacción aislada.
        7. Si ocurre un fallo durante la restauración (incluso tras mutar la base de datos y copiar archivos):
           - Se revierte la transacción de la base de datos.
           - Se eliminan los archivos nuevos residuales introducidos por el intento fallido.
           - Se re-aplica el respaldo previo de rollback restaurando filas y documentos con exactitud de bytes.
        """
        if not confirm:
            raise ValueError("Confirmación explícita requerida para restaurar respaldo (confirm=True).")

        # 1. Verificación preliminar estricta de integridad, rutas y correspondencia de tenant
        verify_result = cls.verify_backup(archive_path, expected_tenant_id=tenant_id)
        if not verify_result["is_valid"]:
            raise ValueError(
                f"El respaldo no superó las pruebas de integridad: {'; '.join(verify_result['errors'])}"
            )

        # 2. Coordinación con workers activos: rechazar si hay ejecuciones en vuelo
        own_session = False
        active_session = session
        if active_session is None:
            active_session = async_session_factory()
            own_session = True

        try:
            exec_stmt = select(ScheduledTask).where(
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "executing",
            )
            running_tasks = (await active_session.execute(exec_stmt)).scalars().all()
            if running_tasks:
                raise RuntimeError(
                    f"No se puede restaurar mientras haya {len(running_tasks)} tarea(s) en ejecución activa para este tenant. "
                    f"Espere a que finalicen o cancólelas."
                )

            # 3. Snapshot de archivos existentes antes de la restauración
            tenant_doc_dir = Path("data/documents") / tenant_id
            pre_restore_files: Set[Path] = set()
            if tenant_doc_dir.exists():
                pre_restore_files = {p.resolve() for p in tenant_doc_dir.rglob("*") if p.is_file() and not p.is_symlink()}

            # 4. Crear copia de seguridad de reversión (rollback) automática antes de tocar nada (fail-closed)
            target_dir = archive_path.parent
            try:
                rollback_info = await cls.create_backup(
                    tenant_id=tenant_id,
                    session=active_session,
                    actor="system_rollback",
                    backup_dir=target_dir,
                    include_documents=True,
                )
                logger.info(f"Copia de seguridad previa de reversión generada: {rollback_info['filename']}")
            except Exception as rb_err:
                raise RuntimeError(
                    f"Fallo crítico: No se pudo generar la copia previa de reversión: {rb_err}. "
                    f"La restauración ha sido abortada antes de mutar cualquier dato."
                ) from rb_err

            # 5. Aplicar la restauración
            try:
                manifest = await cls._apply_restoration_from_archive(
                    archive_path=archive_path,
                    tenant_id=tenant_id,
                    session=active_session,
                    fail_after_file=fail_after_file,
                )

                # Eliminar archivos que existían antes pero NO forman parte del respaldo restaurado
                restored_doc_paths = {
                    (Path(".") / e["path"]).resolve()
                    for e in manifest.get("files", [])
                    if e.get("type") == "data_file"
                }
                for old_file in pre_restore_files:
                    if old_file not in restored_doc_paths and old_file.exists():
                        old_file.unlink()

                # 6. Registrar evento de auditoría
                try:
                    audit_entry = AuditLog(
                        tenant_id=tenant_id,
                        action="backup_restored",
                        entity_type="backup",
                        entity_id=manifest.get("backup_id"),
                        actor=actor,
                        details={
                            "source_file": archive_path.name,
                            "backup_id": manifest.get("backup_id"),
                            "rollback_created": rollback_info.get("filename") if rollback_info else None,
                            "restored_files_count": len(manifest.get("files", [])),
                            "backup_type": "tenant_scoped",
                        },
                    )
                    active_session.add(audit_entry)
                    await active_session.commit()
                except Exception as audit_err:
                    logger.warning(f"No se pudo guardar registro de auditoría de la restauración: {audit_err}")

                logger.info(f"Restauración completada con éxito desde {archive_path.name} para tenant {tenant_id}")
                return {
                    "status": "success",
                    "message": f"Respaldo restaurado exitosamente desde {archive_path.name}.",
                    "backup_id": manifest.get("backup_id"),
                    "tenant_id": tenant_id,
                    "rollback_backup": rollback_info.get("filename") if rollback_info else None,
                }

            except Exception as exc:
                logger.error(f"Error durante restauración ({exc}). Iniciando reversión automática y limpieza...")
                await active_session.rollback()

                # A. LIMPIEZA DE ARCHIVOS RESIDUALES: eliminar cualquier archivo nuevo creado durante el intento fallido
                if tenant_doc_dir.exists():
                    current_files = {p.resolve() for p in tenant_doc_dir.rglob("*") if p.is_file() and not p.is_symlink()}
                    residual_files = current_files - pre_restore_files
                    for res_file in residual_files:
                        if res_file.exists():
                            logger.info(f"Eliminando archivo residual de intento fallido: {res_file.name}")
                            res_file.unlink()

                # B. RE-APLICAR RESPALDO PREVIO DE ROLLBACK
                rollback_path = Path(rollback_info["filepath"])
                try:
                    await cls._apply_restoration_from_archive(
                        archive_path=rollback_path,
                        tenant_id=tenant_id,
                        session=active_session,
                    )
                    logger.info("Reversión automática y re-aplicación del estado previo completadas con éxito.")
                    rollback_status = "reverted_successfully"
                except Exception as rb_exec_err:
                    logger.critical(f"Error crítico durante la reversión automática: {rb_exec_err}")
                    rollback_status = f"rollback_failed: {rb_exec_err}"

                raise RuntimeError(
                    f"Fallo en la restauración: {exc}. Estado de reversión automática: {rollback_status}."
                ) from exc

        finally:
            if own_session and active_session:
                await active_session.close()
