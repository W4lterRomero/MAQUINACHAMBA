"""
Servicio del ciclo obligatorio de preparación, aprobación y control de cambios.
Aprobación inmutable, versionado y reversión ante modificaciones no autorizadas.
"""

from datetime import datetime, time, timezone
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.clock import Clock, get_clock
from backend.app.core.security import hash_content
from backend.app.modules.profile.models import UserProfile, Skill, Experience, ProfessionalSource
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.cv.generator import CVDocumentGenerator
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, CandidacyDocument
from backend.app.modules.crm.models import CRMEntry, CRMEvent
from backend.app.modules.scheduling.service import SchedulingService


class ApprovalService:
    """Gestiona el ciclo de vida de candidaturas, aprobaciones y tareas programadas."""

    @classmethod
    def compute_candidacy_hash(cls, candidacy: Candidacy) -> str:
        """
        Calcula el hash determinista del paquete completo de postulación.
        Cualquier variación en el contenido genera un hash diferente.
        Incorpora los hashes de los documentos vinculados (PDF y DOCX) (P1).
        """
        doc_hashes = (candidacy.adapted_cv_snapshot or {}).get("document_hashes") or {}
        return hash_content(
            candidacy.id,
            candidacy.vacancy_id,
            json.dumps(candidacy.adapted_cv_snapshot, sort_keys=True, ensure_ascii=False),
            candidacy.cover_letter,
            json.dumps(candidacy.form_responses, sort_keys=True, ensure_ascii=False),
            candidacy.target_portal,
            candidacy.target_url,
            json.dumps(candidacy.planned_actions, sort_keys=True, ensure_ascii=False),
            doc_hashes.get("pdf", ""),
            doc_hashes.get("docx", ""),
        )

    @classmethod
    async def prepare_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile: UserProfile,
        cv: MasterCV,
        vacancy: Vacancy,
        evaluation: Optional[Evaluation] = None,
        cover_letter: Optional[str] = None,
        exception_consent: Optional[Dict[str, Any]] = None,
    ) -> Candidacy:
        """
        Fase de preparación: compila los documentos y paquete de postulación.
        ESTRICTAMENTE NINGÚN ENVÍO durante esta fase.
        Congela el payload completo y genera/vincula los documentos PDF y DOCX
        con sus hashes SHA-256 antes de la aprobación (REQ-013, P1, P2).
        """
        # Formatear ubicación congelada
        location_str = ""
        if profile.location_city and profile.location_country:
            location_str = f"{profile.location_city}, {profile.location_country}"
        elif profile.location_city:
            location_str = profile.location_city

        # Recuperar habilidades verificadas (excluyendo datos sintéticos de demostración)
        s_stmt = (
            select(Skill)
            .where(
                and_(
                    Skill.tenant_id == tenant_id,
                    Skill.verification_status == "verified",
                    or_(Skill.context_type != "synthetic_demo", Skill.context_type.is_(None)),
                )
            )
            .order_by(Skill.created_at.asc())
        )
        s_res = await session.execute(s_stmt)
        all_verified_skills = s_res.scalars().all()

        # P2: Respetar selección explícita vs ausente y no reincorporar excluidas
        skills_to_include = []
        if cv.sections and "skills" in cv.sections:
            if isinstance(cv.sections["skills"], list):
                wanted = {str(name).lower() for name in cv.sections["skills"]}
                skills_to_include = [s for s in all_verified_skills if s.name.lower() in wanted]
            else:
                skills_to_include = []
        elif cv.sections and "included_skills" in cv.sections:
            if isinstance(cv.sections["included_skills"], list):
                wanted = {str(name).lower() for name in cv.sections["included_skills"]}
                skills_to_include = [s for s in all_verified_skills if s.name.lower() in wanted]
            else:
                skills_to_include = []
        else:
            skills_to_include = list(all_verified_skills)

        frozen_skills = [
            {
                "name": s.name,
                "category": s.category,
                "proficiency": s.proficiency,
                "years_experience": s.years_experience,
            }
            for s in skills_to_include
        ]

        # Recuperar experiencia laboral verificada (excluyendo fuentes sintéticas de demostración)
        e_stmt = (
            select(Experience)
            .outerjoin(ProfessionalSource, Experience.source_id == ProfessionalSource.id)
            .where(
                and_(
                    Experience.tenant_id == tenant_id,
                    Experience.verification_status == "verified",
                    or_(ProfessionalSource.source_type != "github_simulation", ProfessionalSource.source_type.is_(None)),
                )
            )
            .order_by(Experience.start_date.desc().nullslast())
        )
        e_res = await session.execute(e_stmt)
        verified_experiences = e_res.scalars().all()

        frozen_experiences = [
            {
                "company": e.company,
                "title": e.title,
                "start_date": str(e.start_date) if e.start_date else None,
                "end_date": str(e.end_date) if e.end_date else None,
                "description": e.description,
                "skills_used": e.skills_used if isinstance(e.skills_used, list) else [],
            }
            for e in verified_experiences
        ]

        # Payload completo congelado del CV adaptado
        adapted_cv = {
            "name": profile.full_name,
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": location_str,
            "title": cv.specialization or vacancy.job_title,
            "specialization": cv.specialization or vacancy.job_title,
            "summary": cv.summary or f"Profesional especializado en {cv.specialization}",
            "sections": cv.sections or {},
            "skills": frozen_skills,
            "experiences": frozen_experiences,
            "version": cv.version,
            "document_hashes": {},
        }

        # Generar documentos PDF y DOCX vinculados antes de aprobar (P1)
        cv_doc_payload = {
            "full_name": adapted_cv["full_name"],
            "email": adapted_cv["email"],
            "phone": adapted_cv["phone"],
            "location": adapted_cv["location"],
            "specialization": adapted_cv["specialization"],
            "summary": adapted_cv["summary"],
            "skills": adapted_cv["skills"],
            "experiences": adapted_cv["experiences"],
        }
        pdf_bytes = CVDocumentGenerator.generate_pdf(cv_doc_payload)
        docx_bytes = CVDocumentGenerator.generate_docx(cv_doc_payload)
        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
        docx_hash = hashlib.sha256(docx_bytes).hexdigest()

        adapted_cv["document_hashes"] = {
            "pdf": pdf_hash,
            "docx": docx_hash,
        }

        if cover_letter:
            letter = cover_letter
        else:
            try:
                from backend.app.modules.communication.service import CommunicationService
                letter = await CommunicationService.generate_cover_letter_for_candidacy(
                    session=session,
                    tenant_id=tenant_id,
                    profile=profile,
                    cv=cv,
                    vacancy=vacancy,
                )
            except Exception:
                letter = (
                    f"Estimado equipo de {vacancy.company_name},\n\n"
                    f"Me dirijo a ustedes con gran interés por la posición de {vacancy.job_title}. "
                    f"Cuento con experiencia alineada a sus requerimientos técnicos.\n\n"
                    f"Atentamente,\n{profile.full_name}"
                )

        planned_actions = [
            {"action": "authenticate_session", "target": vacancy.source},
            {"action": "fill_application_form", "fields": ["name", "email", "cv", "letter"]},
            {"action": "submit_candidacy", "method": "portal_mock"},
        ]
        if exception_consent:
            planned_actions.append(exception_consent)

        candidacy = Candidacy(
            tenant_id=tenant_id,
            vacancy_id=vacancy.id,
            profile_id=profile.id,
            cv_id=cv.id,
            evaluation_id=evaluation.id if evaluation else None,
            adapted_cv_snapshot=adapted_cv,
            cover_letter=letter,
            form_responses={"applicant_email": profile.email, "salary_expectation": float(vacancy.salary_min) if vacancy.salary_min is not None else 0},
            target_portal=vacancy.source,
            target_url=vacancy.source_url,
            planned_actions=planned_actions,
            status="pending_approval",
            version=1,
        )
        session.add(candidacy)
        await session.flush()

        # Persistir los archivos físicos vinculados en el directorio del tenant
        docs_dir = os.path.join("data", "documents", tenant_id)
        os.makedirs(docs_dir, exist_ok=True)
        pdf_filename = f"Candidatura_{candidacy.id[:8]}_CV_v1.pdf"
        docx_filename = f"Candidatura_{candidacy.id[:8]}_CV_v1.docx"
        pdf_path = os.path.join(docs_dir, pdf_filename)
        docx_path = os.path.join(docs_dir, docx_filename)

        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        cand_doc_pdf = CandidacyDocument(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            document_type="cv_pdf",
            file_path=pdf_path,
            file_hash=pdf_hash,
            version=candidacy.version,
        )
        cand_doc_docx = CandidacyDocument(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            document_type="cv_docx",
            file_path=docx_path,
            file_hash=docx_hash,
            version=candidacy.version,
        )
        session.add(cand_doc_pdf)
        session.add(cand_doc_docx)
        await session.flush()

        # Registro inicial en CRM
        crm_entry = CRMEntry(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            vacancy_id=vacancy.id,
            current_status="pending_approval",
            priority="medium",
            notes="Candidatura preparada, pendiente de revisión y aprobación humana.",
        )
        session.add(crm_entry)
        await session.flush()

        event = CRMEvent(
            tenant_id=tenant_id,
            crm_entry_id=crm_entry.id,
            event_type="prepared",
            old_status="draft",
            new_status="pending_approval",
            details={"vacancy": vacancy.job_title, "company": vacancy.company_name},
        )
        session.add(event)
        await session.flush()

        return candidacy

    @classmethod
    async def approve_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        user_timezone: str = "America/Mexico_City",
        window_start: time = time(9, 0),
        notes: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> Tuple[Approval, ScheduledTask]:
        """
        Aprobación explícita por el usuario.
        Genera registro inmutable de aprobación y programa la tarea para el día siguiente.
        """
        c = clock or get_clock()

        # 1. Obtener candidatura
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        if candidacy.status not in ("pending_approval", "postponed"):
            raise ValueError(f"No se puede aprobar una candidatura en estado: {candidacy.status}")

        # Validar vigencia e integridad de la vacante (P1, REQ-009)
        v_stmt = select(Vacancy).where(Vacancy.id == candidacy.vacancy_id, Vacancy.tenant_id == tenant_id)
        v_res = await session.execute(v_stmt)
        vacancy = v_res.scalar_one_or_none()
        if vacancy:
            analysis = VacancyIntegrityService.analyze_vacancy(
                title=vacancy.job_title,
                description=vacancy.description,
                source_url=vacancy.source_url or "",
                salary_min=float(vacancy.salary_min) if vacancy.salary_min else None,
                salary_max=float(vacancy.salary_max) if vacancy.salary_max else None,
                salary_currency=vacancy.salary_currency or "USD",
                closing_date=vacancy.closing_date,
                reference_date=c.now_utc().date(),
            )
            was_inactive = not vacancy.is_active
            is_closed = was_inactive or analysis["is_closed"]
            is_fraud_suspect = analysis["is_fraud_suspect"] or any(s.get("severity") == "high" for s in (vacancy.fraud_signals or []))

            # Sincronizar estado en vacante
            vacancy.is_active = not is_closed
            existing_signals = {s.get("id"): s for s in (vacancy.fraud_signals or [])}
            for s in analysis["fraud_signals"]:
                existing_signals[s["id"]] = s
            vacancy.fraud_signals = list(existing_signals.values())

            if analysis["closure_reason"]:
                vacancy.closure_reason = analysis["closure_reason"]
            elif was_inactive and not vacancy.closure_reason:
                vacancy.closure_reason = "Vacante inactiva en el sistema."
            vacancy.last_verified_at = c.now_utc()
            session.add(vacancy)
            await session.flush()

            if is_closed or is_fraud_suspect:
                actions = candidacy.planned_actions or []
                consent = next((a for a in actions if a.get("action") == "user_exception_consent"), None)
                if not consent:
                    reason_str = vacancy.closure_reason or "Señales de fraude de alta gravedad detectadas."
                    raise ValueError(
                        f"No se puede aprobar la candidatura: la vacante está cerrada o presenta indicios de fraude ({reason_str}) y no cuenta con consentimiento explícito de excepción."
                    )

        # 2. Calcular hash inmutable del paquete
        content_hash = cls.compute_candidacy_hash(candidacy)

        # 3. Crear registro inmutable de aprobación
        doc_hashes = (candidacy.adapted_cv_snapshot or {}).get("document_hashes") or {}
        approval = Approval(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            decision="approved",
            decided_by="user",
            decided_at=c.now_utc(),
            content_hash=content_hash,
            document_versions={
                "candidacy_version": candidacy.version,
                "cv_version": candidacy.adapted_cv_snapshot.get("version", 1),
                "cv_pdf_hash": doc_hashes.get("pdf", ""),
                "cv_docx_hash": doc_hashes.get("docx", ""),
            },
            notes=notes,
        )
        session.add(approval)
        await session.flush()

        # 4. Programar para el ciclo del día siguiente
        scheduled_date, scheduled_time, exec_utc = SchedulingService.calculate_next_day_execution(
            user_timezone=user_timezone,
            execution_window_start=window_start,
            clock=c,
        )
        idempotency_key = SchedulingService.generate_idempotency_key(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            scheduled_date=scheduled_date,
            version=candidacy.version,
        )

        task = ScheduledTask(
            tenant_id=tenant_id,
            approval_id=approval.id,
            candidacy_id=candidacy.id,
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
            execution_timezone=user_timezone,
            execution_instant_utc=exec_utc,
            status="pending",
            idempotency_key=idempotency_key,
            attempts=0,
            max_attempts=1,
        )
        session.add(task)

        # 5. Actualizar candidatura y CRM
        candidacy.status = "scheduled"

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "scheduled"
            crm.next_action = f"Ejecución automática programada para {scheduled_date.isoformat()} {scheduled_time.isoformat()}"
            crm.next_action_date = scheduled_date

            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="approved_and_scheduled",
                old_status=old_st,
                new_status="scheduled",
                details={
                    "scheduled_date": scheduled_date.isoformat(),
                    "instant_utc": exec_utc.isoformat(),
                    "content_hash": content_hash,
                },
            ))

        await session.flush()
        return approval, task

    @classmethod
    async def reject_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        reason: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> Approval:
        """Rechazo explícito por el usuario. Bloquea permanentemente el envío."""
        c = clock or get_clock()
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        content_hash = cls.compute_candidacy_hash(candidacy)
        approval = Approval(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            decision="rejected",
            decided_by="user",
            decided_at=c.now_utc(),
            content_hash=content_hash,
            notes=reason,
        )
        session.add(approval)

        candidacy.status = "rejected_by_user"

        # Cancelar cualquier tarea programada
        tasks_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "pending",
            )
        )
        for t in tasks_res.scalars():
            t.status = "cancelled"

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "rejected_by_user"
            crm.notes = f"Rechazada por el usuario: {reason or 'Sin nota'}"
            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="rejected_by_user",
                old_status=old_st,
                new_status="rejected_by_user",
                details={"reason": reason},
            ))

        await session.flush()
        return approval

    @classmethod
    async def postpone_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        notes: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> Candidacy:
        """Pospone una candidatura para revisión posterior sin programar envío."""
        c = clock or get_clock()
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        candidacy.status = "postponed"

        tasks_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "pending",
            )
        )
        for t in tasks_res.scalars():
            t.status = "cancelled"

        crm_res = await session.execute(
            select(CRMEntry).where(CRMEntry.candidacy_id == candidacy.id, CRMEntry.tenant_id == tenant_id)
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "postponed"
            crm.notes = f"Pospuesta: {notes or 'Revisión nocturna posterior'}"
            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="postponed",
                old_status=old_st,
                new_status="postponed",
                details={"notes": notes},
            ))

        await session.flush()
        return candidacy

    @classmethod
    async def modify_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        new_cover_letter: Optional[str] = None,
        new_cv_snapshot: Optional[dict] = None,
        clock: Optional[Clock] = None,
    ) -> Candidacy:
        """
        Regla crítica: Cualquier modificación en el contenido que se enviará
        INVALIDA inmediatamente una aprobación previa y cancela su tarea.
        """
        c = clock or get_clock()
        stmt = select(Candidacy).where(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        res = await session.execute(stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise ValueError(f"Candidatura no encontrada: {candidacy_id}")

        # Invalidar aprobaciones previas activas
        approvals_res = await session.execute(
            select(Approval).where(
                Approval.candidacy_id == candidacy.id,
                Approval.tenant_id == tenant_id,
                Approval.decision == "approved",
                Approval.invalidated_at.is_(None),
            )
        )
        for app in approvals_res.scalars():
            app.invalidated_at = c.now_utc()
            app.invalidation_reason = "Contenido modificado; requiere nueva revisión y aprobación."

        # Cancelar tareas programadas activas
        tasks_res = await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.candidacy_id == candidacy.id,
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == "pending",
            )
        )
        for t in tasks_res.scalars():
            t.status = "cancelled"

        # Aplicar cambios y devolver a revisión
        if new_cover_letter is not None:
            candidacy.cover_letter = new_cover_letter
        if new_cv_snapshot is not None:
            candidacy.adapted_cv_snapshot = new_cv_snapshot

        candidacy.version += 1
        candidacy.status = "pending_approval"

        # Regenerar y vincular documentos para la nueva versión de candidatura
        snap = candidacy.adapted_cv_snapshot or {}
        cv_doc_payload = {
            "full_name": snap.get("full_name") or snap.get("name") or "Candidato",
            "email": snap.get("email") or "",
            "phone": snap.get("phone"),
            "location": snap.get("location") or "",
            "specialization": snap.get("specialization") or snap.get("title") or "Candidatura Especializada",
            "summary": snap.get("summary") or "",
            "skills": snap.get("skills") or [],
            "experiences": snap.get("experiences") or [],
        }
        pdf_bytes = CVDocumentGenerator.generate_pdf(cv_doc_payload)
        docx_bytes = CVDocumentGenerator.generate_docx(cv_doc_payload)
        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
        docx_hash = hashlib.sha256(docx_bytes).hexdigest()

        # Actualizar hashes en snapshot
        snap = dict(snap)
        snap["document_hashes"] = {"pdf": pdf_hash, "docx": docx_hash}
        candidacy.adapted_cv_snapshot = snap

        docs_dir = os.path.join("data", "documents", tenant_id)
        os.makedirs(docs_dir, exist_ok=True)
        pdf_filename = f"Candidatura_{candidacy.id[:8]}_CV_v{candidacy.version}.pdf"
        docx_filename = f"Candidatura_{candidacy.id[:8]}_CV_v{candidacy.version}.docx"
        pdf_path = os.path.join(docs_dir, pdf_filename)
        docx_path = os.path.join(docs_dir, docx_filename)

        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        cand_doc_pdf = CandidacyDocument(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            document_type="cv_pdf",
            file_path=pdf_path,
            file_hash=pdf_hash,
            version=candidacy.version,
        )
        cand_doc_docx = CandidacyDocument(
            tenant_id=tenant_id,
            candidacy_id=candidacy.id,
            document_type="cv_docx",
            file_path=docx_path,
            file_hash=docx_hash,
            version=candidacy.version,
        )
        session.add(cand_doc_pdf)
        session.add(cand_doc_docx)

        crm_res = await session.execute(
            select(CRMEntry).where(
                CRMEntry.candidacy_id == candidacy.id,
                CRMEntry.tenant_id == tenant_id,
            )
        )
        crm = crm_res.scalar_one_or_none()
        if crm:
            old_st = crm.current_status
            crm.current_status = "pending_approval"
            session.add(CRMEvent(
                tenant_id=tenant_id,
                crm_entry_id=crm.id,
                event_type="approval_invalidated_by_edit",
                old_status=old_st,
                new_status="pending_approval",
                details={"version": candidacy.version, "reason": "Edit invalidated approval"},
            ))

        await session.flush()
        return candidacy
