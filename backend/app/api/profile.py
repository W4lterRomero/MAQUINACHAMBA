"""
Router para perfil profesional y CVs maestros (REQ-001, REQ-004).
Permite configurar el perfil y CVs sin editar código.
"""

import base64
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.modules.profile.models import UserProfile, Skill, Experience, ProfessionalSource
from backend.app.modules.profile.parser import InvalidDocumentError, MAX_CV_FILE_SIZE
from backend.app.modules.profile.service import ProfileSourceService
from backend.app.modules.profile.github_service import GitHubImportService, GitHubImportError
from backend.app.modules.cv.models import MasterCV

router = APIRouter(prefix="/profile", tags=["Profile"])


# Schemas
class GitHubImportRequest(BaseModel):
    username: str
    access_token: Optional[str] = None
    max_repos: int = 5
    raw_json_repos: Optional[List[Dict[str, Any]]] = None


class SkillCreate(BaseModel):
    name: str
    category: str = "technical"
    proficiency: Optional[str] = None
    years_experience: Optional[float] = None
    verification_status: str = "pending"  # pending, verified, rejected


class ItemReviewRequest(BaseModel):
    decision: str  # accept, reject, modify
    modifications: Optional[Dict[str, Any]] = None


class CVImportJsonRequest(BaseModel):
    filename: Optional[str] = None
    source_name: Optional[str] = None
    content_base64: Optional[str] = None
    raw_text: Optional[str] = None


class MasterCVCreate(BaseModel):
    name: str
    specialization: str
    summary: str = ""
    template_id: str = "standard_ats"
    sections: Dict[str, Any] = {}


class MasterCVUpdate(BaseModel):
    name: Optional[str] = None
    specialization: Optional[str] = None
    summary: Optional[str] = None
    template_id: Optional[str] = None
    sections: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None



class ProfileCreateOrUpdate(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    location_city: Optional[str] = "No especificada"
    location_country: Optional[str] = "No especificado"
    work_countries: List[str] = []
    availability: str = "immediate"
    job_preferences: Dict[str, Any] = {}
    languages: List[Dict[str, str]] = []
    verification_status: Optional[str] = None


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

    init_status = payload.verification_status or "pending"

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
            verification_status=init_status,
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
        if payload.verification_status:
            profile.verification_status = payload.verification_status

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
    """Obtiene el perfil central junto a sus habilidades, experiencias y CVs maestros."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado para este tenant.")

    s_res = await session.execute(
        select(Skill).where(
            and_(
                Skill.profile_id == profile.id,
                Skill.tenant_id == tenant_id,
                Skill.verification_status == "verified",
            )
        )
    )
    skills = s_res.scalars().all()

    e_res = await session.execute(
        select(Experience).where(
            and_(
                Experience.profile_id == profile.id,
                Experience.tenant_id == tenant_id,
                Experience.verification_status == "verified",
            )
        )
    )
    experiences = e_res.scalars().all()

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
                "source_fragment": s.source_fragment,
                "verification_status": s.verification_status,
            }
            for s in skills
        ],
        "experiences": [
            {
                "id": e.id,
                "company": e.company,
                "title": e.title,
                "start_date": str(e.start_date) if e.start_date else None,
                "end_date": str(e.end_date) if e.end_date else None,
                "date_precision": e.date_precision,
                "description": e.description,
                "source_fragment": e.source_fragment,
                "verification_status": e.verification_status,
            }
            for e in experiences
        ],
        "master_cvs": [
            {
                "id": c.id,
                "name": c.name,
                "specialization": c.specialization,
                "summary": c.summary,
                "template_id": c.template_id,
                "sections": c.sections or {},
                "is_active": c.is_active,
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
        "template_id": cv.template_id,
        "sections": cv.sections or {},
        "is_active": cv.is_active,
        "version": cv.version,
    }


@router.get("/cv", status_code=status.HTTP_200_OK)
async def list_master_cvs(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista todos los CVs maestros especializados del tenant."""
    stmt = select(MasterCV).where(MasterCV.tenant_id == tenant_id).order_by(MasterCV.created_at.desc())
    res = await session.execute(stmt)
    cvs = res.scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "specialization": c.specialization,
            "summary": c.summary,
            "template_id": c.template_id,
            "sections": c.sections or {},
            "is_active": c.is_active,
            "version": c.version,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in cvs
    ]


