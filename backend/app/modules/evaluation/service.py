"""
Servicio de evaluación determinista de vacantes.
Principio: Determinismo primero (reglas antes de IA).
Calcula puntuación de compatibilidad con desglose auditable y justificación.
"""

from typing import List, Tuple
from backend.app.modules.profile.models import UserProfile, Skill
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation


class DeterministicEvaluationService:
    """Motor de evaluación de compatibilidad basado en reglas deterministas."""

    @classmethod
    def evaluate(
        cls,
        tenant_id: str,
        profile: UserProfile,
        cv: MasterCV,
        skills: List[Skill],
        vacancy: Vacancy,
    ) -> Evaluation:
        """
        Evalúa la compatibilidad entre el perfil/CV y la vacante usando reglas deterministas.
        """
        applied_rules = []
        matched_skills = []
        missing_skills = []

        # 1. Extracción de requerimientos de la vacante
        raw_reqs = vacancy.requirements if isinstance(vacancy.requirements, list) else []
        req_keywords = [str(r).lower().strip() for r in raw_reqs if str(r).strip()]

        # 2. Habilidades verificadas del perfil
        # REGLA ESTRICTA (Hito 2.1): Ningún hecho sin verificar se incorpora a la evaluación.
        # Solo consideramos habilidades con estado 'verified'. Habilidades 'pending' o 'rejected' se omiten.
        verified_skills_map = {
            s.name.lower().strip(): s for s in skills if s.verification_status == "verified"
        }

        # Regla 1: Coincidencia de habilidades
        matched_count = 0
        total_reqs = len(req_keywords) if req_keywords else 1

        for req in req_keywords:
            found = False
            for skill_name, skill_obj in verified_skills_map.items():
                if req in skill_name or skill_name in req:
                    matched_skills.append({
                        "requirement": req,
                        "matched_skill": skill_obj.name,
                        "proficiency": skill_obj.proficiency,
                        "years": skill_obj.years_experience,
                        "verified": (skill_obj.verification_status == "verified"),
                    })
                    matched_count += 1
                    found = True
                    break
            if not found:
                missing_skills.append(req)

        skill_ratio = min(1.0, matched_count / total_reqs) if req_keywords else 0.8
        applied_rules.append({
            "rule": "skill_match",
            "weight": 0.6,
            "matched": matched_count,
            "total": len(req_keywords),
            "score": round(skill_ratio, 2),
        })

        # Regla 2: Compatibilidad de modalidad / ubicación
        remote_compatible = False
        if vacancy.remote_type in ("remote", "unknown"):
            remote_compatible = True
            loc_score = 1.0
        elif vacancy.location and profile.location_country:
            if profile.location_country.lower() in vacancy.location.lower():
                remote_compatible = True
                loc_score = 0.9
            else:
                loc_score = 0.2
        else:
            loc_score = 0.5

        applied_rules.append({
            "rule": "location_and_remote",
            "weight": 0.25,
            "remote_type": vacancy.remote_type,
            "location_score": loc_score,
        })

        # Regla 3: Especialización del CV maestro
        specialization_match = 0.5
        if cv and cv.specialization:
            if (
                cv.specialization.lower() in vacancy.job_title.lower()
                or cv.specialization.lower() in vacancy.description.lower()
            ):
                specialization_match = 1.0
            else:
                specialization_match = 0.7

        applied_rules.append({
            "rule": "cv_specialization_alignment",
            "weight": 0.15,
            "specialization": cv.specialization if cv else "none",
            "score": specialization_match,
        })

        # Cálculo de puntaje ponderado
        final_score = round(
            (skill_ratio * 0.60) + (loc_score * 0.25) + (specialization_match * 0.15),
            2,
        )

        # Determinación de recomendación
        if final_score >= 0.80:
            rec = "strong_match"
        elif final_score >= 0.60:
            rec = "good_match"
        elif final_score >= 0.40:
            rec = "partial"
        else:
            rec = "weak"

        # Justificación explicativa
        matched_str = ", ".join(m["matched_skill"] for m in matched_skills) or "ninguna directa"
        missing_str = ", ".join(missing_skills) or "ninguna identificada"
        justification = (
            f"Evaluación determinista (puntuación: {final_score:.0%}). "
            f"Habilidades coincidentes ({len(matched_skills)}/{len(req_keywords)}): {matched_str}. "
            f"Habilidades faltantes: {missing_str}. "
            f"Modalidad: {vacancy.remote_type} (score ubicación: {loc_score:.2f}). "
            f"Alineación de CV maestro '{cv.name if cv else 'General'}': {specialization_match:.2f}."
        )

        return Evaluation(
            tenant_id=tenant_id,
            vacancy_id=vacancy.id,
            profile_id=profile.id,
            cv_id=cv.id if cv else "",
            compatibility_score=final_score,
            score_breakdown={
                "skills_score": round(skill_ratio, 2),
                "location_score": round(loc_score, 2),
                "specialization_score": round(specialization_match, 2),
            },
            justification=justification,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            recommendation=rec,
            rules_applied=applied_rules,
            ai_used=False,
        )
