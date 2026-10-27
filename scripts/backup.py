#!/usr/bin/env python3
"""
Herramienta de línea de comandos (CLI) para gestión de copias de seguridad y restauración (REQ-033, D1).
Uso:
    python scripts/backup.py create --tenant-id <ID> [--no-docs]
    python scripts/backup.py list [--tenant-id <ID>]
    python scripts/backup.py verify --file <archivo.tar.gz>
    python scripts/backup.py restore --file <archivo.tar.gz> --tenant-id <ID> --confirm
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Agregar directorio raíz al PYTHONPATH si no está
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.modules.audit.backup_service import BackupService, DEFAULT_BACKUP_DIR


def format_bytes(size: int) -> str:
    """Convierte bytes a formato legible."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


async def cmd_create(args):
    print(f"📦 Generando copia de seguridad para el tenant '{args.tenant_id}'...")
    try:
        result = await BackupService.create_backup(
            tenant_id=args.tenant_id,
            actor="cli",
            include_documents=not args.no_docs,
        )
        print(f"✅ Respaldo creado exitosamente:")
        print(f"   Archivo:  {result['filename']}")
        print(f"   Ruta:     {result['filepath']}")
        print(f"   Tamaño:   {format_bytes(result['size_bytes'])}")
        print(f"   SHA-256:  {result['archive_sha256']}")
        print(f"   Archivos: {result['manifest']['total_files']}")
    except Exception as e:
        print(f"❌ Error al crear respaldo: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    backups = BackupService.list_backups(tenant_id=args.tenant_id)
    if not backups:
        print("ℹ️ No se encontraron respaldos disponibles.")
        return

    print(f"\n📂 Respaldos disponibles ({len(backups)}):")
    print("-" * 105)
    print(f"{'Archivo':<60} {'Fecha (UTC)':<20} {'Tamaño':<10} {'Archivos':<8}")
    print("-" * 105)
    for b in backups:
        fname = b["filename"]
        created = b.get("created_at", "N/A")[:19].replace("T", " ")
        sz = format_bytes(b["size_bytes"])
        cnt = str(b.get("total_files", "N/A"))
        print(f"{fname:<60} {created:<20} {sz:<10} {cnt:<8}")
    print("-" * 105 + "\n")


def cmd_verify(args):
    file_path = Path(args.file)
    if not file_path.exists():
        file_path = DEFAULT_BACKUP_DIR / args.file
    if not file_path.exists():
        print(f"❌ Archivo no encontrado: {args.file}", file=sys.stderr)
        sys.exit(1)

    print(f"🔍 Verificando integridad de '{file_path.name}'...")
    result = BackupService.verify_backup(file_path)
    if result["is_valid"]:
        print(f"✅ El respaldo es completamente íntegro y válido:")
        print(f"   Backup ID:        {result.get('backup_id')}")
        print(f"   Tenant:           {result.get('tenant_id')}")
        print(f"   Fecha creación:   {result.get('created_at_utc')}")
        print(f"   Archivos válidos: {result.get('checked_files')} / {result.get('total_files')}")
        print(f"   Comprobación DB:  PRAGMA integrity_check OK")
    else:
        print(f"❌ Falló la verificación de integridad:")
        for err in result.get("errors", []):
            print(f"   - {err}")
        sys.exit(2)


async def cmd_restore(args):
    if not args.confirm:
        print("❌ Operación abortada: Debe especificar el flag --confirm para autorizar la restauración.", file=sys.stderr)
        sys.exit(1)

    file_path = Path(args.file)
    if not file_path.exists():
        file_path = DEFAULT_BACKUP_DIR / args.file
    if not file_path.exists():
        print(f"❌ Archivo no encontrado: {args.file}", file=sys.stderr)
        sys.exit(1)

    print(f"⚠️ Restaurando sistema desde '{file_path.name}' para el tenant '{args.tenant_id}'...")
    try:
        result = await BackupService.restore_backup(
            archive_path=file_path,
            tenant_id=args.tenant_id,
            actor="cli",
            confirm=args.confirm,
        )
        print(f"✅ Restauración completada con éxito:")
        print(f"   Tenant:          {result.get('tenant_id')}")
        print(f"   Backup ID:       {result.get('backup_id')}")
        print(f"   Copia reversión: {result.get('rollback_backup', 'Ninguna')}")
    except Exception as e:
        print(f"❌ Error al restaurar: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Herramienta de Backups y Restauración para Máquina de Chamba (REQ-033)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Crear nuevo respaldo")
    p_create.add_argument("--tenant-id", required=True, help="ID del tenant")
    p_create.add_argument("--no-docs", action="store_true", help="Omitir documentos y respaldar solo la BD")

    # list
    p_list = subparsers.add_parser("list", help="Listar respaldos")
    p_list.add_argument("--tenant-id", help="Filtrar por ID de tenant")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verificar integridad de un respaldo")
    p_verify.add_argument("--file", required=True, help="Ruta o nombre del archivo .tar.gz")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restaurar estado a partir de un respaldo")
    p_restore.add_argument("--file", required=True, help="Ruta o nombre del archivo .tar.gz")
    p_restore.add_argument("--tenant-id", required=True, help="ID del tenant autorizado")
    p_restore.add_argument("--confirm", action="store_true", help="Confirmar restauración destructiva")

    args = parser.parse_args()

    if args.command == "create":
        asyncio.run(cmd_create(args))
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "verify":
        cmd_verify(args)
    elif args.command == "restore":
        asyncio.run(cmd_restore(args))


if __name__ == "__main__":
    main()
