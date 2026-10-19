"""
Servicio de ingestión multifuente, normalización y deduplicación para ChatGPT y Gemini (TAREA 2).
Soporta importación universal (Markdown, JSON, CSV, Texto), conectores de correo seguros,
detección de contenido insuficiente y trazabilidad estricta de procedencia.
"""

import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import detect_injection_signals, validate_url
from backend.app.modules.vacancies.models import Vacancy, ImportBatch
from backend.app.modules.vacancies.service import VacancyIntegrityService


# Parámetros de rastreo que deben eliminarse en la canonicalización de URLs
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "fbclid",
    "gclid",
    "gh_src",
    "si",
    "trk",
    "trackingid",
}


def canonicalize_url(raw_url: str) -> str:
    """
    Normaliza y canonicaliza una URL para deduplicación robusta entre proveedores.
    - Pasa esquema y host a minúsculas.
    - Elimina parámetros de tracking/marketing (utm_*, ref, etc.).
    - Remueve slash final en ruta salvo en raíz.
    - Ordena parámetros de consulta restantes para estabilidad criptográfica.
    """
    if not raw_url or not isinstance(raw_url, str):
        return ""
    url = raw_url.strip()
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        # Filtrar y ordenar query params
        query_dict = parse_qs(parsed.query, keep_blank_values=False)
        clean_query = {
            k: v for k, v in query_dict.items() if k.lower() not in TRACKING_PARAMS
        }
        sorted_query = urlencode(clean_query, doseq=True)

        return urlunparse((scheme, netloc, path, parsed.params, sorted_query, ""))
    except Exception:
        return url.strip().rstrip("/")


def compute_dedup_hash(canonical_url: str, company: str, title: str) -> str:
    """
    Calcula un hash determinista de deduplicación.
    Combina la URL canónica (o en su defecto empresa + título normalizados).
    """
    norm_company = re.sub(r"[^\w\s]", "", company.lower()).strip()
    norm_title = re.sub(r"[^\w\s]", "", title.lower()).strip()
    input_str = f"{canonical_url}|{norm_company}|{norm_title}"
    return hashlib.sha256(input_str.encode("utf-8")).hexdigest()


class InsufficientContentError(ValueError):
    """Lanzada cuando un reporte o correo solo contiene avisos o enlaces sin ofertas reales."""
    pass


