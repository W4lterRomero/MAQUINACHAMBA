"""
Router para gestión de copias de seguridad (Backups) y restauración (REQ-033, D1).
Garantiza aislamiento por tenant_id, verificación estricta de integridad y prevención de path traversal.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.audit.backup_service import BackupService, DEFAULT_BACKUP_DIR
from backend.app.modules.audit.models import AuditLog

router = APIRouter(prefix="/backups", tags=["Backups & Restore"])


class CreateBackupRequest(BaseModel):
    include_documents: bool = Field(default=True, description="Incluir archivos y documentos locales en el respaldo")


class VerifyBackupRequest(BaseModel):
    filename: str = Field(..., description="Nombre del archivo de respaldo en el directorio backups/")


class RestoreBackupRequest(BaseModel):
    filename: str = Field(..., description="Nombre del archivo de respaldo a restaurar")
    confirm: bool = Field(..., description="Confirmación explícita para proceder con la restauración")


def resolve_safe_backup_path(filename: str) -> Path:
    """Previene ataques de path traversal asegurando que el archivo resida en DEFAULT_BACKUP_DIR."""
    if os.path.isabs(filename) or ".." in filename.split("/") or "\\" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nombre de archivo inválido: no se permiten rutas absolutas ni secuencias de escape.",
        )
    candidate = (DEFAULT_BACKUP_DIR / filename).resolve()
    try:
        candidate.relative_to(DEFAULT_BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ruta de archivo no autorizada fuera del directorio de respaldos.",
        )
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El archivo de respaldo '{filename}' no fue encontrado.",
        )
    return candidate


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_backup(
    payload: Optional[CreateBackupRequest] = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Crea un respaldo comprimido (.tar.gz) y aislado de los datos y documentos del usuario.
    Genera sumas SHA-256 en manifest.json sin incluir datos de otros tenants.
    """
    include_docs = payload.include_documents if payload else True
    try:
        result = await BackupService.create_backup(
            tenant_id=tenant_id,
            session=session,
            actor="user",
            include_documents=include_docs,
        )
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo al generar respaldo: {str(exc)}",
        )


@router.get("", response_model=List[Dict[str, Any]])
async def list_backups(
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Lista todos los respaldos asociados al tenant autenticado, ordenados cronológicamente.
    """
    return BackupService.list_backups(tenant_id=tenant_id)


@router.post("/verify")
async def verify_backup(
    payload: VerifyBackupRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Verifica la integridad de un archivo de respaldo:
    - Comprueba sumas SHA-256 de cada archivo en el manifiesto.
    - Mitiga riesgos de TarSlip, traversal y miembros duplicados o extraños.
    - Valida correspondencia 1:1 entre miembros y manifiesto.
    - Verifica que los datos pertenezcan exclusivamente al tenant.
    """
    archive_path = resolve_safe_backup_path(payload.filename)
    result = BackupService.verify_backup(archive_path)

    # Validar correspondencia con el tenant autenticado si el archivo es válido
    if result["is_valid"] and result.get("tenant_id") != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"El respaldo pertenece a otro tenant ('{result.get('tenant_id')}'). Acceso denegado.",
        )

    return result


@router.post("/restore", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
async def restore_backup(
    payload: RestoreBackupRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    La restauración desde la API ordinaria se encuentra temporalmente deshabilitada
    por revisión de seguridad y aislamiento multi-tenant.
    """
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "La restauración desde la API ordinaria y la interfaz web se encuentra deshabilitada temporalmente "
            "mientras se completa la revisión de seguridad y aislamiento estricto por tenant. "
            "Utilice el servicio interno o scripts administrativos CLI autorizados."
        ),
    )


@router.get("/audit-logs")
async def list_backup_audit_logs(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Retorna el historial inmutable de auditoría para operaciones de respaldo y restauración.
    """
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.entity_type == "backup",
        )
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    res = await session.execute(stmt)
    records = res.scalars().all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "actor": r.actor,
            "details": r.details,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]
