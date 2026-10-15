"""
Router de autenticación y gestión de sesiones (Fase 1 - Auth).
Provee endpoints de registro, inicio de sesión e inspección de identidad.
"""

import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_session
from backend.app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from backend.app.api.deps import get_current_user
from backend.app.modules.users.models import Tenant, User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8)
    full_name: str = Field(default="", max_length=255)
    tenant_id: Optional[str] = Field(
        default=None,
        description="Parámetro prohibido en registro público. La incorporación a un tenant existente requiere invitación.",
    )


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str



class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    tenant_id: str
    full_name: str


class UserMeResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    full_name: str
    role: str
    is_active: bool


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Registra un nuevo usuario en un nuevo ámbito (tenant) generado exclusivamente por el servidor.
    El registro público rechaza explícitamente cualquier tenant_id para prevenir accesos no autorizados a tenants ajenos.
    """
    # REQ: P1 Dictamen Work sobre 0473d01 - Rechazar explícitamente tenant_id en registro público
    if request.tenant_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "El registro público no permite especificar tenant_id. "
                "Cada usuario recibe un ámbito nuevo generado por el servidor. "
                "La incorporación a un ámbito existente requiere invitación verificable o autorización administrativa."
            ),
        )

    # Verificar si el correo ya existe
    existing_user = await session.execute(
        select(User).where(User.email == request.email)
    )
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ya existe una cuenta con este correo electrónico.",
        )

    # Ámbito nuevo generado exclusivamente por el servidor
    assigned_tenant_id = f"tenant-{uuid.uuid4().hex[:12]}"

    tenant = Tenant(
        id=assigned_tenant_id,
        tenant_id=assigned_tenant_id,
        name=f"Tenant de {request.email}",
    )
    session.add(tenant)
    await session.flush()

    new_user = User(
        id=str(uuid.uuid4()),
        tenant_id=assigned_tenant_id,
        email=request.email,
        hashed_password=hash_password(request.password),
        full_name=request.full_name,
        is_active=True,
        role="user",
    )
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)

    token = create_access_token(
        data={"sub": new_user.id, "email": new_user.email, "tenant_id": new_user.tenant_id}
    )

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user_id=new_user.id,
        email=new_user.email,
        tenant_id=new_user.tenant_id,
        full_name=new_user.full_name,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Inicia sesión verificando credenciales del lado servidor y retornando JWT firmado.
    """
    stmt = select(User).where(User.email == request.email)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo o suspendido.",
        )

    token = create_access_token(
        data={"sub": user.id, "email": user.email, "tenant_id": user.tenant_id}
    )

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user_id=user.id,
        email=user.email,
        tenant_id=user.tenant_id,
        full_name=user.full_name,
    )


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
):
    """
    Retorna la información del usuario autenticado y su tenant autorizado.
    """
    return UserMeResponse(
        user_id=current_user.id,
        email=current_user.email,
        tenant_id=current_user.tenant_id,
        full_name=current_user.full_name,
        role=current_user.role,
        is_active=current_user.is_active,
    )

