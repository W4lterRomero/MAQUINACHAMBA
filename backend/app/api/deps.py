"""
Dependencias compartidas para la API FastAPI.
Garantiza aislamiento por tenant_id en todas las peticiones.
"""

from typing import Optional
from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.core.database import get_session

DEFAULT_LOCAL_TENANT_ID = "default-tenant-local"


async def get_tenant_id(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID")
) -> str:
    """
    Extrae el tenant_id de la cabecera X-Tenant-ID.
    Si no se proporciona en entorno local, usa el tenant por defecto.
    """
    return x_tenant_id if x_tenant_id else DEFAULT_LOCAL_TENANT_ID
