"""
Router de Gestión de Portales, Autorizaciones y Automatización de Navegador (REQ-024, REQ-025).
Permite administrar autorizaciones persistidas por portal/acción y previsualizar llenado web con detención segura.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.config import get_settings
from backend.app.modules.portals.service import PortalAuthorizationService
from backend.app.modules.portals.adapters.browser_adapter import BrowserPortalAdapter

router = APIRouter(prefix="/portals", tags=["Portals & Browser Automation"])


class GrantAuthorizationRequest(BaseModel):
    portal_name: str = Field(..., min_length=2, max_length=100)
    action: str = Field(..., description="Acción autorizada: read_email, search_jobs, register_account, submit_application")
    authorized_by: Optional[str] = "user"
    notes: Optional[str] = None
    expires_at: Optional[Any] = None


class BrowserPreviewRequest(BaseModel):
    target_url: str = Field(..., description="URL del formulario o vacante")
    full_name: Optional[str] = "Candidato Verificado"
    email: Optional[str] = "candidato@test.org"
    phone: Optional[str] = "+503 7000-0000"
    cover_letter: Optional[str] = "Carta de presentación de prueba."


@router.get("/authorizations")
async def list_authorizations(
    portal_name: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista las autorizaciones persistidas para el tenant."""
    auths = await PortalAuthorizationService.list_authorizations(
        session=session,
        tenant_id=tenant_id,
        portal_name=portal_name,
        action=action,
    )
    return [
        {
            "id": a.id,
            "portal_name": a.portal_name,
            "action": a.action,
            "authorized": a.authorized,
            "authorized_by": a.authorized_by,
            "notes": a.notes,
            "is_active": a.is_active(),
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
        }
        for a in auths
    ]


@router.post("/authorizations", status_code=status.HTTP_201_CREATED)
async def grant_authorization(
    payload: GrantAuthorizationRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Otorga una autorización persistida específica por portal y acción.
    REQ-025: Separa estrictamente búsqueda de ofertas y lectura de email de creación de cuenta o envío.
    """
    auth = await PortalAuthorizationService.grant_authorization(
        session=session,
        tenant_id=tenant_id,
        portal_name=payload.portal_name,
        action=payload.action,
        authorized_by=payload.authorized_by or "user",
        notes=payload.notes,
        expires_at=payload.expires_at,
    )
    return {
        "status": "granted",
        "authorization_id": auth.id,
        "portal_name": auth.portal_name,
        "action": auth.action,
        "is_active": auth.is_active(),
    }


@router.delete("/authorizations/{auth_id}")
async def revoke_authorization(
    auth_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Revoca una autorización persistida existente."""
    revoked = await PortalAuthorizationService.revoke_authorization(
        session=session,
        tenant_id=tenant_id,
        auth_id=auth_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Autorización no encontrada.")
    await session.commit()
    return {"status": "revoked", "authorization_id": auth_id}


@router.post("/browser/preview", status_code=status.HTTP_200_OK)
async def preview_browser_form_filling(
    payload: BrowserPreviewRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Ejecuta el agente de navegador en modo simulación:
    Navega a la URL indicada, rellena los campos detectados, enmascara PII en el DOM,
    toma captura de pantalla de evidencia y SE DETIENE estrictamente antes del botón de envío.
    """
    adapter = BrowserPortalAdapter(simulation_mode=True)
    res = await adapter.submit_application(
        tenant_id=tenant_id,
        target_url=payload.target_url,
        applicant_data={"email": payload.email, "name": payload.full_name},
        cover_letter=payload.cover_letter or "",
        cv_payload={
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
        },
        actions=[],
    )
    return {
        "success": res.success,
        "result_type": res.result_type,
        "status_code": res.status_code,
        "evidence": res.evidence,
        "error_message": res.error_message,
    }
