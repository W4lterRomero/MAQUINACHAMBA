"""
Servicio del dominio de CV (Curriculum Vitae).
Gestiona la exportación y generación local de CVs maestros y documentos de candidatura
en formatos PDF y DOCX con trazabilidad de hash SHA-256 e inmutabilidad (REQ-013, D9, D12).
"""

from datetime import datetime, timezone
import hashlib
import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.cv.generator import CVDocumentGenerator
from backend.app.modules.approvals.models import Candidacy, CandidacyDocument


class CVServiceError(Exception):
    """Excepción base para el servicio de CV."""
    pass


class CVServiceNotFoundError(CVServiceError):
    """Error cuando el recurso solicitado no existe o pertenece a otro tenant."""
    pass


class CVServiceImmutableError(CVServiceError):
    """Error cuando se detecta intento de sustitución silenciosa en candidatura aprobada."""
    pass


class CVService:
    """Servicio para exportación y gestión documental de CVs."""

    SUPPORTED_FORMATS = {"pdf", "docx"}
    MEDIA_TYPES = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    @classmethod
    async def export_master_cv(
        cls,
        session: AsyncSession,
        tenant_id: str,
        cv_id: str,
        format_type: str,
    ) -> Tuple[bytes, str, str, str]:
        """
        Exporta un CV maestro en PDF o DOCX garantizando aislamiento por tenant y uso estricto
        de hechos verificados.
        Retorna (content_bytes, filename, media_type, file_hash).
        """
        fmt = format_type.strip().lower()
        if fmt not in cls.SUPPORTED_FORMATS:
            raise CVServiceError(f"Formato no soportado '{format_type}'. Formatos permitidos: {sorted(cls.SUPPORTED_FORMATS)}")

        # 1. Recuperar CV maestro con filtro de tenant_id
        cv_stmt = select(MasterCV).where(
            and_(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id)
        )
        res = await session.execute(cv_stmt)
        cv = res.scalar_one_or_none()
        if not cv:
            raise CVServiceNotFoundError(f"CV maestro '{cv_id}' no encontrado.")

        # 2. Recuperar perfil de usuario
        p_stmt = select(UserProfile).where(UserProfile.tenant_id == tenant_id)
        p_res = await session.execute(p_stmt)
        profile = p_res.scalar_one_or_none()
        if not profile:
            raise CVServiceNotFoundError("Perfil de usuario no configurado.")

        # 3. Recuperar habilidades verificadas
        s_stmt = (
            select(Skill)
            .where(
                and_(
                    Skill.tenant_id == tenant_id,
                    Skill.verification_status == "verified",
                )
            )
            .order_by(Skill.created_at.asc())
        )
        s_res = await session.execute(s_stmt)
        all_verified_skills = s_res.scalars().all()

        # Filtrar o priorizar habilidades si el CV maestro especifica secciones de habilidades
        skills_to_include = []
        if cv.sections and isinstance(cv.sections.get("skills"), list):
            wanted_skill_names = {str(name).lower() for name in cv.sections["skills"]}
            skills_to_include = [
                s for s in all_verified_skills if s.name.lower() in wanted_skill_names
            ]
        if not skills_to_include:
            skills_to_include = list(all_verified_skills)

        # 4. Recuperar experiencia laboral verificada
        e_stmt = (
            select(Experience)
            .where(
                and_(
                    Experience.tenant_id == tenant_id,
                    Experience.verification_status == "verified",
                )
            )
            .order_by(Experience.start_date.desc().nullslast())
        )
        e_res = await session.execute(e_stmt)
        verified_experiences = e_res.scalars().all()

        # 5. Estructurar payload para generación
        location_str = ""
        if profile.location_city and profile.location_country:
            location_str = f"{profile.location_city}, {profile.location_country}"
        elif profile.location_city:
            location_str = profile.location_city

        cv_payload = {
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": location_str,
            "specialization": cv.specialization,
            "summary": cv.summary,
            "skills": [
                {
                    "name": s.name,
                    "category": s.category,
                    "proficiency": s.proficiency,
                    "years_experience": s.years_experience,
                }
                for s in skills_to_include
            ],
            "experiences": [
                {
                    "company": e.company,
                    "title": e.title,
                    "start_date": str(e.start_date) if e.start_date else None,
                    "end_date": str(e.end_date) if e.end_date else None,
                    "description": e.description,
                    "skills_used": e.skills_used if isinstance(e.skills_used, list) else [],
                }
                for e in verified_experiences
            ],
        }

        # 6. Generar documento
        if fmt == "pdf":
            content_bytes = CVDocumentGenerator.generate_pdf(cv_payload)
        else:
            content_bytes = CVDocumentGenerator.generate_docx(cv_payload)

        file_hash = hashlib.sha256(content_bytes).hexdigest()
        clean_cv_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", cv.name).strip("_")
        filename = f"CV_{clean_cv_name}_v{cv.version}.{fmt}"
        media_type = cls.MEDIA_TYPES[fmt]

        return content_bytes, filename, media_type, file_hash

    @classmethod
    async def export_candidacy_cv(
        cls,
        session: AsyncSession,
        tenant_id: str,
        candidacy_id: str,
        format_type: str,
    ) -> Tuple[bytes, str, str, str]:
        """
        Exporta el CV adaptado a partir del snapshot congelado de una candidatura.
        Vincular versión/hash del archivo al paquete revisado y previene sustituciones silenciosas.
        Retorna (content_bytes, filename, media_type, file_hash).
        """
        fmt = format_type.strip().lower()
        if fmt not in cls.SUPPORTED_FORMATS:
            raise CVServiceError(f"Formato no soportado '{format_type}'. Formatos permitidos: {sorted(cls.SUPPORTED_FORMATS)}")

        doc_type = f"cv_{fmt}"

        # 1. Recuperar candidatura con aislamiento de tenant_id
        c_stmt = select(Candidacy).where(
            and_(Candidacy.id == candidacy_id, Candidacy.tenant_id == tenant_id)
        )
        res = await session.execute(c_stmt)
        candidacy = res.scalar_one_or_none()
        if not candidacy:
            raise CVServiceNotFoundError(f"Candidatura '{candidacy_id}' no encontrada.")

        # 2. Recuperar perfil de usuario
        p_stmt = select(UserProfile).where(UserProfile.tenant_id == tenant_id)
        p_res = await session.execute(p_stmt)
        profile = p_res.scalar_one_or_none()

        # 3. Snapshot congelado del CV adaptado
        snapshot = candidacy.adapted_cv_snapshot or {}

        # 4. Habilidades verificadas (respetando la selección congelada del snapshot)
        s_stmt = (
            select(Skill)
            .where(
                and_(
                    Skill.tenant_id == tenant_id,
                    Skill.verification_status == "verified",
                )
            )
            .order_by(Skill.created_at.asc())
        )
        s_res = await session.execute(s_stmt)
        all_verified_skills = s_res.scalars().all()

        snapshot_sections = snapshot.get("sections") or {}
        skills_to_include = []
        if isinstance(snapshot_sections.get("skills"), list):
            wanted = {str(w).lower() for w in snapshot_sections["skills"]}
            skills_to_include = [s for s in all_verified_skills if s.name.lower() in wanted]
        if not skills_to_include:
            skills_to_include = list(all_verified_skills)

        # 5. Experiencias verificadas
        e_stmt = (
            select(Experience)
            .where(
                and_(
                    Experience.tenant_id == tenant_id,
                    Experience.verification_status == "verified",
                )
            )
            .order_by(Experience.start_date.desc().nullslast())
        )
        e_res = await session.execute(e_stmt)
        verified_experiences = e_res.scalars().all()

        location_str = ""
        if profile and profile.location_city and profile.location_country:
            location_str = f"{profile.location_city}, {profile.location_country}"
        elif profile and profile.location_city:
            location_str = profile.location_city

        cv_payload = {
            "full_name": snapshot.get("name") or (profile.full_name if profile else "Candidato"),
            "email": snapshot.get("email") or (profile.email if profile else ""),
            "phone": profile.phone if profile else None,
            "location": location_str,
            "specialization": snapshot.get("title") or "Candidatura Especializada",
            "summary": snapshot.get("summary") or "",
            "skills": [
                {
                    "name": s.name,
                    "category": s.category,
                    "proficiency": s.proficiency,
                    "years_experience": s.years_experience,
                }
                for s in skills_to_include
            ],
            "experiences": [
                {
                    "company": e.company,
                    "title": e.title,
                    "start_date": str(e.start_date) if e.start_date else None,
                    "end_date": str(e.end_date) if e.end_date else None,
                    "description": e.description,
                    "skills_used": e.skills_used if isinstance(e.skills_used, list) else [],
                }
                for e in verified_experiences
            ],
        }

        # 6. Generar documento
        if fmt == "pdf":
            content_bytes = CVDocumentGenerator.generate_pdf(cv_payload)
        else:
            content_bytes = CVDocumentGenerator.generate_docx(cv_payload)

        file_hash = hashlib.sha256(content_bytes).hexdigest()
        media_type = cls.MEDIA_TYPES[fmt]
        filename = f"Candidatura_{candidacy.id[:8]}_CV_v{candidacy.version}.{fmt}"

        # 7. Inmutabilidad y vinculación documental (REQ-013, ADR-003)
        doc_stmt = select(CandidacyDocument).where(
            and_(
                CandidacyDocument.candidacy_id == candidacy.id,
                CandidacyDocument.document_type == doc_type,
                CandidacyDocument.version == candidacy.version,
                CandidacyDocument.tenant_id == tenant_id,
            )
        )
        doc_res = await session.execute(doc_stmt)
        existing_doc = doc_res.scalar_one_or_none()

        is_approved_or_later = candidacy.status in {
            "approved",
            "scheduled",
            "executing",
            "sent_confirmed",
        }

        # Si el documento ya fue generado y registrado para esta versión
        if existing_doc and os.path.exists(existing_doc.file_path):
            with open(existing_doc.file_path, "rb") as f:
                saved_bytes = f.read()
            # Verificar integridad estricta del archivo persistido contra el hash registrado
            saved_hash = hashlib.sha256(saved_bytes).hexdigest()
            if is_approved_or_later and saved_hash != existing_doc.file_hash:
                raise CVServiceImmutableError(
                    "Integridad comprometida: el archivo en disco no coincide con el hash del paquete aprobado."
                )
            return saved_bytes, filename, media_type, existing_doc.file_hash

        # Si no existía aún en disco, persistir el nuevo documento
        docs_dir = os.path.join("data", "documents", tenant_id)
        os.makedirs(docs_dir, exist_ok=True)
        local_path = os.path.join(docs_dir, filename)
        with open(local_path, "wb") as f:
            f.write(content_bytes)

        if not existing_doc:
            cand_doc = CandidacyDocument(
                tenant_id=tenant_id,
                candidacy_id=candidacy.id,
                document_type=doc_type,
                file_path=local_path,
                file_hash=file_hash,
                version=candidacy.version,
            )
            session.add(cand_doc)
            await session.commit()
        else:
            existing_doc.file_path = local_path
            existing_doc.file_hash = file_hash
            await session.commit()

        return content_bytes, filename, media_type, file_hash