@router.get("/cv/{cv_id}", status_code=status.HTTP_200_OK)
async def get_master_cv_detail(
    cv_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene el detalle de un CV maestro con sus habilidades y experiencias verificadas vinculadas."""
    stmt = select(MasterCV).where(and_(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id))
    res = await session.execute(stmt)
    cv = res.scalar_one_or_none()
    if not cv:
        raise HTTPException(status_code=404, detail="CV Maestro no encontrado.")

    s_stmt = select(Skill).where(and_(Skill.profile_id == cv.profile_id, Skill.tenant_id == tenant_id, Skill.verification_status == "verified"))
    s_res = await session.execute(s_stmt)
    skills = s_res.scalars().all()

    e_stmt = select(Experience).where(and_(Experience.profile_id == cv.profile_id, Experience.tenant_id == tenant_id, Experience.verification_status == "verified"))
    e_res = await session.execute(e_stmt)
    experiences = e_res.scalars().all()

    return {
        "cv": {
            "id": cv.id,
            "name": cv.name,
            "specialization": cv.specialization,
            "summary": cv.summary,
            "template_id": cv.template_id,
            "sections": cv.sections or {},
            "is_active": cv.is_active,
            "version": cv.version,
        },
        "verified_skills": [
            {
                "id": s.id,
                "name": s.name,
                "category": s.category,
                "proficiency": s.proficiency,
                "years_experience": s.years_experience,
                "source_fragment": s.source_fragment,
            }
            for s in skills
        ],
        "verified_experiences": [
            {
                "id": e.id,
                "company": e.company,
                "title": e.title,
                "start_date": str(e.start_date) if e.start_date else None,
                "end_date": str(e.end_date) if e.end_date else None,
                "date_precision": e.date_precision,
                "description": e.description,
                "source_fragment": e.source_fragment,
            }
            for e in experiences
        ],
    }


@router.put("/cv/{cv_id}", status_code=status.HTTP_200_OK)
async def update_master_cv(
    cv_id: str,
    payload: MasterCVUpdate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Actualiza un CV maestro especializado e incrementa su versión (REQ-004)."""
    stmt = select(MasterCV).where(and_(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id))
    res = await session.execute(stmt)
    cv = res.scalar_one_or_none()
    if not cv:
        raise HTTPException(status_code=404, detail="CV Maestro no encontrado.")

    if payload.name is not None and payload.name.strip():
        cv.name = payload.name.strip()
    if payload.specialization is not None and payload.specialization.strip():
        cv.specialization = payload.specialization.strip()
    if payload.summary is not None:
        cv.summary = payload.summary.strip()
    if payload.template_id is not None:
        cv.template_id = payload.template_id
    if payload.sections is not None:
        cv.sections = payload.sections
    if payload.is_active is not None:
        cv.is_active = payload.is_active

    cv.version += 1
    await session.commit()
    await session.refresh(cv)
    return {
        "id": cv.id,
        "name": cv.name,
        "specialization": cv.specialization,
        "summary": cv.summary,
        "template_id": cv.template_id,
        "sections": cv.sections,
        "is_active": cv.is_active,
        "version": cv.version,
    }


@router.delete("/cv/{cv_id}", status_code=status.HTTP_200_OK)
async def delete_master_cv(
    cv_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Elimina un CV maestro especializado del tenant."""
    stmt = select(MasterCV).where(and_(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id))
    res = await session.execute(stmt)
    cv = res.scalar_one_or_none()
    if not cv:
        raise HTTPException(status_code=404, detail="CV Maestro no encontrado.")

    await session.delete(cv)
    await session.commit()
    return {"status": "deleted", "id": cv_id}


@router.post("/cv/{cv_id}/select", status_code=status.HTTP_200_OK)
async def select_master_cv(
    cv_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Marca un CV maestro como el activo para candidaturas y desactiva los demás."""
    stmt = select(MasterCV).where(and_(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id))
    res = await session.execute(stmt)
    cv = res.scalar_one_or_none()
    if not cv:
        raise HTTPException(status_code=404, detail="CV Maestro no encontrado.")

    all_stmt = select(MasterCV).where(and_(MasterCV.tenant_id == tenant_id, MasterCV.profile_id == cv.profile_id))
    all_res = await session.execute(all_stmt)
    for other_cv in all_res.scalars().all():
        other_cv.is_active = (other_cv.id == cv_id)

    await session.commit()
    await session.refresh(cv)
    return {"status": "active", "id": cv.id, "name": cv.name}


@router.get("/cv/{cv_id}/export/{format_type}")
async def export_master_cv(
    cv_id: str,
    format_type: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Exporta un CV maestro en PDF o DOCX con encabezados y hash SHA-256 (REQ-013).
    """
    from backend.app.modules.cv.service import (
        CVService,
        CVServiceError,
        CVServiceNotFoundError,
    )
    from fastapi import Response

    try:
        content_bytes, filename, media_type, file_hash = await CVService.export_master_cv(
            session=session,
            tenant_id=tenant_id,
            cv_id=cv_id,
            format_type=format_type,
        )
        return Response(
            content=content_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Document-Hash": file_hash,
            },
        )
    except CVServiceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CVServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verify", status_code=status.HTTP_200_OK)
async def mark_profile_verified(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Marca el perfil como verificado tras revisión humana satisfactoria."""
    stmt = select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    res = await session.execute(stmt)
    profile = res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado.")
    profile.verification_status = "verified"
    await session.commit()
    await session.refresh(profile)
    return {"status": "verified", "profile_id": profile.id}



# ==============================================================================
# HITO 2.1: IMPORTACIÓN DE CV Y REVISIÓN HUMANA (REQ-002, D12)
# ==============================================================================

@router.post("/sources/upload", status_code=status.HTTP_201_CREATED)
async def upload_cv_document(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Importa un archivo de CV (PDF, DOCX, TXT) mediante multipart/form-data.
    Aplica lectura acotada en streaming para prevenir agotamiento de memoria (P1).
    Extrae entidades con estado 'pending' y procedencia vinculada (Hito 2.1).
    """
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado. Cree el perfil primero.")

    # Lectura acotada en chunks de 64 KB (máximo 5 MB en total)
    chunk_size = 64 * 1024
    buffer = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_CV_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"El archivo excede el tamaño máximo permitido de 5 MB ({len(buffer)} bytes).",
            )

    file_bytes = bytes(buffer)
    try:
        result = await ProfileSourceService.import_cv_document(
            session=session,
            tenant_id=tenant_id,
            profile_id=profile.id,
            filename=file.filename or "uploaded_cv.txt",
            file_bytes=file_bytes,
        )
        await session.commit()
        return result
    except InvalidDocumentError as ide:
        raise HTTPException(status_code=400, detail=str(ide))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@router.post("/sources/import", status_code=status.HTTP_201_CREATED)
async def import_cv_json(
    payload: CVImportJsonRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Importa un documento de CV a través de JSON (base64 o texto plano).
    Aplica límites estrictos de tamaño previo a la decodificación (P1).
    """
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado. Cree el perfil primero.")

    if payload.content_base64:
        # Límite previo en longitud de la cadena base64: 5 MB en base64 son ~6.7 MB
        max_b64_len = (MAX_CV_FILE_SIZE * 4 // 3) + 1024
        if len(payload.content_base64) > max_b64_len:
            raise HTTPException(
                status_code=400,
                detail="El contenido base64 excede el tamaño máximo permitido de 5 MB.",
            )
        try:
            file_bytes = base64.b64decode(payload.content_base64)
        except Exception:
            raise HTTPException(status_code=400, detail="Formato base64 inválido.")
        if len(file_bytes) > MAX_CV_FILE_SIZE:
            raise HTTPException(status_code=400, detail="El archivo excede el tamaño máximo permitido de 5 MB.")
    elif payload.raw_text is not None:
        file_bytes = payload.raw_text.encode("utf-8")
        if len(file_bytes) > MAX_CV_FILE_SIZE:
            raise HTTPException(status_code=400, detail="El texto excede el tamaño máximo permitido de 5 MB.")
    else:
        raise HTTPException(status_code=400, detail="Debe proporcionar content_base64 o raw_text.")

    try:
        result = await ProfileSourceService.import_cv_document(
            session=session,
            tenant_id=tenant_id,
            profile_id=profile.id,
            filename=payload.filename or payload.source_name or "cv_document.txt",
            file_bytes=file_bytes,
        )
        await session.commit()
        return result
    except InvalidDocumentError as ide:
        raise HTTPException(status_code=400, detail=str(ide))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@router.post("/sources/github/import", status_code=status.HTTP_201_CREATED)
async def import_github_repositories(
    payload: GitHubImportRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Importa repositorios y proyectos de GitHub con permisos mínimos (REQ-003, D12).
    Genera entidades Skill y Experience en estado 'pending' para revisión humana.
    """
    p_res = await session.execute(
        select(UserProfile)
        .where(UserProfile.tenant_id == tenant_id)
        .order_by(UserProfile.created_at.desc())
    )
    profile = p_res.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado. Cree el perfil primero.")

    try:
        result = await GitHubImportService.import_github_profile(
            session=session,
            tenant_id=tenant_id,
            profile_id=profile.id,
            username=payload.username,
            token=payload.access_token,
            max_repos=payload.max_repos,
            raw_json_repos=payload.raw_json_repos,
        )
        await session.commit()
        return result
    except GitHubImportError as gie:
        raise HTTPException(status_code=400, detail=str(gie))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@router.get("/sources")
async def list_sources(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista las fuentes profesionales importadas del usuario."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado.")

    sources = await ProfileSourceService.list_sources(
        session=session,
        tenant_id=tenant_id,
        profile_id=profile.id,
    )
    return [
        {
            "id": s.id,
            "source_name": s.source_name,
            "source_type": s.source_type,
            "file_hash": s.file_hash,
            "verification_status": s.verification_status,
            "extracted_data": s.extracted_data,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sources
    ]


@router.get("/sources/{source_id}")
async def get_source_detail(
    source_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene el detalle de una fuente profesional y sus elementos extraídos."""
    stmt = select(ProfessionalSource).where(
        and_(ProfessionalSource.id == source_id, ProfessionalSource.tenant_id == tenant_id)
    )
    res = await session.execute(stmt)
    source = res.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Fuente profesional no encontrada en el tenant.")

    # Obtener skills y experiencias asociadas a la fuente
    s_stmt = select(Skill).where(
        and_(Skill.source_id == source.id, Skill.tenant_id == tenant_id)
    )
    s_res = await session.execute(s_stmt)
    skills = list(s_res.scalars().all())

    e_stmt = select(Experience).where(
        and_(Experience.source_id == source.id, Experience.tenant_id == tenant_id)
    )
    e_res = await session.execute(e_stmt)
    experiences = list(e_res.scalars().all())

    return {
        "id": source.id,
        "source_name": source.source_name,
        "source_type": source.source_type,
        "file_hash": source.file_hash,
        "verification_status": source.verification_status,
        "extracted_data": source.extracted_data,
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
        "experiences": [
            {
                "id": e.id,
                "company": e.company,
                "title": e.title,
                "start_date": str(e.start_date),
                "end_date": str(e.end_date) if e.end_date else None,
                "description": e.description,
                "verification_status": e.verification_status,
            }
            for e in experiences
        ],
    }


@router.get("/items/pending")
async def get_pending_review_items(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista todos los elementos pendientes de revisión humana para el perfil."""
    p_res = await session.execute(
        select(UserProfile).where(UserProfile.tenant_id == tenant_id)
    )
    profile = p_res.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no configurado.")

    return await ProfileSourceService.get_pending_items(
        session=session,
        tenant_id=tenant_id,
        profile_id=profile.id,
    )


@router.post("/items/skills/{skill_id}/review")
async def review_skill_item(
    skill_id: str,
    payload: ItemReviewRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Revisión humana de habilidad: aceptar, rechazar o modificar.
    Aceptada/Modificada -> 'verified'. Rechazada -> 'rejected'.
    """
    try:
        skill = await ProfileSourceService.review_skill(
            session=session,
            tenant_id=tenant_id,
            skill_id=skill_id,
            decision=payload.decision,
            modifications=payload.modifications,
        )
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
    except KeyError:
        raise HTTPException(status_code=404, detail="Habilidad no encontrada en el tenant.")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@router.post("/items/experiences/{experience_id}/review")
async def review_experience_item(
    experience_id: str,
    payload: ItemReviewRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Revisión humana de experiencia laboral: aceptar, rechazar o modificar.
    """
    try:
        exp = await ProfileSourceService.review_experience(
            session=session,
            tenant_id=tenant_id,
            experience_id=experience_id,
            decision=payload.decision,
            modifications=payload.modifications,
        )
        await session.commit()
        await session.refresh(exp)
        return {
            "id": exp.id,
            "company": exp.company,
            "title": exp.title,
            "start_date": str(exp.start_date),
            "end_date": str(exp.end_date) if exp.end_date else None,
            "verification_status": exp.verification_status,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Experiencia no encontrada en el tenant.")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

