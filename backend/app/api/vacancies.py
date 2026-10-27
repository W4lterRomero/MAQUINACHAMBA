"""
Router para vacantes de empleo, ingestión multifuente y búsqueda automatizada (REQ-007, REQ-008, REQ-009, REQ-030).
Deduplicación por hash, trazabilidad de origen (ChatGPT / Gemini / Manual / Conectores directos),
preferencias persistentes, detección de vacantes cerradas o sospechosas y clasificación determinista contra el perfil.
"""

from datetime import date, datetime, timezone
import hashlib
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.security import validate_url
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.vacancies.connectors.remotive import RemotiveJobConnector
from backend.app.modules.vacancies.models import ImportBatch, JobSearchPreferences, Vacancy
from backend.app.modules.vacancies.search_service import JobSearchService
from backend.app.modules.vacancies.service import VacancyIntegrityService
from backend.app.modules.vacancies.ingestion import JobIngestionService, canonicalize_url, compute_dedup_hash, InsufficientContentError
from backend.app.modules.vacancies.email_connector import EmailJobSearchConnector

router = APIRouter(prefix="/vacancies", tags=["Vacancies"])


class VacancyCreate(BaseModel):
    company_name: str
    job_title: str
    source_url: str
    source: str = "manual"
    location: str = "Remoto"
    remote_type: str = "remote"  # remote, onsite, hybrid, unknown
    allowed_countries: List[str] = []
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = "USD"
    requirements: List[str] = []
    description: str = ""
    closing_date: Optional[date] = None


class UniversalImportRequest(BaseModel):
    provider: str = Field(default="chatgpt", description="chatgpt, gemini, manual")
    content: str = Field(..., description="Reporte en Markdown, JSON, CSV o Texto")
    format_hint: Optional[str] = Field(default=None, description="markdown, json, csv, text")


class StructuredJobItem(BaseModel):
    company_name: str
    job_title: str
    source_url: str
    source: Optional[str] = "api"
    location: Optional[str] = "Remoto"
    remote_type: Optional[str] = "remote"
    allowed_countries: Optional[List[str]] = []
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = "USD"
    requirements: Optional[List[str]] = []
    experience_level: Optional[str] = "unknown"
    description: Optional[str] = ""
    original_snippet: Optional[str] = ""


class StructuredImportRequest(BaseModel):
    provider: str = Field(default="api", description="chatgpt, gemini, api")
    vacancies: List[StructuredJobItem]


class EmailSyncRequest(BaseModel):
    sender: str = "alerts@chatgpt.com"
    subject: str = "ChatGPT Daily Job Search"
    body: str


class PreferencesUpdate(BaseModel):
    desired_job_titles: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    remote_modalities: Optional[List[str]] = None
    experience_level: Optional[str] = None
    salary_min: Optional[float] = None
    salary_currency: Optional[str] = None
    excluded_keywords: Optional[List[str]] = None
    excluded_companies: Optional[List[str]] = None
    interval_hours: Optional[int] = None


class AuthorizeSourcePayload(BaseModel):
    source_name: str = "remotive"
    confirm: bool = True


class SearchRunPayload(BaseModel):
    force_dry_run: bool = False
    limit: int = Field(default=20, ge=1, le=50)


