"""
Router API para Configuración de IA, BYOK y Control de Costos (ADR-004, REQ-029).
Permite:
- Selección de proveedor (OpenAI, Anthropic, Google) y modelo configurable.
- Gestión segura de claves de API (almacenadas cifradas en vault 0600; nunca expuestas).
- Control de presupuesto mensual por usuario/tenant en USD.
- Registro de consumo y verificación en modo simulación.
- Ejecución simulada con fallback determinista transparente al agotar presupuesto.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.secrets.models import APIKeyConfig, CostLog
from backend.app.modules.secrets.service import SecretsVaultService
from backend.app.modules.secrets.ai_service import (
    AIService,
    PROVIDERS_CATALOG,
    SIMULATION_BANNER_TEXT,
)

router = APIRouter(prefix="/ai", tags=["AI Configuration & BYOK"])
vault_service = SecretsVaultService()
ai_service = AIService(vault_service=vault_service)


class AIConfigUpdateRequest(BaseModel):
    provider: str = Field(default="openai", description="Proveedor (openai, anthropic, google)")
    model: Optional[str] = Field(default=None, description="Modelo específico dentro del catálogo")
    monthly_budget_usd: Optional[float] = Field(default=None, ge=0.0, le=1000.0, description="Límite mensual en USD")
    is_active: Optional[bool] = Field(default=None, description="Habilitar o pausar uso de IA")
    max_tokens_per_request: Optional[int] = Field(default=None, ge=100, le=32000)
    max_concurrent_requests: Optional[int] = Field(default=None, ge=1, le=10)


class KeyStoreRequest(BaseModel):
    provider: str = Field(..., description="Proveedor para el que se registra la clave")
    key_value: str = Field(..., min_length=8, description="Clave secreta (se cifrará inmediatamente)")


class SimulateCallRequest(BaseModel):
    provider: Optional[str] = Field(default="openai")
    operation: str = Field(default="evaluation", description="Operación (evaluation, cv_adaptation, cover_letter)")
    prompt: str = Field(default="Evaluar perfil contra vacante de software engineer")
    expected_output_tokens: Optional[int] = Field(default=200, ge=10, le=4096)
    force_fallback: Optional[bool] = Field(default=False)


@router.get("/catalog")
async def get_ai_catalog():
    """Retorna los proveedores y modelos disponibles con especificaciones y tarifas estimadas."""
    return {
        "providers": PROVIDERS_CATALOG,
        "is_simulation": True,
        "simulation_banner": SIMULATION_BANNER_TEXT,
    }


@router.get("/config")
async def get_ai_config(
    provider: str = Query("openai", description="Proveedor consultado"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Obtiene el estado de configuración, saldo presupuestario y máscara de clave.
    NUNCA devuelve claves en texto plano.
    """
    config = await ai_service.get_or_create_config(session, tenant_id, provider)
    has_key = vault_service.has_key(tenant_id, provider)

    committed = round(config.current_month_usage_usd, 4)
    reserved = round(config.reserved_budget_usd, 4)
    total_consumed = round(committed + reserved, 4)
    remaining = max(0.0, round(config.monthly_budget_usd - total_consumed, 4))

    # Determinar nivel de alerta
    if total_consumed >= config.monthly_budget_usd:
        budget_status = "exhausted"
    elif config.monthly_budget_usd > 0 and (total_consumed / config.monthly_budget_usd) >= 0.8:
        budget_status = "warning"
    else:
        budget_status = "ok"

    return {
        "tenant_id": tenant_id,
        "provider": config.provider,
        "model": config.model,
        "monthly_budget_usd": config.monthly_budget_usd,
        "current_month_usage_usd": committed,
        "reserved_budget_usd": reserved,
        "remaining_budget_usd": remaining,
        "budget_status": budget_status,
        "is_active": config.is_active,
        "max_tokens_per_request": config.max_tokens_per_request,
        "max_concurrent_requests": config.max_concurrent_requests,
        "has_key": has_key,
        "key_preview": config.key_preview if has_key else None,
        "is_simulation": True,
        "simulation_banner": SIMULATION_BANNER_TEXT,
    }


