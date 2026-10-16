"""
Servicio para gestión de fuentes profesionales y revisión humana (Hito 2.1).
Principios clave:
1. Extraer no equivale a verificar (verification_status obligatorio, 'pending' por defecto).
2. Procedencia obligatoria por dato (source_id vinculante).
3. Aceptación / corrección / rechazo explícitos por el usuario.
4. Aislamiento estricto por tenant_id en todas las consultas y mutaciones.
5. Ningún hecho sin verificar ('pending' o 'rejected') se incorpora a candidaturas.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.profile.models import UserProfile, ProfessionalSource, Skill, Experience
from backend.app.modules.profile.parser import DocumentParser, InvalidDocumentError


class ProfileSourceService:
    """Servicio de gestión y verificación de fuentes profesionales."""

    @classmethod
    async def import_cv_document(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
        filename: str,
        file_bytes: bytes,
    ) -> Dict[str, Any]:
        """
        Importa un documento de CV (PDF/DOCX/TXT), extrae entidades y las guarda
        con estado 'pending' y procedencia 'source_id'.
        """
        # 1. Verificar que el perfil pertenece al tenant
        stmt_prof = select(UserProfile).where(
            and_(UserProfile.id == profile_id, UserProfile.tenant_id == tenant_id)
        )
        res_prof = await session.execute(stmt_prof)
        profile = res_prof.scalar_one_or_none()
        if not profile:
            raise ValueError(f"Perfil {profile_id} no encontrado en el tenant {tenant_id}.")

        # 2. Parseo y extracción determinista (fail-closed)
        extracted_doc = DocumentParser.parse(file_bytes=file_bytes, filename=filename)

        # 3. Determinar el tipo de fuente
        ext = filename.lower().split(".")[-1] if "." in filename else "raw"
        source_type = f"cv_import_{ext}"

        # 4. Crear registro inmutable de procedencia (ProfessionalSource)
        source = ProfessionalSource(
            tenant_id=tenant_id,
            profile_id=profile.id,
            source_type=source_type,
            source_name=filename,
            source_uri=f"local://uploads/{filename}",
            file_hash=extracted_doc.file_hash,
            raw_text=extracted_doc.raw_text,
            extracted_data={
                "suggested_profile": extracted_doc.suggested_profile,
                "injection_signals": extracted_doc.injection_signals,
                "extracted_skills_count": len(extracted_doc.skills),
                "extracted_experiences_count": len(extracted_doc.experiences),
            },
            verification_status="pending",  # Extracción != Verificación
        )
        session.add(source)
        await session.flush()

        # 5. Guardar habilidades extraídas con procedencia obligatoria y estado pending
        created_skills = []
        for es in extracted_doc.skills:
            skill = Skill(
                tenant_id=tenant_id,
                profile_id=profile.id,
                source_id=source.id,
                name=es.name,
                category=es.category,
                proficiency=es.proficiency,
                years_experience=es.years_experience,
                verification_status="pending",  # Pendiente de revisión humana
            )
            session.add(skill)
            created_skills.append(skill)

        # 6. Guardar experiencias extraídas con procedencia obligatoria y estado pending
        created_experiences = []
        for ee in extracted_doc.experiences:
            # Parsear start_date y end_date a objetos date
            try:
                s_date = datetime.strptime(ee.start_date, "%Y-%m-%d").date()
            except Exception:
                s_date = datetime.now(timezone.utc).date()

            e_date = None
            if ee.end_date:
                try:
                    e_date = datetime.strptime(ee.end_date, "%Y-%m-%d").date()
                except Exception:
                    e_date = None

            exp = Experience(
                tenant_id=tenant_id,
                profile_id=profile.id,
                source_id=source.id,
                company=ee.company,
                title=ee.title,
                start_date=s_date,
                end_date=e_date,
                description=ee.description,
                skills_used=ee.skills_used,
                verification_status="pending",  # Pendiente de revisión humana
            )
            session.add(exp)
            created_experiences.append(exp)

        await session.flush()

        return {
            "source": {
                "id": source.id,
                "tenant_id": source.tenant_id,
                "source_name": source.source_name,
                "source_type": source.source_type,
                "file_hash": source.file_hash,
                "verification_status": source.verification_status,
                "suggested_profile": extracted_doc.suggested_profile,
            },
            "pending_skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "category": s.category,
                    "proficiency": s.proficiency,
                    "years_experience": s.years_experience,
                    "source_id": s.source_id,
                    "verification_status": s.verification_status,
                }
                for s in created_skills
            ],
            "pending_experiences": [
                {
                    "id": e.id,
                    "company": e.company,
                    "title": e.title,
                    "start_date": str(e.start_date),
                    "end_date": str(e.end_date) if e.end_date else None,
                    "description": e.description,
                    "source_id": e.source_id,
                    "verification_status": e.verification_status,
                }
                for e in created_experiences
            ],
        }

    @classmethod
    async def review_skill(
        cls,
        session: AsyncSession,
        tenant_id: str,
        skill_id: str,
        decision: str,  # "accept", "reject", "modify"
        modifications: Optional[Dict[str, Any]] = None,
    ) -> Skill:
        """
        Revisión humana explícita de una habilidad extraída.
        Decisión:
        - 'accept': pasa a 'verified'
        - 'reject': pasa a 'rejected' (excluida para siempre)
        - 'modify': aplica correcciones del usuario y pasa a 'verified'
        """
        stmt = select(Skill).where(and_(Skill.id == skill_id, Skill.tenant_id == tenant_id))
        res = await session.execute(stmt)
        skill = res.scalar_one_or_none()
        if not skill:
            raise KeyError(f"Habilidad {skill_id} no encontrada en tenant {tenant_id}.")

        if decision == "accept":
            skill.verification_status = "verified"
        elif decision == "reject":
            skill.verification_status = "rejected"
        elif decision == "modify":
            if modifications:
                if "name" in modifications and modifications["name"].strip():
                    skill.name = modifications["name"].strip()
                if "category" in modifications:
                    skill.category = modifications["category"]
                if "proficiency" in modifications:
                    skill.proficiency = modifications["proficiency"]
                if "years_experience" in modifications:
                    skill.years_experience = float(modifications["years_experience"])
            skill.verification_status = "verified"
        else:
            raise ValueError(f"Decisión inválida '{decision}'. Opciones: accept, reject, modify.")

        await session.flush()
        return skill

    @classmethod
    async def review_experience(
        cls,
        session: AsyncSession,
        tenant_id: str,
        experience_id: str,
        decision: str,  # "accept", "reject", "modify"
        modifications: Optional[Dict[str, Any]] = None,
    ) -> Experience:
        """
        Revisión humana explícita de una experiencia laboral extraída.
        """
        stmt = select(Experience).where(
            and_(Experience.id == experience_id, Experience.tenant_id == tenant_id)
        )
        res = await session.execute(stmt)
        exp = res.scalar_one_or_none()
        if not exp:
            raise KeyError(f"Experiencia {experience_id} no encontrada en tenant {tenant_id}.")

        if decision == "accept":
            exp.verification_status = "verified"
        elif decision == "reject":
            exp.verification_status = "rejected"
        elif decision == "modify":
            if modifications:
                if "company" in modifications and modifications["company"].strip():
                    exp.company = modifications["company"].strip()
                if "title" in modifications and modifications["title"].strip():
                    exp.title = modifications["title"].strip()
                if "description" in modifications:
                    exp.description = modifications["description"]
                if "start_date" in modifications and modifications["start_date"]:
                    try:
                        exp.start_date = datetime.strptime(modifications["start_date"], "%Y-%m-%d").date()
                    except Exception:
                        pass
                if "end_date" in modifications:
                    if modifications["end_date"]:
                        try:
                            exp.end_date = datetime.strptime(modifications["end_date"], "%Y-%m-%d").date()
                        except Exception:
                            pass
                    else:
                        exp.end_date = None
            exp.verification_status = "verified"
        else:
            raise ValueError(f"Decisión inválida '{decision}'. Opciones: accept, reject, modify.")

        await session.flush()
        return exp

    @classmethod
    async def get_verified_skills_only(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
    ) -> List[Skill]:
        """
        Garantía estricta: Retorna ÚNICAMENTE habilidades verificadas.
        Cualquier habilidad con estado 'pending', 'extracted' o 'rejected' es excluida.
        """
        stmt = select(Skill).where(
            and_(
                Skill.tenant_id == tenant_id,
                Skill.profile_id == profile_id,
                Skill.verification_status == "verified",
            )
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @classmethod
    async def get_verified_experiences_only(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
    ) -> List[Experience]:
        """
        Garantía estricta: Retorna ÚNICAMENTE experiencias verificadas.
        """
        stmt = select(Experience).where(
            and_(
                Experience.tenant_id == tenant_id,
                Experience.profile_id == profile_id,
                Experience.verification_status == "verified",
            )
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @classmethod
    async def list_sources(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
    ) -> List[ProfessionalSource]:
        """Lista todas las fuentes importadas de un perfil en el tenant."""
        stmt = (
            select(ProfessionalSource)
            .where(
                and_(
                    ProfessionalSource.tenant_id == tenant_id,
                    ProfessionalSource.profile_id == profile_id,
                )
            )
            .order_by(ProfessionalSource.created_at.desc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @classmethod
    async def get_pending_items(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
    ) -> Dict[str, Any]:
        """Obtiene todas las habilidades y experiencias con estado 'pending' que requieren revisión."""
        s_stmt = select(Skill).where(
            and_(
                Skill.tenant_id == tenant_id,
                Skill.profile_id == profile_id,
                Skill.verification_status == "pending",
            )
        )
        s_res = await session.execute(s_stmt)
        pending_skills = list(s_res.scalars().all())

        e_stmt = select(Experience).where(
            and_(
                Experience.tenant_id == tenant_id,
                Experience.profile_id == profile_id,
                Experience.verification_status == "pending",
            )
        )
        e_res = await session.execute(e_stmt)
        pending_experiences = list(e_res.scalars().all())

        return {
            "pending_skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "category": s.category,
                    "proficiency": s.proficiency,
                    "years_experience": s.years_experience,
                    "source_id": s.source_id,
                    "verification_status": s.verification_status,
                }
                for s in pending_skills
            ],
            "pending_experiences": [
                {
                    "id": e.id,
                    "company": e.company,
                    "title": e.title,
                    "start_date": str(e.start_date),
                    "end_date": str(e.end_date) if e.end_date else None,
                    "description": e.description,
                    "source_id": e.source_id,
                    "verification_status": e.verification_status,
                }
                for e in pending_experiences
            ],
        }