class TogglePeriodicPayload(BaseModel):
    enabled: bool
    interval_hours: Optional[int] = Field(default=None, ge=1, le=168)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_import_vacancy(
    payload: VacancyCreate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Registra o importa una vacante de empleo individual (manual o estructurada).
    Aplica validación de URL (anti-SSRF formato), deduplicación por hash determinista
    y análisis de integridad contra vacantes cerradas y señales de fraude (REQ-009).
    """
    is_safe, reason = validate_url(payload.source_url)
    if not is_safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL rechazada por política de seguridad: {reason}",
        )

    integrity = VacancyIntegrityService.analyze_vacancy(
        title=payload.job_title,
        description=payload.description,
        source_url=payload.source_url,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_currency=payload.salary_currency,
        closing_date=payload.closing_date,
    )

    clean_url = canonicalize_url(payload.source_url)
    dedup_hash = compute_dedup_hash(clean_url, payload.company_name, payload.job_title)

    existing_res = await session.execute(
        select(Vacancy).where(
            Vacancy.tenant_id == tenant_id,
            Vacancy.dedup_hash == dedup_hash,
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing:
        return {
            "id": existing.id,
            "status": "already_exists",
            "dedup_hash": existing.dedup_hash,
            "company_name": existing.company_name,
            "job_title": existing.job_title,
            "is_active": existing.is_active,
            "is_fraud_suspect": any(s.get("severity") == "high" for s in (existing.fraud_signals or [])),
            "fraud_signals": existing.fraud_signals or [],
            "closure_reason": existing.closure_reason,
        }

    now_dt = datetime.now(timezone.utc)
    vacancy = Vacancy(
        tenant_id=tenant_id,
        company_name=payload.company_name,
        job_title=payload.job_title,
        source=payload.source,
        source_url=clean_url,
        location=payload.location,
        remote_type=payload.remote_type,
        allowed_countries=payload.allowed_countries,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_currency=payload.salary_currency,
        requirements=payload.requirements,
        technical_requirements=payload.requirements,
        description=payload.description,
        original_snippet=f"[{payload.source.upper()}]: {payload.job_title} en {payload.company_name}",
        closing_date=payload.closing_date,
        discovered_date=now_dt.date(),
        posted_date=now_dt.date(),
        is_active=integrity["is_active"],
        verification_status="pending_verification",
        unknown_fields=[] if payload.salary_min else ["salary"],
        sources_detail=[{
            "source": payload.source,
            "discovered_at": now_dt.isoformat(),
            "snippet": f"{payload.job_title} en {payload.company_name}",
        }],
        fraud_signals=integrity["fraud_signals"],
        closure_reason=integrity["closure_reason"],
        dedup_hash=dedup_hash,
        fetched_at=now_dt,
        is_real_search_result=False,
    )
    session.add(vacancy)
    await session.commit()
    await session.refresh(vacancy)

    return {
        "id": vacancy.id,
        "status": "created",
        "dedup_hash": vacancy.dedup_hash,
        "company_name": vacancy.company_name,
        "job_title": vacancy.job_title,
        "location": vacancy.location,
        "remote_type": vacancy.remote_type,
        "is_active": vacancy.is_active,
        "is_fraud_suspect": integrity["is_fraud_suspect"],
        "fraud_signals": vacancy.fraud_signals,
        "is_closed": integrity["is_closed"],
        "closure_reason": vacancy.closure_reason,
    }


@router.post("/import/universal", status_code=status.HTTP_200_OK)
async def import_universal_vacancies(
    payload: UniversalImportRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Importación manual universal (Markdown, Texto, JSON, CSV).
    Parsea, valida URLs, detecta señales de fraude y deduplica consolidando fuentes (ChatGPT + Gemini).
    Falla de forma segura (insufficient_content) si el reporte solo contiene avisos o enlaces sin ofertas reales.
    """
    batch, vacancies = await JobIngestionService.process_and_import(
        session=session,
        tenant_id=tenant_id,
        provider=payload.provider,
        raw_content=payload.content,
        import_type="manual_paste",
        format_hint=payload.format_hint,
    )

    return {
        "batch_id": batch.id,
        "provider": batch.provider,
        "status": batch.status,
        "vacancies_found": batch.vacancies_found,
        "vacancies_imported": batch.vacancies_imported,
        "vacancies_deduplicated": batch.vacancies_deduplicated,
        "notes": batch.notes,
        "vacancies": [
            {
                "id": v.id,
                "company_name": v.company_name,
                "job_title": v.job_title,
                "source": v.source,
                "source_url": v.source_url,
                "location": v.location,
                "remote_type": v.remote_type,
                "allowed_countries": v.allowed_countries or [],
                "salary_currency": v.salary_currency,
                "salary_min": float(v.salary_min) if v.salary_min else None,
                "salary_max": float(v.salary_max) if v.salary_max else None,
                "requirements": v.requirements or [],
                "experience_level": v.experience_level,
                "is_active": v.is_active,
                "is_fraud_suspect": any(s.get("severity") == "high" for s in (v.fraud_signals or [])),
                "fraud_signals": v.fraud_signals or [],
                "verification_status": v.verification_status,
                "sources_detail": v.sources_detail or [],
            }
            for v in vacancies
        ],
    }


@router.post("/import/upload", status_code=status.HTTP_200_OK)
async def import_uploaded_file(
    provider: str = Form("chatgpt"),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Carga y procesamiento de archivos TXT, MD, JSON o CSV desde la interfaz.
    """
    content_bytes = await file.read()
    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content_text = content_bytes.decode("latin-1", errors="replace")

    ext = file.filename.split(".")[-1].lower() if file.filename and "." in file.filename else None
    format_hint = ext if ext in ("md", "markdown", "json", "csv", "txt") else None

    batch, vacancies = await JobIngestionService.process_and_import(
        session=session,
        tenant_id=tenant_id,
        provider=provider,
        raw_content=content_text,
        import_type="file_upload",
        format_hint=format_hint,
    )

    return {
        "batch_id": batch.id,
        "provider": batch.provider,
        "filename": file.filename,
        "status": batch.status,
        "vacancies_found": batch.vacancies_found,
        "vacancies_imported": batch.vacancies_imported,
        "vacancies_deduplicated": batch.vacancies_deduplicated,
        "notes": batch.notes,
        "vacancies_count": len(vacancies),
    }


@router.post("/import-source", status_code=status.HTTP_201_CREATED)
async def import_structured_source(
    payload: StructuredImportRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Entrada estructurada de vacantes.
    Endpoint autenticado de importación de resultados para integraciones.
    """
    batch = ImportBatch(
        tenant_id=tenant_id,
        provider=payload.provider,
        import_type="api",
        status="completed",
        vacancies_found=len(payload.vacancies),
    )
    session.add(batch)
    await session.flush()

    imported = []
    now_dt = datetime.now(timezone.utc)
    today = now_dt.date()

    for item in payload.vacancies:
        clean_url = canonicalize_url(item.source_url)
        is_safe, _ = validate_url(clean_url)
        dedup_hash = compute_dedup_hash(clean_url, item.company_name, item.job_title)

        existing_res = await session.execute(
            select(Vacancy).where(Vacancy.tenant_id == tenant_id, Vacancy.dedup_hash == dedup_hash)
        )
        existing = existing_res.scalar_one_or_none()

        source_rec = {
            "source": payload.provider,
            "discovered_at": now_dt.isoformat(),
            "snippet": item.original_snippet or f"{item.job_title} en {item.company_name}",
            "import_batch_id": batch.id,
        }

        if existing:
            batch.vacancies_deduplicated += 1
            sources = list(existing.sources_detail or [])
            sources.append(source_rec)
            existing.sources_detail = sources
            if existing.source != payload.provider and payload.provider in ("chatgpt", "gemini"):
                existing.source = "both"
            existing.last_verified_at = now_dt
            session.add(existing)
            imported.append(existing)
        else:
            batch.vacancies_imported += 1
            integrity = VacancyIntegrityService.analyze_vacancy(
                title=item.job_title,
                description=item.description or "",
                source_url=clean_url,
                salary_min=item.salary_min,
                salary_max=item.salary_max,
                salary_currency=item.salary_currency or "USD",
            )
            vac = Vacancy(
                tenant_id=tenant_id,
                source=payload.provider,
                source_url=clean_url,
                company_name=item.company_name,
                job_title=item.job_title,
                location=item.location or "Remoto",
                remote_type=item.remote_type or "remote",
                allowed_countries=item.allowed_countries or [],
                salary_min=item.salary_min,
                salary_max=item.salary_max,
                salary_currency=item.salary_currency or "USD",
                requirements=item.requirements or [],
                technical_requirements=item.requirements or [],
                experience_level=item.experience_level or "unknown",
                description=item.description or "",
                original_snippet=item.original_snippet or f"[{payload.provider.upper()}]: {item.job_title}",
                posted_date=today,
                discovered_date=today,
                is_active=integrity["is_active"],
                verification_status="verified" if is_safe else "pending_verification",
                unknown_fields=[] if item.salary_min else ["salary"],
                sources_detail=[source_rec],
                import_batch_id=batch.id,
                fraud_signals=integrity["fraud_signals"],
                closure_reason=integrity["closure_reason"],
                dedup_hash=dedup_hash,
                last_verified_at=now_dt,
            )
            session.add(vac)
            imported.append(vac)

    await session.commit()
    return {
        "status": "success",
        "batch_id": batch.id,
        "vacancies_imported": batch.vacancies_imported,
        "vacancies_deduplicated": batch.vacancies_deduplicated,
    }


@router.post("/sync-email", status_code=status.HTTP_200_OK)
async def sync_email_reports(
    payload: Optional[EmailSyncRequest] = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Conector de correo para reportes de ChatGPT y Gemini.
    Si se proporciona payload procesa ese correo; de lo contrario busca correos no leídos vía IMAP.
    Detecta reportes y marca contenido insuficiente si solo contiene enlaces o avisos.
    """
    if payload:
        batch, vacancies, message = await EmailJobSearchConnector.process_email_message(
            session=session,
            tenant_id=tenant_id,
            sender=payload.sender,
            subject=payload.subject,
            body=payload.body,
        )
    else:
        # Modo sync directo (IMAP mailbox search)
        batch, vacancies, message = await EmailJobSearchConnector.sync_from_mailbox(
            session=session,
            tenant_id=tenant_id,
        )

    return {
        "status": batch.status if batch else "ignored",
        "message": message,
        "vacancies_imported": len(vacancies) if vacancies else 0,
        "batch_id": batch.id if batch else None,
    }


@router.get("/import-batches")
async def list_import_batches(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Historial de importaciones y procedencia."""
    res = await session.execute(
        select(ImportBatch)
        .where(ImportBatch.tenant_id == tenant_id)
        .order_by(ImportBatch.created_at.desc())
    )
    batches = res.scalars().all()
    return [
        {
            "id": b.id,
            "provider": b.provider,
            "import_type": b.import_type,
            "status": b.status,
            "vacancies_found": b.vacancies_found,
            "vacancies_imported": b.vacancies_imported,
            "vacancies_deduplicated": b.vacancies_deduplicated,
            "notes": b.notes,
            "raw_content_preview": b.raw_content_preview,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in batches
    ]


@router.get("")
async def list_vacancies(
    include_inactive: bool = Query(True, description="Incluir vacantes cerradas o sospechosas"),
    source_filter: Optional[str] = Query(None, description="chatgpt, gemini, both, manual, remotive"),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Lista las vacantes del tenant con filtro por fuente (ChatGPT, Gemini, Ambos, manual, remotive),
    indicadores de fraude, vigencia, requisitos técnicos, fecha de consulta y compatibilidad calculada.
    """
    query = select(Vacancy).where(Vacancy.tenant_id == tenant_id)
    if not include_inactive:
        query = query.where(Vacancy.is_active.is_(True))

    if source_filter:
        s_filt = source_filter.lower().strip()
        if s_filt in ("chatgpt", "gemini", "both", "manual", "remotive"):
            query = query.where(Vacancy.source == s_filt)

    res = await session.execute(query.order_by(Vacancy.created_at.desc()))
    vacancies = res.scalars().all()

    evals_res = await session.execute(
        select(Evaluation).where(Evaluation.tenant_id == tenant_id)
    )
    eval_by_vac: Dict[str, Evaluation] = {}
    for ev in evals_res.scalars().all():
        if ev.vacancy_id not in eval_by_vac:
            eval_by_vac[ev.vacancy_id] = ev

    result_list = []
    for v in vacancies:
        eval_item = eval_by_vac.get(v.id)
        result_list.append({
            "id": v.id,
            "company_name": v.company_name,
            "job_title": v.job_title,
            "source": v.source,
            "source_url": v.source_url,
            "location": v.location,
            "remote_type": v.remote_type,
            "allowed_countries": v.allowed_countries or [],
            "salary_min": float(v.salary_min) if v.salary_min else None,
            "salary_max": float(v.salary_max) if v.salary_max else None,
            "salary_currency": v.salary_currency,
            "requirements": v.requirements or [],
            "technical_requirements": v.technical_requirements or v.requirements or [],
            "experience_level": v.experience_level,
            "posted_date": v.posted_date.isoformat() if v.posted_date else None,
            "discovered_date": v.discovered_date.isoformat() if v.discovered_date else None,
            "closing_date": v.closing_date.isoformat() if v.closing_date else None,
            "fetched_at": v.fetched_at.isoformat() if hasattr(v, "fetched_at") and v.fetched_at else None,
            "is_real_search_result": getattr(v, "is_real_search_result", False),
            "is_active": v.is_active,
            "verification_status": v.verification_status,
            "unknown_fields": v.unknown_fields or [],
            "sources_detail": v.sources_detail or [],
            "original_snippet": v.original_snippet,
            "is_fraud_suspect": any(s.get("severity") == "high" for s in (v.fraud_signals or [])),
            "fraud_signals": v.fraud_signals or [],
            "closure_reason": v.closure_reason,
            "last_verified_at": v.last_verified_at.isoformat() if v.last_verified_at else None,
            "compatibility_score": eval_item.compatibility_score if eval_item else None,
            "recommendation": eval_item.recommendation if eval_item else None,
            "matched_skills": eval_item.matched_skills if eval_item else [],
            "missing_skills": eval_item.missing_skills if eval_item else [],
        })

    return result_list


# === ENDPOINTS DE PREFERENCIAS Y BÚSQUEDA AUTOMATIZADA ===


@router.get("/preferences")
async def get_search_preferences(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Retorna las preferencias de búsqueda configuradas y el estado de autorización de fuentes."""
    pref = await JobSearchService.get_or_create_preferences(session, tenant_id)
    connector = RemotiveJobConnector()

    return {
        "desired_job_titles": pref.desired_job_titles or [],
        "skills": pref.skills or [],
        "locations": pref.locations or [],
        "remote_modalities": pref.remote_modalities or ["remote"],
        "experience_level": pref.experience_level,
        "salary_min": float(pref.salary_min) if pref.salary_min else None,
        "salary_currency": pref.salary_currency,
        "excluded_keywords": pref.excluded_keywords or [],
        "excluded_companies": pref.excluded_companies or [],
        "is_periodic_enabled": pref.is_periodic_enabled,
        "interval_hours": pref.interval_hours,
        "last_run_at": pref.last_run_at.isoformat() if pref.last_run_at else None,
        "next_run_at": pref.next_run_at.isoformat() if pref.next_run_at else None,
        "last_status": pref.last_status,
        "last_error": pref.last_error,
        "last_results_count": pref.last_results_count,
        "last_new_jobs_count": pref.last_new_jobs_count,
        "source_access_authorized": pref.source_access_authorized,
        "authorized_sources": pref.authorized_sources or ["remotive"],
        "source_metadata": {
            "name": connector.source_name,
            "base_domain": connector.base_domain,
            "data_sent": connector.data_sent_description,
            "requires_credentials": False,
        },
    }


@router.put("/preferences")
async def update_search_preferences(
    payload: PreferencesUpdate,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Actualiza los criterios y exclusiones de búsqueda de empleo."""
    updated = await JobSearchService.update_preferences(
        session=session,
        tenant_id=tenant_id,
        data=payload.model_dump(exclude_unset=True),
    )
    return {
        "status": "success",
        "message": "Preferencias de búsqueda actualizadas correctamente.",
        "desired_job_titles": updated.desired_job_titles or [],
        "skills": updated.skills or [],
        "locations": updated.locations or [],
        "remote_modalities": updated.remote_modalities or ["remote"],
        "experience_level": updated.experience_level,
        "salary_min": float(updated.salary_min) if updated.salary_min else None,
        "salary_currency": updated.salary_currency,
        "excluded_keywords": updated.excluded_keywords or [],
        "excluded_companies": updated.excluded_companies or [],
        "is_periodic_enabled": updated.is_periodic_enabled,
        "interval_hours": updated.interval_hours,
        "last_run_at": updated.last_run_at.isoformat() if updated.last_run_at else None,
        "next_run_at": updated.next_run_at.isoformat() if updated.next_run_at else None,
        "last_status": updated.last_status,
        "last_error": updated.last_error,
        "last_results_count": updated.last_results_count,
        "last_new_jobs_count": updated.last_new_jobs_count,
        "source_access_authorized": updated.source_access_authorized,
        "authorized_sources": updated.authorized_sources or ["remotive"],
        "preferences": {
            "desired_job_titles": updated.desired_job_titles,
            "skills": updated.skills,
            "locations": updated.locations,
            "remote_modalities": updated.remote_modalities,
            "experience_level": updated.experience_level,
            "salary_min": float(updated.salary_min) if updated.salary_min else None,
            "salary_currency": updated.salary_currency,
            "excluded_keywords": updated.excluded_keywords,
            "excluded_companies": updated.excluded_companies,
            "interval_hours": updated.interval_hours,
        },
    }


@router.post("/preferences/authorize-source")
async def authorize_source_access(
    payload: AuthorizeSourcePayload,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Otorga autorización explícita para que la aplicación realice consultas HTTP
    de solo lectura a la fuente externa pública configurada (Remotive).
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe confirmar explícitamente la autorización de acceso a red.",
        )

    updated = await JobSearchService.authorize_source_access(
        session=session,
        tenant_id=tenant_id,
        source_name=payload.source_name,
    )

    return {
        "status": "authorized",
        "message": f"Acceso de lectura a la fuente '{payload.source_name}' autorizado por el usuario.",
        "source_access_authorized": updated.source_access_authorized,
        "authorized_sources": updated.authorized_sources,
    }


@router.post("/preferences/revoke-source")
async def revoke_source_access(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Revoca la autorización de consultas de red externas, regresando a modo local seguro."""
    updated = await JobSearchService.revoke_source_access(session, tenant_id)
    return {
        "status": "revoked",
        "message": "Acceso a fuentes de red externas revocado. El sistema operará en modo local sin salida a red.",
        "source_access_authorized": updated.source_access_authorized,
    }


@router.post("/search/toggle-periodic")
async def toggle_periodic_search(
    payload: TogglePeriodicPayload,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Activa o pausa la búsqueda periódica automática en segundo plano."""
    updated = await JobSearchService.toggle_periodic_search(
        session=session,
        tenant_id=tenant_id,
        enabled=payload.enabled,
        interval_hours=payload.interval_hours,
    )
    return {
        "status": "success",
        "is_periodic_enabled": updated.is_periodic_enabled,
        "interval_hours": updated.interval_hours,
        "next_run_at": updated.next_run_at.isoformat() if updated.next_run_at else None,
    }


@router.post("/search/run")
async def run_job_search(
    payload: SearchRunPayload = SearchRunPayload(),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Ejecuta un ciclo de búsqueda inmediata de vacantes:
    - Consulta la fuente Remotive (en vivo si está autorizada; con muestra verificable si no).
    - Aplica deduplicación, filtros de exclusión y análisis de integridad.
    - Clasifica de manera determinista respecto al perfil verificado del usuario.
    """
    try:
        result = await JobSearchService.execute_job_search(
            session=session,
            tenant_id=tenant_id,
            force_dry_run=payload.force_dry_run,
            limit=payload.limit,
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo durante la ejecución de búsqueda: {e}",
        )


@router.get("/search/status")
async def get_search_status(
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Retorna el estado operativo actual del servicio de búsqueda y última ejecución."""
    pref = await JobSearchService.get_or_create_preferences(session, tenant_id)
    return {
        "last_status": pref.last_status,
        "last_run_at": pref.last_run_at.isoformat() if pref.last_run_at else None,
        "next_run_at": pref.next_run_at.isoformat() if pref.next_run_at else None,
        "last_error": pref.last_error,
        "is_periodic_enabled": pref.is_periodic_enabled,
        "interval_hours": pref.interval_hours,
        "source_access_authorized": pref.source_access_authorized,
        "last_results_count": pref.last_results_count,
        "last_new_jobs_count": pref.last_new_jobs_count,
    }


@router.get("/{vacancy_id}")
async def get_vacancy(
    vacancy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Obtiene los detalles de una vacante específica incluyendo auditoría de integridad y procedencia."""
    res = await session.execute(
        select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    return {
        "id": vacancy.id,
        "company_name": vacancy.company_name,
        "job_title": vacancy.job_title,
        "source": vacancy.source,
        "source_url": vacancy.source_url,
        "location": vacancy.location,
        "remote_type": vacancy.remote_type,
        "allowed_countries": vacancy.allowed_countries or [],
        "salary_min": float(vacancy.salary_min) if vacancy.salary_min else None,
        "salary_max": float(vacancy.salary_max) if vacancy.salary_max else None,
        "salary_currency": vacancy.salary_currency,
        "requirements": vacancy.requirements or [],
        "technical_requirements": vacancy.technical_requirements or vacancy.requirements or [],
        "experience_level": vacancy.experience_level,
        "description": vacancy.description,
        "original_snippet": vacancy.original_snippet,
        "posted_date": vacancy.posted_date.isoformat() if vacancy.posted_date else None,
        "discovered_date": vacancy.discovered_date.isoformat() if vacancy.discovered_date else None,
        "closing_date": vacancy.closing_date.isoformat() if vacancy.closing_date else None,
        "fetched_at": vacancy.fetched_at.isoformat() if hasattr(vacancy, "fetched_at") and vacancy.fetched_at else None,
        "is_real_search_result": getattr(vacancy, "is_real_search_result", False),
        "is_active": vacancy.is_active,
        "verification_status": vacancy.verification_status,
        "unknown_fields": vacancy.unknown_fields or [],
        "sources_detail": vacancy.sources_detail or [],
        "is_fraud_suspect": any(s.get("severity") == "high" for s in (vacancy.fraud_signals or [])),
        "fraud_signals": vacancy.fraud_signals or [],
        "closure_reason": vacancy.closure_reason,
        "dedup_hash": vacancy.dedup_hash,
        "last_verified_at": vacancy.last_verified_at.isoformat() if vacancy.last_verified_at else None,
    }


@router.post("/{vacancy_id}/verify-status")
async def verify_vacancy_status(
    vacancy_id: str,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Re-analiza y actualiza deterministamente el estado de vigencia y fraude de la vacante (REQ-009).
    """
    res = await session.execute(
        select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.tenant_id == tenant_id)
    )
    vacancy = res.scalar_one_or_none()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacante no encontrada.")

    integrity = VacancyIntegrityService.analyze_vacancy(
        title=vacancy.job_title,
        description=vacancy.description,
        source_url=vacancy.source_url,
        salary_min=float(vacancy.salary_min) if vacancy.salary_min else None,
        salary_max=float(vacancy.salary_max) if vacancy.salary_max else None,
        salary_currency=vacancy.salary_currency,
        closing_date=vacancy.closing_date,
    )

    vacancy.is_active = integrity["is_active"]
    vacancy.fraud_signals = integrity["fraud_signals"]
    vacancy.closure_reason = integrity["closure_reason"]
    vacancy.last_verified_at = datetime.now(timezone.utc)
    session.add(vacancy)
    await session.commit()
    await session.refresh(vacancy)

    return {
        "id": vacancy.id,
        "is_active": vacancy.is_active,
        "is_fraud_suspect": integrity["is_fraud_suspect"],
        "fraud_signals": vacancy.fraud_signals,
        "is_closed": integrity["is_closed"],
        "closure_reason": vacancy.closure_reason,
        "last_verified_at": vacancy.last_verified_at.isoformat(),
    }
