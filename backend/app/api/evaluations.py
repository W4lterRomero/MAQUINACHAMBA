"""
Router para evaluación de compatibilidad de vacantes (REQ-010, REQ-011).
Determinismo primero: cálculo con desglose y reglas antes de IA.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.evaluation.service import DeterministicEvaluationService

router = APIRouter(prefix="/evaluations", tags=["Evaluations"])


class EvaluateRequest(BaseModel):
    vacancy_id: str
    cv_id: Optional[str] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def evaluate_vacancy(
    payload: EvaluateRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Evalúa deterministamente la compatibilidad entre el perfil/CV y la vacante.
    """
    # 1. Obtener perfil
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado.")

    # 2. Obtener vacante
    v_res = await session.execute(
        select(Vacancy).where(Vacancy.id == payload.vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = v_res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    # 3. Obtener CV (el especificado o el primer activo)
    if payload.cv_id:
        c_res = await session.execute(
            select(MasterCV).where(MasterCV.id == payload.cv_id, MasterCV.tenant_id == tenant_id)
        )
        cv = c_res.scalar_one_or_none()
    else:
        c_res = await session.execute(
            select(MasterCV)
            .where(MasterCV.profile_id == profile.id, MasterCV.tenant_id == tenant_id)
            .order_by(MasterCV.created_at.desc())
        )
        cv = c_res.scalars().first()

    if not cv:
        raise HTTPException(status_code=400, detail="No hay CV maestro disponible para evaluar.")

    # 4. Obtener skills
    s_res = await session.execute(
        select(Skill).where(Skill.profile_id == profile.id, Skill.tenant_id == tenant_id)
    )
    skills = s_res.scalars().all()

    # 5. Ejecutar evaluación determinista
    evaluation = DeterministicEvaluationService.evaluate(
        tenant_id=tenant_id,
        profile=profile,
        cv=cv,
        skills=list(skills),
        vacancy=vacancy,
    )
    session.add(evaluation)
    await session.commit()
    await session.refresh(evaluation)

    return {
        "id": evaluation.id,
        "vacancy_id": evaluation.vacancy_id,
        "cv_id": evaluation.cv_id,
        "compatibility_score": evaluation.compatibility_score,
        "recommendation": evaluation.recommendation,
        "justification": evaluation.justification,
        "matched_skills": evaluation.matched_skills,
        "missing_skills": evaluation.missing_skills,
        "score_breakdown": evaluation.score_breakdown,
        "ai_used": evaluation.ai_used,
    }


@router.get("/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene el resultado y desglose de una evaluación."""
    res = await session.execute(
        select(Evaluation).where(
            Evaluation.id == evaluation_id,
            Evaluation.tenant_id == tenant_id,
        )
    )
    ev = res.scalar_one_or_none()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada.")

    return {
        "id": ev.id,
        "vacancy_id": ev.vacancy_id,
        "cv_id": ev.cv_id,
        "compatibility_score": ev.compatibility_score,
        "recommendation": ev.recommendation,
        "justification": ev.justification,
        "matched_skills": ev.matched_skills,
        "missing_skills": ev.missing_skills,
        "score_breakdown": ev.score_breakdown,
        "rules_applied": ev.rules_applied,
        "ai_used": ev.ai_used,
    }
