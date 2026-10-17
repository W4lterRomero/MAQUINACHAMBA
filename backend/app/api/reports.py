"""
Router para métricas de dashboard y reportes de correo consolidado (REQ-028, D8).
Permite visualizar KPIs de búsqueda laboral y generar/enviar resúmenes periódicos.
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.email_mod.service import EmailReportService

router = APIRouter(prefix="/reports", tags=["Reports & Dashboard"])


class SendReportRequest(BaseModel):
    recipient_email: Optional[str] = None


@router.get("/dashboard-metrics")
async def get_dashboard_metrics(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Obtiene las métricas consolidadas del dashboard de búsqueda laboral:
    - Estado y desglose de vacantes (activas, sospechosas, cerradas).
    - Métricas de candidaturas (pendientes, aprobadas, enviadas, intervención).
    - Despachos programados para la ventana del día siguiente.
    - Habilidades verificadas vs pendientes de revisión humana.
    - Saldo y consumo del presupuesto de IA (BYOK).
    """
    metrics = await EmailReportService.get_dashboard_metrics(session=session, tenant_id=tenant_id)
    return metrics


@router.get("/daily-email/preview")
async def preview_daily_report(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Genera una vista previa del reporte diario en texto plano y HTML
    sin realizar ningún envío externo.
    """
    report = await EmailReportService.build_daily_report(session=session, tenant_id=tenant_id)
    return report


@router.post("/daily-email/send", status_code=status.HTTP_200_OK)
async def send_daily_report(
    payload: Optional[SendReportRequest] = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Envía el reporte diario consolidado por correo electrónico.
    En SIMULATION_MODE=true el envío se procesa de forma simulada y trazable
    sin abrir sockets de red externos.
    """
    custom_recipient = payload.recipient_email if payload else None
    try:
        result = await EmailReportService.send_daily_report(
            session=session,
            tenant_id=tenant_id,
            custom_recipient=custom_recipient,
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error en despacho de reporte: {str(exc)}")
