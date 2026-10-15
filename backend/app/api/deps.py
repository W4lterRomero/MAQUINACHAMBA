"""
Dependencias compartidas para la API FastAPI.
Garantiza autenticación obligatoria y aislamiento estricto por tenant_id
resuelto del lado servidor.
"""

from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_session
from backend.app.core.security import decode_access_token
from backend.app.modules.users.models import User

# HTTPBearer sin forzar error 403 por defecto para controlar respuestas 401 estructuradas
security_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """
    Resuelve la identidad del usuario a partir del token Bearer JWT.
    Valida firma, expiración y existencia activa en la base de datos.
    Falla cerrado ante cualquier anomalía.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales de autenticación no proporcionadas.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        user_id: Optional[str] = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token de acceso inválido: sujeto no identificado.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de acceso inválido o expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Consultar usuario activo en la base de datos del servidor
    stmt = select(User).where(User.id == user_id, User.is_active.is_(True))
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_tenant_id(
    current_user: User = Depends(get_current_user),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> str:
    """
    Resuelve el tenant_id de forma estrictamente vinculada a la identidad
    autenticada del lado del servidor.

    Si el cliente incluye la cabecera opcional X-Tenant-ID:
    - Si coincide con current_user.tenant_id, se autoriza.
    - Si difiere, se rechaza inmediatamente con 403 Forbidden para
      impedir cualquier intento de suplantación o fuga entre tenants.
    """
    if x_tenant_id is not None and x_tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Acceso denegado: el usuario no tiene permisos sobre el tenant '{x_tenant_id}'. "
                f"Ámbito autorizado: '{current_user.tenant_id}'."
            ),
        )

    return current_user.tenant_id
