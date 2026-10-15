"""
Router para perfil profesional y CVs maestros (REQ-001, REQ-004).
Permite configurar el perfil y CVs sin editar código.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV

router = APIRouter(prefix="/profile", tags=["Profile"])


# Schemas
class SkillCreate(BaseModel):
    name: str
    category: str = "technical"
    proficiency: str = "intermediate"
    years_experience: float = 1.0
    verification_status: str = "extracted"  # extracted, verified, rejected


class MasterCVCreate(BaseModel):
    name: str
    specialization: str
    summary: str = ""
    template_id: str = "standard_ats"
    sections: Dict[str, Any] = {}


class ProfileCreateOrUpdate(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    location_city: str
    location_country: str
    work_countries: List[str] = []
    availability: str = "immediate"
    job_preferences: Dict[str, Any] = {}
    languages: List[Dict[str, str]] = []


@router.post("", status_code=status.HTTP_200_OK)
async def create_or_update_profile(
    payload: ProfileCreateOrUpdate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Crea o actualiza el perfil central del usuario para el tenant activo."""
    stmt = select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    res = await session.execute(stmt)
    profile = res.scalar_one_or_none()

    if profile is None:
        profile = UserProfile(
            tenant_id=tenant_id,
            full_name=payload.full_name,
            email=str(payload.email),
            phone=payload.phone,
            location_city=payload.location_city,
            location_country=payload.location_country,
            work_countries=payload.work_countries,
            availability=payload.availability,
            job_preferences=payload.job_preferences,
            languages=payload.languages,
            verification_status="pending",
        )
        session.add(profile)
    else:
        profile.full_name = payload.full_name
        profile.email = str(payload.email)
        profile.phone = payload.phone
        profile.location_city = payload.location_city
        profile.location_country = payload.location_country
        profile.work_countries = payload.work_countries
        profile.availability = payload.availability
        profile.job_preferences = payload.job_preferences
        profile.languages = payload.languages

    await session.commit()
    await session.refresh(profile)
    return {
        "id": profile.id,
        "tenant_id": profile.tenant_id,
        "full_name": profile.full_name,
        "email": profile.email,
        "location_city": profile.location_city,
        "location_country": profile.location_country,
        "verification_status": profile.verification_status,
    }


@router.get("")
async def get_profile(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene el perfil central junto a sus habilidades y CVs maestros."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado para este tenant.")

    s_res = await session.execute(
        select(Skill).where(Skill.profile_id == profile.id, Skill.tenant_id == tenant_id)
    )
    skills = s_res.scalars().all()

    cv_res = await session.execute(
        select(MasterCV).where(MasterCV.profile_id == profile.id, MasterCV.tenant_id == tenant_id)
    )
    cvs = cv_res.scalars().all()

    return {
        "profile": {
            "id": profile.id,
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location_city": profile.location_city,
            "location_country": profile.location_country,
            "work_countries": profile.work_countries,
            "availability": profile.availability,
            "verification_status": profile.verification_status,
        },
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "category": s.category,
                "proficiency": s.proficiency,
                "years_experience": s.years_experience,
                "verification_status": s.verification_status,
            }
            for s in skills
        ],
        "master_cvs": [
            {
                "id": c.id,
                "name": c.name,
                "specialization": c.specialization,
                "summary": c.summary,
                "version": c.version,
            }
            for c in cvs
        ],
    }


@router.post("/skills", status_code=status.HTTP_201_CREATED)
async def add_skill(
    payload: SkillCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Añade una habilidad al perfil con trazabilidad de verificación."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado. Cree el perfil primero.")

    skill = Skill(
        tenant_id=tenant_id,
        profile_id=profile.id,
        name=payload.name,
        category=payload.category,
        proficiency=payload.proficiency,
        years_experience=payload.years_experience,
        verification_status=payload.verification_status,
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    return {
        "id": skill.id,
        "name": skill.name,
        "category": skill.category,
        "proficiency": skill.proficiency,
        "years_experience": skill.years_experience,
        "verification_status": skill.verification_status,
    }


@router.post("/cv", status_code=status.HTTP_201_CREATED)
async def create_master_cv(
    payload: MasterCVCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Crea un nuevo CV maestro especializado asociado al perfil (REQ-004)."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado. Cree el perfil primero.")

    cv = MasterCV(
        tenant_id=tenant_id,
        profile_id=profile.id,
        name=payload.name,
        specialization=payload.specialization,
        summary=payload.summary,
        template_id=payload.template_id,
        sections=payload.sections,
        version=1,
    )
    session.add(cv)
    await session.commit()
    await session.refresh(cv)
    return {
        "id": cv.id,
        "name": cv.name,
        "specialization": cv.specialization,
        "summary": cv.summary,
        "version": cv.version,
    }
