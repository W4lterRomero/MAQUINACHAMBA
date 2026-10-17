"""
Router para evaluación de compatibilidad de vacantes (REQ-010, REQ-011, REQ-029).
Determinismo primero: cálculo explicable basado en reglas y perfil verificado.
Integración con IA (BYOK) en modo simulación con control de presupuesto y fallback determinista.
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.evaluation.service import DeterministicEvaluationService
from backend.app.modules.secrets.ai_service import AIService

router = APIRouter(prefix="/evaluations", tags=["Evaluations"])
ai_service = AIService()


class EvaluateRequest(BaseModel):
    vacancy_id: str = Field(..., description="ID de la vacante a evaluar")
    cv_id: Optional[str] = Field(None, description="ID del CV maestro a usar (opcional)")
    use_ai: Optional[bool] = Field(False, description="Intentar evaluación enriquecida con IA simulada")
    provider: Optional[str] = Field("openai", description="Proveedor configurado (openai, anthropic, google)")


@router.post("", status_code=status.HTTP_201_CREATED)
async def evaluate_vacancy(
    payload: EvaluateRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Evalúa la compatibilidad entre el perfil/CV y la vacante.
    Calcula siempre una base determinista explicable basada en hechos verificados.
    Si se solicita IA y hay presupuesto, ejecuta simulación de IA con registro de costo;
    en caso de presupuesto agotado o ausencia de clave, aplica fallback determinista sin gasto.
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

    # 4. Obtener skills verificadas
    s_res = await session.execute(
        select(Skill).where(Skill.profile_id == profile.id, Skill.tenant_id == tenant_id)
    )
    skills = list(s_res.scalars().all())

    # 5. Ejecutar evaluación determinista basada estrictamente en datos verificados
    evaluation = DeterministicEvaluationService.evaluate(
        tenant_id=tenant_id,
        profile=profile,
        cv=cv,
        skills=skills,
        vacancy=vacancy,
    )

    simulation_info: Optional[Dict[str, Any]] = None

    # 6. Si se solicita evaluación con IA (BYOK en modo simulación)
    if payload.use_ai:
        prov = (payload.provider or "openai").lower().strip()
        verified_skill_names = [s.name for s in skills if s.verification_status == "verified"]
        prompt_text = (
            f"Vacante: {vacancy.job_title} en {vacancy.company_name}.\n"
            f"Requisitos: {', '.join(str(r) for r in (vacancy.requirements or []))}.\n"
            f"Habilidades verificadas del candidato: {', '.join(verified_skill_names) or 'Ninguna verificada'}.\n"
            f"Especialización: {cv.specialization}."
        )

        ai_call_res = await ai_service.execute_simulated_call(
            session=session,
            tenant_id=tenant_id,
            provider=prov,
            operation="evaluation",
            prompt_text=prompt_text,
            expected_output_tokens=120,
            force_fallback=False,
            vacancy_id=vacancy.id,
            cv_id=cv.id,
        )

        if ai_call_res.get("is_fallback"):
            evaluation.ai_used = False
            evaluation.justification = (
                f"{evaluation.justification} "
                f"[Fallback determinista activado: {ai_call_res.get('fallback_reason')}]."
            )
            simulation_info = {
                "ai_requested": True,
                "ai_used": False,
                "is_fallback": True,
                "fallback_reason": ai_call_res.get("fallback_reason"),
                "cost_incurred_usd": 0.0,
                "tokens_consumed": 0,
                "simulation_banner": ai_call_res.get("simulation_banner"),
            }
        else:
            evaluation.ai_used = True
            sim_text = ai_call_res.get("data", {}).get("text", "")
            evaluation.justification = (
                f"{evaluation.justification}\n\n"
                f"Análisis IA simulado ({prov.upper()} - {ai_call_res.get('model')}): {sim_text}"
            )
            simulation_info = {
                "ai_requested": True,
                "ai_used": True,
                "is_fallback": False,
                "provider": prov,
                "model": ai_call_res.get("model"),
                "cost_incurred_usd": ai_call_res.get("cost_incurred_usd"),
                "tokens_consumed": ai_call_res.get("tokens_consumed"),
                "remaining_budget_usd": ai_call_res.get("remaining_budget_usd"),
                "simulation_banner": ai_call_res.get("simulation_banner"),
            }
    else:
        simulation_info = {
            "ai_requested": False,
            "ai_used": False,
            "is_fallback": False,
            "cost_incurred_usd": 0.0,
            "tokens_consumed": 0,
            "note": "Evaluación 100% determinista basada en reglas locales sin llamadas a IA.",
        }

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
        "rules_applied": evaluation.rules_applied,
        "ai_used": evaluation.ai_used,
        "simulation_info": simulation_info,
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
