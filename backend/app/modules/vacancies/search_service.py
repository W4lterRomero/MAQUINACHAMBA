"""
Servicio de búsqueda y descubrimiento de ofertas de empleo (REQ-007, REQ-008, REQ-009, REQ-030).
Gestiona preferencias persistentes, deduplicación, exclusiones, normalización,
clasificación determinista contra el perfil del usuario y control periódico de ejecuciones.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.cv.models import MasterCV
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.evaluation.service import DeterministicEvaluationService
from backend.app.modules.profile.models import Skill, UserProfile
from backend.app.modules.vacancies.connectors.remotive import RemotiveJobConnector
from backend.app.modules.vacancies.models import JobSearchPreferences, Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService

logger = logging.getLogger("maquinachamba.vacancies.search")


class JobSearchService:
    """Servicio para orquestar la búsqueda real, preferencias y clasificación de vacantes."""

    @classmethod
    async def get_or_create_preferences(
        cls,
        session: AsyncSession,
        tenant_id: str,
    ) -> JobSearchPreferences:
        """Obtiene o crea las preferencias persistentes de búsqueda del tenant."""
        stmt = select(JobSearchPreferences).where(JobSearchPreferences.tenant_id == tenant_id)
        result = await session.execute(stmt)
        pref = result.scalar_one_or_none()

        if not pref:
            pref = JobSearchPreferences(
                tenant_id=tenant_id,
                desired_job_titles=["Python Backend Engineer", "Desarrollador Software"],
                skills=["Python", "FastAPI", "SQLAlchemy", "Git", "Docker"],
                locations=["Remoto", "Worldwide", "LATAM", "Mexico"],
                remote_modalities=["remote"],
                experience_level="mid_senior",
                salary_min=None,
                salary_currency="USD",
                excluded_keywords=["apuestas", "casino", "crypto trading", "ventas por comision"],
                excluded_companies=[],
                is_periodic_enabled=False,
                interval_hours=24,
                source_access_authorized=False,
                authorized_sources=["remotive"],
                last_status="idle",
            )
            session.add(pref)
            await session.commit()
            await session.refresh(pref)

        return pref

    @classmethod
    async def update_preferences(
        cls,
        session: AsyncSession,
        tenant_id: str,
        data: Dict[str, Any],
    ) -> JobSearchPreferences:
        """Actualiza los criterios de búsqueda y exclusión del tenant."""
        pref = await cls.get_or_create_preferences(session, tenant_id)

        allowed_fields = [
            "desired_job_titles",
            "skills",
            "locations",
            "remote_modalities",
            "experience_level",
            "salary_min",
            "salary_currency",
            "excluded_keywords",
            "excluded_companies",
            "interval_hours",
        ]

        for field in allowed_fields:
            if field in data and data[field] is not None:
                setattr(pref, field, data[field])

        await session.commit()
        await session.refresh(pref)
        return pref

    @classmethod
    async def authorize_source_access(
        cls,
        session: AsyncSession,
        tenant_id: str,
        source_name: str = "remotive",
    ) -> JobSearchPreferences:
        """
        Registra la autorización explícita del usuario para acceder a una fuente externa de red.
        Cumple con el protocolo de consentimiento previo del propietario.
        """
        pref = await cls.get_or_create_preferences(session, tenant_id)
        pref.source_access_authorized = True
        if source_name not in pref.authorized_sources:
            sources = list(pref.authorized_sources)
            sources.append(source_name)
            pref.authorized_sources = sources

        await session.commit()
        await session.refresh(pref)
        logger.info(f"[{tenant_id}] Autorización de acceso concedida para fuente externa: '{source_name}'.")
        return pref

    @classmethod
    async def revoke_source_access(
        cls,
        session: AsyncSession,
        tenant_id: str,
        source_name: str = "remotive",
    ) -> JobSearchPreferences:
        """Revoca la autorización de acceso a la fuente externa."""
        pref = await cls.get_or_create_preferences(session, tenant_id)
        pref.source_access_authorized = False
        await session.commit()
        await session.refresh(pref)
        logger.info(f"[{tenant_id}] Autorización de acceso revocada para fuente externa: '{source_name}'.")
        return pref

    @classmethod
    async def toggle_periodic_search(
        cls,
        session: AsyncSession,
        tenant_id: str,
        enabled: bool,
        interval_hours: Optional[int] = None,
    ) -> JobSearchPreferences:
        """Inicia o pausa la búsqueda periódica automática en segundo plano."""
        pref = await cls.get_or_create_preferences(session, tenant_id)
        pref.is_periodic_enabled = enabled
        if interval_hours and interval_hours > 0:
            pref.interval_hours = interval_hours

        now = datetime.now(timezone.utc)
        if enabled:
            # Próxima ejecución programada
            pref.next_run_at = now
            pref.last_status = "idle"
        else:
            pref.next_run_at = None

        await session.commit()
        await session.refresh(pref)
        logger.info(f"[{tenant_id}] Búsqueda periódica {'activada' if enabled else 'pausada'}.")
        return pref

    @classmethod
    async def execute_job_search(
        cls,
        session: AsyncSession,
        tenant_id: str,
        force_dry_run: bool = False,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Ejecuta la búsqueda de vacantes para el tenant:
        1. Consulta preferencias y estado de autorización de red.
        2. Invoca el conector de Remotive (modo real autorizado o muestra estructurada).
        3. Filtra exclusiones declaradas (empresas y palabras clave).
        4. Detecta señales de fraude y vigencia de la oferta.
        5. Aplica deduplicación estricta (dedup_hash).
        6. Persiste ofertas nuevas y actualiza existentes.
        7. Clasifica la compatibilidad determinista respecto al perfil verificado del usuario.
        8. Actualiza estadísticas y próxima ejecución.
        """
        pref = await cls.get_or_create_preferences(session, tenant_id)
        pref.last_status = "running"
        await session.flush()

        connector = RemotiveJobConnector()
        dry_run = force_dry_run or not pref.source_access_authorized

        try:
            raw_results = await connector.search_jobs(
                preferences=pref,
                limit=limit,
                dry_run=dry_run,
            )
        except Exception as conn_err:
            pref.last_status = "error"
            pref.last_error = str(conn_err)
            await session.commit()
            raise conn_err

        # Cargar perfil activo y CV maestro para clasificación determinista explicable
        prof_res = await session.execute(
            select(UserProfile).where(UserProfile.tenant_id == tenant_id).order_by(UserProfile.created_at.desc())
        )
        profile = prof_res.scalars().first()

        cv_res = await session.execute(
            select(MasterCV).where(MasterCV.tenant_id == tenant_id, MasterCV.is_active.is_(True)).order_by(MasterCV.created_at.desc())
        )
        cv = cv_res.scalars().first()

        skills_res = await session.execute(
            select(Skill).where(Skill.tenant_id == tenant_id)
        )
        skills = skills_res.scalars().all()

        excluded_companies_lower = {str(c).lower().strip() for c in (pref.excluded_companies or []) if str(c).strip()}
        excluded_keywords_lower = [str(k).lower().strip() for k in (pref.excluded_keywords or []) if str(k).strip()]

        jobs_found = len(raw_results)
        jobs_new = 0
        jobs_duplicate = 0
        jobs_excluded = 0
        saved_vacancies: List[Dict[str, Any]] = []

        now = datetime.now(timezone.utc)

        for item in raw_results:
            comp_lower = item.company_name.lower().strip()
            title_lower = item.job_title.lower()
            desc_lower = item.description.lower()

            # 1. Filtro de empresas excluidas
            if comp_lower in excluded_companies_lower:
                jobs_excluded += 1
                logger.info(f"[{tenant_id}] Vacante descartada por empresa excluida: '{item.company_name}'")
                continue

            # 2. Filtro de palabras clave excluidas
            has_excluded_kw = any(kw in title_lower or kw in desc_lower for kw in excluded_keywords_lower)
            if has_excluded_kw:
                jobs_excluded += 1
                logger.info(f"[{tenant_id}] Vacante descartada por palabra clave excluida: '{item.job_title}'")
                continue

            # 3. Análisis de integridad y fraude (REQ-009)
            integrity = VacancyIntegrityService.analyze_vacancy(
                title=item.job_title,
                description=item.description,
                source_url=item.source_url,
                salary_min=item.salary_min,
                salary_max=item.salary_max,
                salary_currency=item.salary_currency,
                closing_date=item.closing_date,
            )

            # 4. Cálculo de dedup_hash determinista (REQ-008)
            dedup_input = f"{item.source_url.strip().lower()}|{comp_lower}|{title_lower}"
            dedup_hash = hashlib.sha256(dedup_input.encode("utf-8")).hexdigest()

            # 5. Comprobar existencia previa
            existing_res = await session.execute(
                select(Vacancy).where(
                    Vacancy.tenant_id == tenant_id,
                    Vacancy.dedup_hash == dedup_hash,
                )
            )
            existing = existing_res.scalar_one_or_none()

            if existing:
                existing.last_verified_at = now
                existing.fetched_at = item.fetched_at
                jobs_duplicate += 1
                vac_target = existing
            else:
                vac_target = Vacancy(
                    tenant_id=tenant_id,
                    external_id=item.external_id,
                    source=item.source,
                    source_url=item.source_url,
                    company_name=item.company_name,
                    job_title=item.job_title,
                    location=item.location,
                    remote_type=item.remote_type,
                    salary_min=item.salary_min,
                    salary_max=item.salary_max,
                    salary_currency=item.salary_currency,
                    requirements=item.requirements,
                    description=item.description,
                    posted_date=item.posted_date,
                    closing_date=item.closing_date,
                    is_active=integrity["is_active"],
                    fraud_signals=integrity["fraud_signals"],
                    closure_reason=integrity["closure_reason"],
                    dedup_hash=dedup_hash,
                    last_verified_at=now,
                    fetched_at=item.fetched_at,
                    is_real_search_result=item.is_real_search_result,
                    raw_source_payload=item.raw_payload,
                )
                session.add(vac_target)
                await session.flush()
                jobs_new += 1

            # 6. Clasificación explicable automática respecto al perfil verificado
            compat_score = 0.0
            recommendation = "insufficient_data"
            matched_skills_list = []
            missing_skills_list = []

            if profile and cv:
                # Comprobar si ya existe evaluación
                eval_stmt = select(Evaluation).where(
                    Evaluation.tenant_id == tenant_id,
                    Evaluation.vacancy_id == vac_target.id,
                    Evaluation.cv_id == cv.id,
                )
                eval_res = await session.execute(eval_stmt)
                eval_obj = eval_res.scalar_one_or_none()

                if not eval_obj:
                    eval_obj = DeterministicEvaluationService.evaluate(
                        tenant_id=tenant_id,
                        profile=profile,
                        cv=cv,
                        skills=skills,
                        vacancy=vac_target,
                    )
                    session.add(eval_obj)
                    await session.flush()

                compat_score = eval_obj.compatibility_score
                recommendation = eval_obj.recommendation
                matched_skills_list = eval_obj.matched_skills or []
                missing_skills_list = eval_obj.missing_skills or []

            saved_vacancies.append({
                "id": vac_target.id,
                "job_title": vac_target.job_title,
                "company_name": vac_target.company_name,
                "location": vac_target.location,
                "remote_type": vac_target.remote_type,
                "source": vac_target.source,
                "source_url": vac_target.source_url,
                "posted_date": vac_target.posted_date.isoformat() if vac_target.posted_date else None,
                "fetched_at": vac_target.fetched_at.isoformat() if vac_target.fetched_at else None,
                "is_active": vac_target.is_active,
                "is_fraud_suspect": integrity["is_fraud_suspect"],
                "fraud_signals": vac_target.fraud_signals or [],
                "compatibility_score": compat_score,
                "recommendation": recommendation,
                "matched_skills": matched_skills_list,
                "missing_skills": missing_skills_list,
                "is_real_search_result": vac_target.is_real_search_result,
                "unknown_fields": item.unknown_fields,
            })

        # 7. Actualizar preferencias y próxima ejecución
        pref.last_run_at = now
        if pref.is_periodic_enabled:
            pref.next_run_at = now + timedelta(hours=pref.interval_hours)
        pref.last_status = "completed"
        pref.last_error = None
        pref.last_results_count = jobs_found
        pref.last_new_jobs_count = jobs_new

        await session.commit()

        logger.info(
            f"[{tenant_id}] Búsqueda completada: {jobs_found} encontradas, "
            f"{jobs_new} nuevas, {jobs_duplicate} duplicadas, {jobs_excluded} excluidas."
        )

        return {
            "status": "completed",
            "source": connector.source_name,
            "is_real_network_call": not dry_run,
            "source_access_authorized": pref.source_access_authorized,
            "jobs_found": jobs_found,
            "jobs_new": jobs_new,
            "jobs_duplicate": jobs_duplicate,
            "jobs_excluded": jobs_excluded,
            "vacancies": saved_vacancies,
        }