@router.post("/config")
async def update_ai_config(
    body: AIConfigUpdateRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """Actualiza la configuración del modelo, presupuesto y parámetros operativos."""
    prov = body.provider.lower().strip()
    if prov not in PROVIDERS_CATALOG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Proveedor '{body.provider}' no soportado. Opciones: {list(PROVIDERS_CATALOG.keys())}",
        )

    config = await ai_service.get_or_create_config(session, tenant_id, prov)

    if body.model is not None:
        valid_models = PROVIDERS_CATALOG[prov]["models"]
        if body.model not in valid_models:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Modelo '{body.model}' no reconocido para {prov}. Opciones: {list(valid_models.keys())}",
            )
        config.model = body.model

    if body.monthly_budget_usd is not None:
        config.monthly_budget_usd = round(body.monthly_budget_usd, 4)

    if body.is_active is not None:
        config.is_active = body.is_active

    if body.max_tokens_per_request is not None:
        config.max_tokens_per_request = body.max_tokens_per_request

    if body.max_concurrent_requests is not None:
        config.max_concurrent_requests = body.max_concurrent_requests

    session.add(config)
    await session.commit()
    await session.refresh(config)

    return {
        "status": "updated",
        "tenant_id": tenant_id,
        "provider": config.provider,
        "model": config.model,
        "monthly_budget_usd": config.monthly_budget_usd,
        "is_active": config.is_active,
        "is_simulation": True,
    }


@router.post("/key")
async def store_provider_key(
    body: KeyStoreRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Cifra y almacena la clave en el vault local (0600) del usuario.
    NUNCA almacena la clave en la base de datos ni la retorna en la respuesta.
    """
    prov = body.provider.lower().strip()
    if prov not in PROVIDERS_CATALOG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Proveedor '{body.provider}' no soportado.",
        )

    ref = vault_service.store_key(tenant_id, prov, body.key_value)
    preview = vault_service.mask_key(body.key_value)

    config = await ai_service.get_or_create_config(session, tenant_id, prov)
    config.key_reference = ref
    config.key_preview = preview
    session.add(config)
    await session.commit()

    return {
        "status": "stored",
        "provider": prov,
        "has_key": True,
        "key_preview": preview,
        "key_reference": ref,
        "message": "Clave cifrada con éxito en vault seguro (permisos 0600).",
        "is_simulation": True,
    }


@router.delete("/key")
async def delete_provider_key(
    provider: str = Query(..., description="Proveedor de la clave a eliminar"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Elimina y sobrescribe de forma segura la clave del vault local.
    """
    prov = provider.lower().strip()
    deleted = vault_service.delete_key(tenant_id, prov)

    config = await ai_service.get_or_create_config(session, tenant_id, prov)
    config.key_reference = None
    config.key_preview = None
    session.add(config)
    await session.commit()

    return {
        "status": "deleted",
        "provider": prov,
        "has_key": False,
        "message": "Clave eliminada de forma segura del almacenamiento cifrado.",
        "is_simulation": True,
    }


@router.post("/simulate-call")
async def simulate_ai_call(
    body: SimulateCallRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Simula una llamada de IA con verificación y reserva atómica de presupuesto.
    Si el presupuesto está agotado, ejecuta automáticamente la alternativa determinista.
    """
    prov = (body.provider or "openai").lower().strip()
    result = await ai_service.execute_simulated_call(
        session=session,
        tenant_id=tenant_id,
        provider=prov,
        operation=body.operation,
        prompt_text=body.prompt,
        expected_output_tokens=body.expected_output_tokens or 200,
        force_fallback=bool(body.force_fallback),
    )
    return result


@router.get("/cost-logs")
async def get_cost_logs(
    limit: int = Query(50, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """Retorna el historial de consumo estimado y simulado para el tenant."""
    stmt = (
        select(CostLog)
        .where(CostLog.tenant_id == tenant_id)
        .order_by(desc(CostLog.created_at))
        .limit(limit)
    )
    res = await session.execute(stmt)
    logs = res.scalars().all()

    return {
        "tenant_id": tenant_id,
        "total_records": len(logs),
        "is_simulation": True,
        "simulation_banner": SIMULATION_BANNER_TEXT,
        "logs": [
            {
                "id": l.id,
                "provider": l.provider,
                "model_used": l.model_used,
                "operation": l.operation,
                "input_tokens": l.input_tokens,
                "output_tokens": l.output_tokens,
                "estimated_cost_usd": l.estimated_cost_usd,
                "is_simulation": l.is_simulation,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }
