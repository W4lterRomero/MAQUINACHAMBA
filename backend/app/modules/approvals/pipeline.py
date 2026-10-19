"""
Orquestador del Pipeline Automático de Preparación para la Bandeja de Aprobación (TAREA 3).
Flujo: Vacante importada → Validación → Evaluación → Selección de CV → Adaptación → Generación de mensaje → Creación del borrador → Bandeja de aprobación.
Aplica evaluación determinista primero, selección inteligente del CV maestro y fidelidad a hechos verificados.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.evaluation.service import DeterministicEvaluationService
from backend.app.modules.communication.models import CommunicationProfile
from backend.app.modules.communication.service import CommunicationService
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, CandidacyDocument
from backend.app.modules.approvals.service import ApprovalService

logger = logging.getLogger("maquinachamba.pipeline")


class AutoApprovalPipeline:
    """
    Orquesta la preparación automática de candidaturas para las vacantes descubiertas
    por ChatGPT, Gemini o importaciones manuales.
    """

    DEFAULT_AUTO_PREPARE_THRESHOLD = 0.60

    @classmethod
    def select_best_master_cv(
        cls,
        master_cvs: List[MasterCV],
        vacancy: Vacancy,
        verified_skills: List[Skill],
    ) -> Optional[MasterCV]:
        """
        Selecciona inteligentemente el CV maestro más adecuado según la vacante,
        no simplemente el último CV creado (REQ Tarea 3.2).
        Calcula la alineación de especialización y solapamiento de habilidades incluidas.
        """
        if not master_cvs:
            return None
        if len(master_cvs) == 1:
            return master_cvs[0]

        vac_text = f"{vacancy.job_title} {vacancy.description} {' '.join(vacancy.requirements or [])}".lower()

        best_cv = master_cvs[0]
        best_score = -1.0

        for cv in master_cvs:
            score = 0.0
            spec = (cv.specialization or "").lower().strip()
            name = (cv.name or "").lower().strip()

            # Coincidencia de especialización
            if spec and spec in vac_text:
                score += 3.0
            elif spec:
                spec_words = [w for w in spec.split() if len(w) > 3]
                score += sum(1.0 for w in spec_words if w in vac_text)

            # Coincidencia de nombre de CV
            if name and name in vac_text:
                score += 1.5

            # Solapamiento de habilidades incluidas en el CV maestro
            included_skill_names = set()
            if cv.sections and "skills" in cv.sections and isinstance(cv.sections["skills"], list):
                included_skill_names = {str(s).lower().strip() for s in cv.sections["skills"]}
            elif cv.sections and "included_skills" in cv.sections and isinstance(cv.sections["included_skills"], list):
                included_skill_names = {str(s).lower().strip() for s in cv.sections["included_skills"]}

            vac_reqs = {str(r).lower().strip() for r in (vacancy.requirements or [])}
            common_skills = included_skill_names.intersection(vac_reqs)
            score += len(common_skills) * 1.0

            if score > best_score:
                best_score = score
                best_cv = cv

        return best_cv

    @classmethod
    def evaluate_vacancy_deterministically(
        cls,
        tenant_id: str,
        profile: UserProfile,
        cv: Optional[MasterCV],
        skills: List[Skill],
        vacancy: Vacancy,
    ) -> Evaluation:
        """
        Ejecuta la evaluación determinista considerando habilidades verificadas, experiencia,
        restricciones geográficas y modalidad.
        No asigna compatibilidad alta si los requerimientos son desconocidos (REQ Tarea 3.1).
        """
        eval_result = DeterministicEvaluationService.evaluate(
            tenant_id=tenant_id,
            profile=profile,
            cv=cv,
            skills=skills,
            vacancy=vacancy,
        )

        # Penalización estricta por falta de datos: si los requerimientos técnicos eran desconocidos,
        # no permitir que un 'remote_type' infle artificialmente la nota por encima del 50%.
        raw_reqs = vacancy.requirements or []
        if not raw_reqs:
            # Requisitos desconocidos: acotar puntuación máxima a 0.45 y marcar recomendación incierta
            eval_result.compatibility_score = min(0.45, eval_result.compatibility_score)
            eval_result.recommendation = "weak"
            eval_result.justification = (
                f"{eval_result.justification} "
                f"[Aviso de incertidumbre: La vacante no especificó requerimientos técnicos; puntuación acotada]."
            )

        # Verificación de restricciones geográficas
        if vacancy.allowed_countries and profile.location_country:
            allowed_norm = [c.lower() for c in vacancy.allowed_countries]
            country_norm = profile.location_country.lower()
            is_country_allowed = any(
                country_norm in a or a in country_norm or a in ("worldwide", "global", "latam")
                for a in allowed_norm
            )
            if not is_country_allowed:
                eval_result.compatibility_score = max(0.10, eval_result.compatibility_score - 0.35)
                eval_result.justification += (
                    f" [Alerta geográfica: El país '{profile.location_country}' no figura entre los países "
                    f"autorizados para remoto: {', '.join(vacancy.allowed_countries)}]."
                )

        return eval_result

    @classmethod
    async def run_pipeline_for_vacancies(
        cls,
        session: AsyncSession,
        tenant_id: str,
        min_score_threshold: float = DEFAULT_AUTO_PREPARE_THRESHOLD,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Ejecuta el pipeline automático sobre vacantes sin candidatura previa:
        1. Carga perfil verificado y habilidades verificadas.
        2. Carga CVs maestros disponibles.
        3. Evalúa vacantes deterministamente.
        4. Si compatibilidad >= threshold y sin fraude:
           - Selecciona mejor CV maestro.
           - Genera carta adaptada con CommunicationProfile.
           - Prepara candidatura, genera PDF y DOCX con hashes inmutables.
           - Añade a la bandeja de aprobación en estado 'pending_approval'.
        5. Si compatibilidad < threshold: se mantiene en vacantes para revisión manual.
        """
        # 1. Perfil de usuario
        p_res = await session.execute(
            select(UserProfile).where(UserProfile.tenant_id == tenant_id).order_by(UserProfile.created_at.desc())
        )
        profile = p_res.scalars().first()
        if not profile:
            return {"status": "error", "message": "Perfil de usuario no configurado."}

        # 2. Habilidades verificadas estrictamente
        s_res = await session.execute(
            select(Skill).where(
                and_(Skill.tenant_id == tenant_id, Skill.verification_status == "verified")
            )
        )
        verified_skills = s_res.scalars().all()

        # 3. CVs maestros
        cv_res = await session.execute(
            select(MasterCV).where(MasterCV.tenant_id == tenant_id).order_by(MasterCV.created_at.desc())
        )
        master_cvs = cv_res.scalars().all()
        if not master_cvs:
            return {"status": "error", "message": "No hay CVs maestros configurados para adaptar candidaturas."}

        # 4. Perfil de comunicación
        comm_profile = await CommunicationService.get_or_create_profile(session, tenant_id, profile.id)

        # 5. Vacantes activas que no tengan ya una candidatura creada
        existing_cands_res = await session.execute(
            select(Candidacy.vacancy_id).where(Candidacy.tenant_id == tenant_id)
        )
        candidacy_vacancy_ids = set(existing_cands_res.scalars().all())

        vac_query = (
            select(Vacancy)
            .where(
                and_(
                    Vacancy.tenant_id == tenant_id,
                    Vacancy.is_active.is_(True),
                    Vacancy.id.not_in(candidacy_vacancy_ids) if candidacy_vacancy_ids else True,
                )
            )
            .order_by(Vacancy.created_at.desc())
            .limit(limit)
        )
        v_res = await session.execute(vac_query)
        candidate_vacancies = v_res.scalars().all()

        prepared_count = 0
        skipped_low_score_count = 0
        skipped_fraud_count = 0
        results_summary = []

        for vac in candidate_vacancies:
            # 5.1 Verificación de fraude y cierre
            integrity = VacancyIntegrityService.analyze_vacancy(
                title=vac.job_title,
                description=vac.description or "",
                source_url=vac.source_url or "",
                salary_min=float(vac.salary_min) if vac.salary_min else None,
                salary_max=float(vac.salary_max) if vac.salary_max else None,
                salary_currency=vac.salary_currency or "USD",
                closing_date=vac.closing_date,
            )
            is_fraud_suspect = integrity["is_fraud_suspect"] or any(
                s.get("severity") == "high" for s in (vac.fraud_signals or [])
            )
            if integrity["is_closed"] or is_fraud_suspect:
                skipped_fraud_count += 1
                continue

            # 5.2 Selección inteligente del mejor CV maestro
            best_cv = cls.select_best_master_cv(master_cvs, vac, verified_skills)

            # 5.3 Evaluación determinista
            evaluation = cls.evaluate_vacancy_deterministically(
                tenant_id=tenant_id,
                profile=profile,
                cv=best_cv,
                skills=verified_skills,
                vacancy=vac,
            )
            session.add(evaluation)
            await session.flush()

            # 5.4 Comprobación de umbral
            if evaluation.compatibility_score < min_score_threshold:
                skipped_low_score_count += 1
                continue

            # 5.5 Generación de carta de presentación
            verified_skill_names = [s.name for s in verified_skills]
            cover_letter, _, _ = CommunicationService.generate_deterministic_cover_letter(
                comm_profile=comm_profile,
                profile=profile,
                cv=best_cv,
                vacancy_title=vac.job_title,
                company_name=vac.company_name,
                verified_skills=verified_skill_names,
            )

            # 5.6 Creación del paquete de candidatura y documentos
            candidacy = await ApprovalService.prepare_candidacy(
                session=session,
                tenant_id=tenant_id,
                profile=profile,
                cv=best_cv,
                vacancy=vac,
                evaluation=evaluation,
                cover_letter=cover_letter,
            )
            # Marcar explícitamente como 'pending_approval' para la bandeja nocturna
            candidacy.status = "pending_approval"
            prepared_count += 1

            results_summary.append({
                "candidacy_id": candidacy.id,
                "company": vac.company_name,
                "title": vac.job_title,
                "score": evaluation.compatibility_score,
                "selected_cv": best_cv.name,
                "status": "pending_approval",
            })

        await session.commit()

        return {
            "status": "success",
            "vacancies_evaluated": len(candidate_vacancies),
            "candidacies_prepared": prepared_count,
            "skipped_low_score": skipped_low_score_count,
            "skipped_fraud_or_closed": skipped_fraud_count,
            "prepared_candidacies": results_summary,
        }