class JobIngestionService:
    """
    Servicio principal de ingestión de vacantes desde ChatGPT, Gemini y entradas manuales.
    Normaliza contra esquema unificado, deduplica preservando ambas fuentes y audita procedencia.
    """

    @classmethod
    def sanitize_untrusted_text(cls, text: Optional[str]) -> Tuple[str, List[str]]:
        """
        Trata el contenido importado como no confiable.
        Detecta señales de inyección de prompts y sanitiza el texto sin ejecutar instrucciones.
        """
        if not text:
            return "", []
        text_str = str(text).strip()
        signals = detect_injection_signals(text_str)
        return text_str, signals

    @classmethod
    def parse_salary_range(cls, text: str) -> Tuple[Optional[float], Optional[float], str]:
        """Extrae montos mínimo, máximo y moneda desde texto de salario."""
        if not text:
            return None, None, "USD"
        currency = "USD"
        if "eur" in text.lower() or "€" in text:
            currency = "EUR"
        elif "mxn" in text.lower() or "pesos" in text.lower():
            currency = "MXN"

        numbers = re.findall(r"\b\d{1,3}(?:[,\.]\d{3})*(?:\.\d{1,2})?\b|\b\d{3,6}\b", text)
        clean_nums = []
        for n in numbers:
            try:
                val = float(n.replace(",", ""))
                if val >= 200:  # Filtrar números que no parecen salarios mensuales o anuales
                    clean_nums.append(val)
            except ValueError:
                continue

        if not clean_nums:
            return None, None, currency
        if len(clean_nums) == 1:
            return clean_nums[0], clean_nums[0], currency
        return min(clean_nums), max(clean_nums), currency

    @classmethod
    def extract_allowed_countries_and_remote(
        cls, location_text: str, description_text: str
    ) -> Tuple[str, str, List[str]]:
        """
        Analiza ubicación, modalidad remota y países permitidos.
        NO asume que remoto significa mundial (REQ Tarea 2.3).
        """
        combined = f"{location_text} {description_text}".lower()
        remote_type = "remote" if "remot" in combined else "unknown"
        if "híbrid" in combined or "hybrid" in combined:
            remote_type = "hybrid"
        elif "presencial" in combined or "onsite" in combined or "en sitio" in combined:
            remote_type = "onsite"

        allowed = []
        # Reconocer patrones comunes de restricciones geográficas
        if "el salvador" in combined:
            allowed.append("El Salvador")
        if "latam" in combined or "latinoamérica" in combined or "latin america" in combined:
            allowed.append("LATAM")
        if "méxico" in combined or "mexico" in combined:
            allowed.append("México")
        if "colombia" in combined:
            allowed.append("Colombia")
        if "argentina" in combined:
            allowed.append("Argentina")
        if "estados unidos" in combined or "usa" in combined or "us only" in combined:
            allowed.append("USA")
        if "españa" in combined or "spain" in combined:
            allowed.append("España")
        if "worldwide" in combined or "global" in combined or "cualquier país" in combined:
            allowed.append("Worldwide")

        return remote_type, location_text.strip() or "Remoto", allowed

    @classmethod
    def extract_experience_level(cls, text: str) -> str:
        """Determina nivel de experiencia o unknown."""
        t = text.lower()
        if "lead" in t or "principal" in t or "staff" in t:
            return "lead"
        if "senior" in t or "sr" in t:
            return "senior"
        if "mid" in t or "semi-senior" in t or "ssr" in t:
            return "mid"
        if "junior" in t or "jr" in t or "trainee" in t or "entry" in t:
            return "junior"
        return "unknown"

    @classmethod
    def parse_markdown_report(cls, md_text: str) -> List[Dict[str, Any]]:
        """
        Parsea resultados en formato Markdown generados por ChatGPT o Gemini.
        Soporta tanto tablas Markdown como bloques de encabezados y listas viñeteadas.
        """
        vacancies = []
        lines = md_text.splitlines()

        # 1. Intentar parsear tabla Markdown (| Empresa | Puesto | URL | ...)
        table_lines = [l.strip() for l in lines if l.strip().startswith("|") and l.strip().endswith("|")]
        if len(table_lines) >= 3:
            header_cells = [c.strip().lower() for c in table_lines[0].split("|")[1:-1]]
            # Verificar si parece encabezado de tabla de vacantes
            has_company = any("empresa" in h or "company" in h for h in header_cells)
            has_title = any("puesto" in h or "cargo" in h or "título" in h or "title" in h or "rol" in h for h in header_cells)
            if has_company and has_title:
                for row_line in table_lines[2:]:  # Omitir cabecera y separador |--|--|
                    cells = [c.strip() for c in row_line.split("|")[1:-1]]
                    if len(cells) != len(header_cells):
                        continue
                    row_data = dict(zip(header_cells, cells))
                    comp = next((v for k, v in row_data.items() if "empresa" in k or "company" in k), "Empresa Desconocida")
                    tit = next((v for k, v in row_data.items() if any(w in k for w in ("puesto", "cargo", "título", "title", "rol"))), "Puesto Desconocido")
                    url = next((v for k, v in row_data.items() if "url" in k or "enlace" in k or "link" in k), "")
                    # Extraer URL si está en formato markdown [Link](https://...)
                    m_url = re.search(r"\((https?://[^\)]+)\)", url)
                    if m_url:
                        url = m_url.group(1)
                    loc = next((v for k, v in row_data.items() if "ubicaci" in k or "location" in k or "modalidad" in k), "Remoto")
                    sal = next((v for k, v in row_data.items() if "salario" in k or "salary" in k or "sueldo" in k), "")
                    reqs = next((v for k, v in row_data.items() if "requisito" in k or "skill" in k or "tecnolog" in k), "")

                    vacancies.append({
                        "company_name": comp,
                        "job_title": tit,
                        "source_url": url,
                        "location": loc,
                        "salary_text": sal,
                        "requirements_text": reqs,
                        "description": row_line,
                        "original_snippet": row_line,
                    })
                if vacancies:
                    return vacancies

        # 2. Parsear bloques de encabezados (### 1. Puesto - Empresa o **Empresa:** ...)
        current_job: Dict[str, Any] = {}
        current_snippet_lines: List[str] = []

        def flush_current():
            if current_job and (current_job.get("company_name") or current_job.get("job_title")):
                current_job["original_snippet"] = "\n".join(current_snippet_lines).strip()
                if not current_job.get("description"):
                    current_job["description"] = current_job["original_snippet"]
                vacancies.append(dict(current_job))
            current_job.clear()
            current_snippet_lines.clear()

        for line in lines:
            trimmed = line.strip()
            # Encabezado que inicia vacante: ### 1. Senior Python Dev — Acme
            m_header = re.match(r"^#{1,4}\s*(?:\d+[\.\)]\s*)?([^\n—–-]+)(?:[—–-]\s*([^\n]+))?", trimmed)
            if m_header and len(trimmed) > 5 and not trimmed.lower().startswith(("# reporte", "## resumen", "### resumen")):
                flush_current()
                part1 = m_header.group(1).strip()
                part2 = m_header.group(2).strip() if m_header.group(2) else ""
                if part2:
                    current_job["job_title"] = part1
                    current_job["company_name"] = part2
                else:
                    current_job["job_title"] = part1
                    current_job["company_name"] = "Empresa por confirmar"
                current_snippet_lines.append(trimmed)
                continue

            if not trimmed:
                continue

            current_snippet_lines.append(trimmed)

            # Patrones de atributos
            lower = trimmed.lower()
            if any(lower.startswith(prefix) for prefix in ("empresa:", "**empresa:**", "- empresa:", "* empresa:", "company:", "**company:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:empresa|company)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                current_job["company_name"] = val
            elif any(lower.startswith(prefix) for prefix in ("puesto:", "**puesto:**", "cargo:", "**cargo:**", "rol:", "**rol:**", "título:", "**título:**", "title:", "**title:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:puesto|cargo|rol|título|title)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                current_job["job_title"] = val
            elif any(lower.startswith(prefix) for prefix in ("url:", "**url:**", "enlace:", "**enlace:**", "link:", "**link:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:url|enlace|link)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                m_url = re.search(r"https?://[^\s\)\>]+", val)
                if m_url:
                    current_job["source_url"] = m_url.group(0)
            elif any(lower.startswith(prefix) for prefix in ("ubicación:", "**ubicación:**", "location:", "**location:**", "modalidad:", "**modalidad:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:ubicación|location|modalidad)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                current_job["location"] = val
            elif any(lower.startswith(prefix) for prefix in ("salario:", "**salario:**", "salary:", "**salary:**", "sueldo:", "**sueldo:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:salario|salary|sueldo)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                current_job["salary_text"] = val
            elif any(lower.startswith(prefix) for prefix in ("requisitos:", "**requisitos:**", "skills:", "**skills:**", "tecnologías:", "**tecnologías:**")):
                val = re.sub(r"^[-*]?\s*\**\s*(?:requisitos|skills|tecnologías)\s*\**\s*:\s*", "", trimmed, flags=re.I).strip()
                current_job["requirements_text"] = val
            elif "https://" in trimmed or "http://" in trimmed:
                if not current_job.get("source_url"):
                    m_url = re.search(r"https?://[^\s\)\>]+", trimmed)
                    if m_url:
                        current_job["source_url"] = m_url.group(0)

        flush_current()
        return vacancies

    @classmethod
    def parse_json_report(cls, json_text: str) -> List[Dict[str, Any]]:
        """Parsea JSON estructurado con lista de vacantes."""
        data = json.loads(json_text)
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("jobs", "vacancies", "vacantes", "results", "ofertas", "data"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if not items and ("company" in data or "company_name" in data or "title" in data or "job_title" in data):
                items = [data]

        vacancies = []
        for it in items:
            if not isinstance(it, dict):
                continue
            comp = it.get("company_name") or it.get("company") or it.get("empresa") or "Empresa Desconocida"
            tit = it.get("job_title") or it.get("title") or it.get("puesto") or it.get("cargo") or "Puesto Desconocido"
            url = it.get("source_url") or it.get("url") or it.get("link") or ""
            loc = it.get("location") or it.get("ubicacion") or "Remoto"
            reqs = it.get("requirements") or it.get("requisitos") or it.get("skills") or []
            if isinstance(reqs, list):
                reqs_str = ", ".join(str(r) for r in reqs)
            else:
                reqs_str = str(reqs)

            vacancies.append({
                "company_name": comp,
                "job_title": tit,
                "source_url": url,
                "location": loc,
                "remote_type": it.get("remote_type", "remote"),
                "salary_min": it.get("salary_min"),
                "salary_max": it.get("salary_max"),
                "salary_currency": it.get("salary_currency", "USD"),
                "salary_text": it.get("salary") or it.get("salario") or "",
                "requirements_text": reqs_str,
                "description": it.get("description") or it.get("descripcion") or f"{tit} en {comp}",
                "original_snippet": json.dumps(it, ensure_ascii=False),
            })
        return vacancies

    @classmethod
    def parse_csv_report(cls, csv_text: str) -> List[Dict[str, Any]]:
        """Parsea archivo o texto CSV con vacantes."""
        reader = csv.DictReader(io.StringIO(csv_text))
        vacancies = []
        for row in reader:
            clean_row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            comp = next((v for k, v in clean_row.items() if "company" in k or "empresa" in k), "Empresa Desconocida")
            tit = next((v for k, v in clean_row.items() if any(w in k for w in ("title", "puesto", "cargo", "job"))), "Puesto Desconocido")
            url = next((v for k, v in clean_row.items() if "url" in k or "link" in k or "enlace" in k), "")
            loc = next((v for k, v in clean_row.items() if "loc" in k or "ubicaci" in k), "Remoto")
            sal = next((v for k, v in clean_row.items() if "sal" in k or "sueldo" in k), "")
            reqs = next((v for k, v in clean_row.items() if "req" in k or "skill" in k), "")

            vacancies.append({
                "company_name": comp,
                "job_title": tit,
                "source_url": url,
                "location": loc,
                "salary_text": sal,
                "requirements_text": reqs,
                "description": f"Vacante CSV: {tit} en {comp}. Ubicación: {loc}.",
                "original_snippet": json.dumps(row, ensure_ascii=False),
            })
        return vacancies

    @classmethod
    def detect_and_parse(cls, content: str, format_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Detección automática de formato (Markdown, JSON, CSV, Texto) y extracción segura.
        Verifica contenido insuficiente ante notificaciones vacías.
        """
        content_stripped = content.strip()
        if not content_stripped:
            raise InsufficientContentError("El contenido proporcionado está vacío.")

        # Detección de notificaciones de correo o mensajes sin vacantes
        notification_markers = [
            r"tus\s+alertas?\s+diarias?\s+est[aá]n\s+listas?",
            r"daily\s+search\s+alert",
            r"haz\s+clic\s+aqu[ií]\s+para\s+ver\s+(?:tus\s+)?resultados",
            r"click\s+here\s+to\s+view",
            r"se\s+encontraron\s+0\s+empleos",
            r"no\s+new\s+jobs\s+found",
        ]
        has_notification_notice = any(re.search(pat, content_stripped, re.I) for pat in notification_markers)

        # 1. Intentar JSON
        if format_hint == "json" or content_stripped.startswith(("{", "[")):
            try:
                jobs = cls.parse_json_report(content_stripped)
                if jobs:
                    return jobs
            except Exception:
                pass

        # 2. Intentar CSV
        if format_hint == "csv" or ("," in content_stripped and "\n" in content_stripped and any(h in content_stripped.lower() for h in ("empresa,", "company,", "puesto,", "title,"))):
            try:
                jobs = cls.parse_csv_report(content_stripped)
                if jobs:
                    return jobs
            except Exception:
                pass

        # 3. Intentar Markdown / Texto
        jobs = cls.parse_markdown_report(content_stripped)
        if jobs:
            return jobs

        # Si no se extrajeron empleos y tiene marcadores de notificación o solo enlaces genéricos:
        if has_notification_notice or len(content_stripped) < 200:
            raise InsufficientContentError(
                "El contenido importado parece ser una simple notificación o enlace sin detalles de las vacantes. "
                "No se han generado vacantes ficticias (Regla de Veracidad)."
            )

        raise InsufficientContentError(
            "No se pudieron identificar vacantes en el formato proporcionado. "
            "Asegúrate de pegar el reporte con puestos, empresas y URLs."
        )

    @classmethod
    async def process_and_import(
        cls,
        session: AsyncSession,
        tenant_id: str,
        provider: str,
        raw_content: str,
        import_type: str = "manual_paste",
        format_hint: Optional[str] = None,
    ) -> Tuple[ImportBatch, List[Vacancy]]:
        """
        Ejecuta el ciclo completo de ingestión:
        1. Parseo seguro y detección de formato.
        2. Normalización de URLs y atributos.
        3. Detección de fraude y expiración.
        4. Deduplicación determinista:
           - Si la vacante ya existe (ej. encontrada antes por ChatGPT y ahora por Gemini),
             se consolida una sola vacante con source='both' y se añade trazabilidad.
        5. Persistencia del ImportBatch con auditoría de procedencia.
        """
        provider_norm = provider.lower().strip()
        if provider_norm not in ("chatgpt", "gemini", "manual"):
            provider_norm = "manual"

        # Crear registro inicial del lote
        batch = ImportBatch(
            tenant_id=tenant_id,
            provider=provider_norm,
            import_type=import_type,
            status="completed",
            raw_content_preview=raw_content[:500] if raw_content else "",
        )
        session.add(batch)
        await session.flush()

        try:
            raw_jobs = cls.detect_and_parse(raw_content, format_hint=format_hint)
        except InsufficientContentError as ice:
            batch.status = "insufficient_content"
            batch.notes = str(ice)
            await session.commit()
            return batch, []
        except Exception as exc:
            batch.status = "failed"
            batch.notes = f"Error de procesamiento: {exc}"
            await session.commit()
            return batch, []

        batch.vacancies_found = len(raw_jobs)
        imported_vacancies: List[Vacancy] = []
        now_dt = datetime.now(timezone.utc)
        today = now_dt.date()

        for r_job in raw_jobs:
            company, _ = cls.sanitize_untrusted_text(r_job.get("company_name", "Empresa Desconocida"))
            title, _ = cls.sanitize_untrusted_text(r_job.get("job_title", "Puesto"))
            raw_url = r_job.get("source_url", "")
            canonical_url = canonicalize_url(raw_url)

            # Validar URL con defensas anti-SSRF de la aplicación
            is_safe_url, _ = validate_url(canonical_url)
            verification_status = "pending_verification"
            if not is_safe_url or not canonical_url:
                # Si no tiene URL válida externa, marcar para verificación en lugar de descartar
                verification_status = "pending_verification"
                if not canonical_url:
                    canonical_url = f"https://example.com/unverified-search-job/{hashlib.sha256(f'{company}|{title}'.encode()).hexdigest()[:12]}"

            loc_raw = r_job.get("location", "Remoto")
            desc_raw = r_job.get("description", "")
            remote_type, clean_loc, allowed_countries = cls.extract_allowed_countries_and_remote(loc_raw, desc_raw)

            # Salario
            sal_min = r_job.get("salary_min")
            sal_max = r_job.get("salary_max")
            sal_curr = r_job.get("salary_currency", "USD")
            if sal_min is None and r_job.get("salary_text"):
                sal_min, sal_max, sal_curr = cls.parse_salary_range(r_job["salary_text"])

            # Requisitos y nivel
            reqs_text = r_job.get("requirements_text", "")
            tech_reqs = [
                s.strip() for s in re.split(r"[,;•\n]+", reqs_text) if s.strip() and len(s.strip()) > 1
            ]
            exp_level = cls.extract_experience_level(f"{title} {desc_raw} {reqs_text}")

            snippet = r_job.get("original_snippet", "")
            unknown_fields = []
            if not r_job.get("salary_text") and sal_min is None:
                unknown_fields.append("salary")
            if not tech_reqs:
                unknown_fields.append("technical_requirements")
            if exp_level == "unknown":
                unknown_fields.append("experience_level")
            if not allowed_countries:
                unknown_fields.append("allowed_countries")

            # Análisis determinista de fraude y vacante cerrada
            integrity = VacancyIntegrityService.analyze_vacancy(
                title=title,
                description=desc_raw,
                source_url=canonical_url,
                salary_min=sal_min,
                salary_max=sal_max,
                salary_currency=sal_curr,
            )

            # Hash de deduplicación
            dedup_hash = compute_dedup_hash(canonical_url, company, title)

            # Verificar si ya existe en la base de datos para este tenant
            stmt = select(Vacancy).where(
                and_(
                    Vacancy.tenant_id == tenant_id,
                    Vacancy.dedup_hash == dedup_hash,
                )
            )
            res = await session.execute(stmt)
            existing = res.scalar_one_or_none()

            source_record = {
                "source": provider_norm,
                "discovered_at": now_dt.isoformat(),
                "snippet": snippet,
                "import_batch_id": batch.id,
            }

            if existing:
                # DEDUPLICACIÓN CONSERVANDO AMBAS FUENTES (Tarea 2.3)
                batch.vacancies_deduplicated += 1
                existing_sources = list(existing.sources_detail or [])
                existing_sources.append(source_record)
                existing.sources_detail = existing_sources

                # Si venía de un proveedor y ahora llega del otro, consolidar como 'both'
                if existing.source != provider_norm and provider_norm in ("chatgpt", "gemini"):
                    existing.source = "both"

                # Enriquecer requisitos si el nuevo reporte aporta más
                if tech_reqs:
                    current_reqs = set(existing.requirements or [])
                    current_reqs.update(tech_reqs)
                    existing.requirements = list(current_reqs)
                    existing.technical_requirements = list(current_reqs)

                if snippet and snippet not in (existing.original_snippet or ""):
                    existing.original_snippet = f"{existing.original_snippet}\n---\n[{provider_norm.upper()}]: {snippet}".strip()

                existing.last_verified_at = now_dt
                session.add(existing)
                imported_vacancies.append(existing)
            else:
                # NUEVA VACANTE
                batch.vacancies_imported += 1
                vac = Vacancy(
                    tenant_id=tenant_id,
                    source=provider_norm,
                    source_url=canonical_url,
                    company_name=company,
                    job_title=title,
                    location=clean_loc,
                    remote_type=remote_type,
                    allowed_countries=allowed_countries,
                    salary_min=sal_min,
                    salary_max=sal_max,
                    salary_currency=sal_curr,
                    requirements=tech_reqs,
                    technical_requirements=tech_reqs,
                    experience_level=exp_level,
                    description=desc_raw or snippet,
                    original_snippet=f"[{provider_norm.upper()}]: {snippet}".strip(),
                    posted_date=today,
                    discovered_date=today,
                    is_active=not integrity["is_closed"],
                    verification_status=verification_status,
                    unknown_fields=unknown_fields,
                    sources_detail=[source_record],
                    import_batch_id=batch.id,
                    fraud_signals=integrity["fraud_signals"],
                    closure_reason=integrity["closure_reason"],
                    dedup_hash=dedup_hash,
                    last_verified_at=now_dt,
                )
                session.add(vac)
                imported_vacancies.append(vac)

        await session.commit()
        await session.refresh(batch)
        return batch, imported_vacancies
