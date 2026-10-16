"""
Router de la API para el Perfil de Comunicación y Estilo (REQ-006, D10).
Permite configurar el cuestionario de estilo, ejemplos de redacción y
previsualizar el mensaje determinista según el tono seleccionado.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.communication.service import (
    CommunicationService,
    CommunicationServiceError,
)

router = APIRouter(prefix="/communication", tags=["Communication"])


class ExampleTextItem(BaseModel):
    id: Optional[str] = None
    title: str = Field(..., max_length=100)
    content: str = Field(..., max_length=5000)


class CommunicationProfileUpdateRequest(BaseModel):
    tone: Optional[str] = None
    language_preferences: Optional[Dict[str, Any]] = None
    writing_rules: Optional[Dict[str, Any]] = None
    questionnaire_answers: Optional[Dict[str, Any]] = None
    example_texts: Optional[List[ExampleTextItem]] = None
    status: Optional[str] = None


class MessagePreviewRequest(BaseModel):
    vacancy_id: Optional[str] = None
    custom_vacancy_title: Optional[str] = None
    custom_company_name: Optional[str] = None
    override_tone: Optional[str] = None
    override_conciseness: Optional[str] = None
    override_focus_area: Optional[str] = None


@router.get("", response_model=Dict[str, Any])
async def get_communication_profile(
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Obtiene el perfil de comunicación y preferencias de estilo del tenant autenticado.
    """
    comm_profile = await CommunicationService.get_or_create_profile(session, tenant_id)
    return comm_profile.to_dict()


@router.post("", response_model=Dict[str, Any])
async def update_communication_profile(
    payload: CommunicationProfileUpdateRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Crea o actualiza las preferencias de comunicación, cuestionario y ejemplos del usuario.
    """
    try:
        examples_data = None
        if payload.example_texts is not None:
            examples_data = [item.model_dump() for item in payload.example_texts]

        updated = await CommunicationService.update_profile(
            session=session,
            tenant_id=tenant_id,
            tone=payload.tone,
            language_preferences=payload.language_preferences,
            writing_rules=payload.writing_rules,
            questionnaire_answers=payload.questionnaire_answers,
            example_texts=examples_data,
            status=payload.status,
        )
        return updated.to_dict()
    except CommunicationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/preview", response_model=Dict[str, Any])
async def preview_communication_message(
    payload: MessagePreviewRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
):
    """
    Genera en tiempo real una vista previa determinista del mensaje de candidatura
    según el estilo configurado, sin alterar hechos profesionales verificados.
    """
    try:
        preview_data = await CommunicationService.preview_message(
            session=session,
            tenant_id=tenant_id,
            vacancy_id=payload.vacancy_id,
            custom_vacancy_title=payload.custom_vacancy_title,
            custom_company_name=payload.custom_company_name,
            override_tone=payload.override_tone,
            override_conciseness=payload.override_conciseness,
            override_focus_area=payload.override_focus_area,
        )
        return preview_data
    except CommunicationServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
