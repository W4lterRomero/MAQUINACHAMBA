"""
Extractor seguro de documentos de CV para Máquina de Chamba (Hito 2.1).
Principios clave:
1. Extracción separada de verificación (extraer != verificar).
2. No inventar hechos: prohibido asignar años, niveles o empleos ficticios;
   preservar null/desconocido y precisión de fechas.
3. Defensas fail-closed y presupuestos de recursos:
   - Límite estricto de tamaño (5 MB).
   - Inspección previa de ZIP (máximo 500 entradas, 20 MB expansión, ratio compresión <= 100x).
   - Límite de páginas PDF (máximo 50 páginas) y presupuesto de texto extraído (1 MB).
   - Detección de inyección de prompts y sanitización de contenido como datos.
   - Gestión de archivos corruptos sin ejecutar código ni recurrir a red externa.
"""

from dataclasses import dataclass, field
import hashlib
import io
import re
from typing import Any, Dict, List, Optional
import zipfile

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

from backend.app.core.security import detect_injection_signals


# Límites y presupuestos de recursos
MAX_CV_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

MAX_ZIP_ENTRIES = 500  # Máximo de archivos dentro de un contenedor DOCX
MAX_ZIP_DECOMPRESSED_SIZE = 20 * 1024 * 1024  # 20 MB límite acumulado de expansión
MAX_ZIP_COMPRESSION_RATIO = 100.0  # Razón máxima de compresión (uncompressed / compressed)
MAX_ZIP_ENTRY_SIZE = 10 * 1024 * 1024  # 10 MB límite para una sola entrada
MAX_PDF_PAGES = 50  # Máximo de páginas para un documento curricular
MAX_EXTRACTED_TEXT_LENGTH = 1_000_000  # 1 MB de caracteres de texto (~200,000 palabras)

# Diccionario curado de tecnologías y habilidades reconocidas
# NUNCA CONTIENE AÑOS NI NIVELES POR DEFECTO (prohibido inventar hechos)
KNOWN_SKILL_CATEGORIES: Dict[str, str] = {
    "python": "technical",
    "fastapi": "technical",
    "django": "technical",
    "flask": "technical",
    "sqlite": "technical",
    "postgresql": "technical",
    "mysql": "technical",
    "docker": "technical",
    "kubernetes": "technical",
    "linux": "technical",
    "git": "technical",
    "bash": "technical",
    "react": "technical",
    "typescript": "technical",
    "javascript": "technical",
    "aws": "technical",
    "gcp": "technical",
    "azure": "technical",
    "rest": "technical",
    "graphql": "technical",
    "ci/cd": "technical",
    "redis": "technical",
    "sqlalchemy": "technical",
    "pytest": "technical",
    "scrum": "soft",
    "agile": "soft",
    "liderazgo": "soft",
    "comunicación": "soft",
    "inglés": "language",
    "español": "language",
}

# Patrones para distinguir intereses/menciones o negaciones de experiencia declarada
INTEREST_OR_NEGATION_PATTERN = re.compile(
    r"(?:inter[eé]s\s+en\s+aprender|deseo\s+aprender|gustar[ií]a\s+aprender|aprendiendo|estudiando|"
    r"sin\s+experiencia|no\s+domino|aspirante|interesado\s+en|interested\s+in|want\s+to\s+learn|"
    r"learning|looking\s+to\s+learn|no\s+experiencia)",
    re.IGNORECASE,
)

EXPLICIT_NO_EXPERIENCE_PATTERN = re.compile(
    r"(?:sin\s+experiencia\s+laboral|no\s+cuento\s+con\s+experiencia|sin\s+experiencia|no\s+work\s+experience|no\s+prior\s+experience)",
    re.IGNORECASE,
)


class InvalidDocumentError(ValueError):
    """Excepción lanzada cuando un archivo es corrupto, no soportado, excede límites o es inválido."""
    pass


@dataclass
class ExtractedSkill:
    name: str
    category: str = "technical"
    proficiency: Optional[str] = None  # None si no está declarado (NUNCA inventar)
    years_experience: Optional[float] = None  # None si no está declarado (NUNCA inventar)
    source_fragment: Optional[str] = None  # Fragmento del documento donde aparece
    context_type: str = "declared"  # "declared", "interest", "mention"
    verification_status: str = "pending"  # Siempre pending por defecto


@dataclass
class ExtractedExperience:
    company: str
    title: str
    start_date: Optional[str] = None  # YYYY-MM-DD o None si no se declara
    end_date: Optional[str] = None  # YYYY-MM-DD o None si actual/presente
    date_precision: str = "day"  # "day", "month", "year", "unknown"
    source_fragment: Optional[str] = None  # Fragmento del documento de procedencia
    description: str = ""
    skills_used: List[str] = field(default_factory=list)
    verification_status: str = "pending"  # Siempre pending por defecto


@dataclass
class ExtractedDocument:
    filename: str
    file_hash: str
    raw_text: str
    suggested_profile: Dict[str, Any]
    skills: List[ExtractedSkill]
    experiences: List[ExtractedExperience]
    injection_signals: List[str]


class DocumentParser:
    """Parser local y seguro para documentos curriculares con presupuestos estrictos de recursos."""

    @classmethod
    def parse(cls, file_bytes: bytes, filename: str) -> ExtractedDocument:
        """
        Valida, sanitiza y extrae información estructurada de un archivo de CV.
        No ejecuta código ni realiza peticiones externas.
        """
        # 1. Validación de tamaño
        if not file_bytes or len(file_bytes) == 0:
            raise InvalidDocumentError("El archivo está vacío.")

        if len(file_bytes) > MAX_CV_FILE_SIZE:
            raise InvalidDocumentError(
                f"El archivo excede el tamaño máximo permitido de 5 MB ({len(file_bytes)} bytes)."
            )

        # 2. Validación de extensión
        lower_name = filename.lower()
        matched_ext = next((ext for ext in ALLOWED_EXTENSIONS if lower_name.endswith(ext)), None)
        if not matched_ext:
            raise InvalidDocumentError(
                f"Tipo de archivo no permitido para '{filename}'. Formatos soportados: PDF, DOCX, TXT."
            )

        # 3. Extracción de texto según formato con límites estrictos de recursos
        raw_text = ""
        if matched_ext == ".pdf":
            raw_text = cls._extract_pdf(file_bytes)
        elif matched_ext == ".docx":
            raw_text = cls._extract_docx(file_bytes)
        elif matched_ext in (".txt", ".md"):
            raw_text = cls._extract_text(file_bytes)

        # 4. Sanitización e integridad
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        sanitized_text = cls._sanitize_text(raw_text)
        injection_signals = detect_injection_signals(sanitized_text)

        # 5. Extracción determinista de entidades sin inventar datos
        suggested_profile = cls._extract_contact_info(sanitized_text)
        skills = cls._extract_skills(sanitized_text)
        experiences = cls._extract_experiences(sanitized_text)

        return ExtractedDocument(
            filename=filename,
            file_hash=file_hash,
            raw_text=sanitized_text,
            suggested_profile=suggested_profile,
            skills=skills,
            experiences=experiences,
            injection_signals=injection_signals,
        )

    @classmethod
    def _inspect_zip_limits(cls, file_bytes: bytes) -> zipfile.ZipFile:
        """
        Inspecciona los metadatos del contenedor ZIP ANTES de descomprimir entradas.
        Aplica límites de número de entradas, tamaño total de expansión y razón de compresión
        para prevenir bombas de descompresión (zip bombs) y agotamiento de recursos.
        """
        try:
            z = zipfile.ZipFile(io.BytesIO(file_bytes))
        except zipfile.BadZipFile as bz:
            raise InvalidDocumentError(f"Archivo DOCX corrupto: {bz}") from bz

        infolist = z.infolist()
        if len(infolist) > MAX_ZIP_ENTRIES:
            raise InvalidDocumentError(
                f"El contenedor DOCX contiene demasiadas entradas ({len(infolist)} > {MAX_ZIP_ENTRIES})."
            )

        total_uncompressed = 0
        for entry in infolist:
            total_uncompressed += entry.file_size
            if total_uncompressed > MAX_ZIP_DECOMPRESSED_SIZE:
                raise InvalidDocumentError(
                    f"El tamaño descomprimido del DOCX excede el límite permitido de 20 MB ({total_uncompressed} bytes)."
                )
            if entry.file_size > MAX_ZIP_ENTRY_SIZE:
                raise InvalidDocumentError(
                    f"La entrada '{entry.filename}' excede el límite permitido de 10 MB ({entry.file_size} bytes)."
                )
            if entry.compress_size > 0 and entry.file_size > 1024:
                ratio = entry.file_size / entry.compress_size
                if ratio > MAX_ZIP_COMPRESSION_RATIO:
                    raise InvalidDocumentError(
                        f"Razón de compresión sospechosa ({ratio:.1f}x > {MAX_ZIP_COMPRESSION_RATIO}x) "
                        f"detectada en '{entry.filename}' (posible bomba de descompresión)."
                    )

        return z

    @classmethod
    def _extract_pdf(cls, file_bytes: bytes) -> str:
        """Extrae texto de PDF con comprobación estricta de formato, páginas y capturas de error."""
        if not file_bytes.startswith(b"%PDF"):
            raise InvalidDocumentError("Formato PDF inválido o corrupto: cabecera %PDF ausente.")

        if pypdf is None:
            # Fallback seguro por streams de texto
            try:
                text_chunks = re.findall(rb"\((.*?)\)", file_bytes)
                extracted = " ".join(c.decode("utf-8", errors="ignore") for c in text_chunks)
                if not extracted.strip():
                    raise InvalidDocumentError("No se pudo extraer texto del PDF.")
                return extracted[:MAX_EXTRACTED_TEXT_LENGTH]
            except Exception as e:
                raise InvalidDocumentError(f"Error procesando PDF: {e}") from e

        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            num_pages = len(reader.pages)
            if num_pages == 0:
                raise InvalidDocumentError("El documento PDF no contiene páginas legibles.")

            # Presupuesto de recursos: Límite de páginas
            if num_pages > MAX_PDF_PAGES:
                raise InvalidDocumentError(
                    f"El documento PDF excede el número máximo permitido de páginas ({num_pages} > {MAX_PDF_PAGES})."
                )

            pages_text = []
            total_chars = 0
            for i, page in enumerate(reader.pages):
                try:
                    pt = page.extract_text()
                    if pt:
                        total_chars += len(pt)
                        if total_chars > MAX_EXTRACTED_TEXT_LENGTH:
                            raise InvalidDocumentError(
                                "El texto extraído del PDF excede el límite máximo permitido de recursos."
                            )
                        pages_text.append(pt)
                except InvalidDocumentError:
                    raise
                except Exception as page_err:
                    raise InvalidDocumentError(f"Página {i+1} corrupta en archivo PDF: {page_err}") from page_err

            combined = "\n".join(pages_text).strip()
            if not combined:
                return "PDF importado sin capa de texto seleccionable."
            return combined
        except (pypdf.errors.PyPdfError, InvalidDocumentError) as e:
            if isinstance(e, InvalidDocumentError):
                raise
            raise InvalidDocumentError(f"Archivo PDF corrupto o no válido: {e}") from e
        except Exception as e:
            raise InvalidDocumentError(f"Fallo al leer PDF: {e}") from e

    @classmethod
    def _extract_docx(cls, file_bytes: bytes) -> str:
        """Extrae texto de DOCX validando previamente límites de descompresión y estructura."""
        if not file_bytes.startswith(b"PK\x03\x04"):
            raise InvalidDocumentError("Formato DOCX inválido o corrupto: cabecera ZIP ausente.")

        # Inspección de seguridad previa (límites de entradas, expansión y ratio)
        z = cls._inspect_zip_limits(file_bytes)

        if "word/document.xml" not in z.namelist():
            raise InvalidDocumentError("Estructura DOCX inválida: word/document.xml ausente.")

        if docx is not None:
            try:
                doc = docx.Document(io.BytesIO(file_bytes))
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                tables_text = []
                for table in doc.tables:
                    for row in table.rows:
                        row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if row_text:
                            tables_text.append(" | ".join(row_text))
                combined = "\n".join(paragraphs + tables_text).strip()
                if len(combined) > MAX_EXTRACTED_TEXT_LENGTH:
                    raise InvalidDocumentError("El texto extraído de DOCX excede el límite máximo permitido.")
                return combined or "Documento DOCX sin texto."
            except InvalidDocumentError:
                raise
            except Exception as e:
                raise InvalidDocumentError(f"Archivo DOCX corrupto o estructura no válida: {e}") from e

        # Fallback estándar vía zipfile y document.xml
        try:
            xml_content = z.read("word/document.xml").decode("utf-8", errors="ignore")
            text_parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml_content)
            combined = " ".join(text_parts).strip()
            if len(combined) > MAX_EXTRACTED_TEXT_LENGTH:
                raise InvalidDocumentError("El texto extraído de DOCX excede el límite máximo permitido.")
            return combined
        except Exception as e:
            raise InvalidDocumentError(f"Error procesando DOCX: {e}") from e

    @classmethod
    def _extract_text(cls, file_bytes: bytes) -> str:
        """Extrae texto plano rechazando secuencias binarias o ejecutables."""
        if b"\x00" in file_bytes[:1024]:
            raise InvalidDocumentError("El archivo contiene secuencias binarias no permitidas para texto.")
        try:
            decoded = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                decoded = file_bytes.decode("latin-1")
            except Exception as e:
                raise InvalidDocumentError(f"No se pudo decodificar el archivo de texto: {e}") from e

        if len(decoded) > MAX_EXTRACTED_TEXT_LENGTH:
            raise InvalidDocumentError("El texto del archivo excede el límite máximo permitido de recursos.")
        return decoded

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        """Sanitiza caracteres de control asegurando que el contenido sea tratado como datos."""
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
        return cleaned.strip()

    @classmethod
    def _extract_contact_info(cls, text: str) -> Dict[str, Any]:
        """Extrae de forma determinista sugerencias de datos de contacto sin inventar."""
        info: Dict[str, Any] = {}

        # Email
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
        if email_match:
            info["email"] = email_match.group(0).lower()

        # Teléfono
        phone_match = re.search(r"(\+?[0-9]{1,3}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4}", text)
        if phone_match:
            phone_str = phone_match.group(0).strip()
            if len(re.sub(r"\D", "", phone_str)) >= 8:
                info["phone"] = phone_str

        # Nombre sugerido (primera línea corta no vacía que no parezca título ni email)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for line in lines[:5]:
            if "@" not in line and not line.lower().startswith("curriculum") and not line.lower().startswith("cv"):
                if len(line) <= 60 and not re.search(r"\d{3,}", line):
                    info["full_name"] = line
                    break

        return info

    @classmethod
    def _extract_skills(cls, text: str) -> List[ExtractedSkill]:
        """
        Extrae habilidades detectadas en el texto.
        Garantía determinista:
        - NUNCA inventa años de experiencia (years_experience = None si no está declarado explícitamente).
        - NUNCA inventa nivel de competencia (proficiency = None si no está declarado explícitamente).
        - Distingue menciones de interés/aprendizaje frente a experiencia declarada.
        - Registra fragmento de origen para trazabilidad y procedencia.
        """
        found_skills: List[ExtractedSkill] = []
        lower_text = f" {text.lower()} "

        # Dividir texto en oraciones/líneas para contexto local de cada habilidad
        sentences = [s.strip() for s in re.split(r"[\n\.\;\•]", text) if s.strip()]

        for skill_kw, category in KNOWN_SKILL_CATEGORIES.items():
            pattern = rf"(?:\b|\s){re.escape(skill_kw)}(?:\b|\s)"
            if not re.search(pattern, lower_text):
                continue

            # Localizar la oración o fragmento de origen donde aparece la habilidad
            source_fragment = None
            context_type = "declared"
            extracted_years: Optional[float] = None
            extracted_proficiency: Optional[str] = None

            for sentence in sentences:
                if re.search(pattern, f" {sentence.lower()} "):
                    source_fragment = sentence[:250]
                    # Comprobar si está en contexto de interés, aprendizaje o negación
                    if INTEREST_OR_NEGATION_PATTERN.search(sentence):
                        context_type = "interest"

                    # Intentar extraer años ÚNICAMENTE si están declarados de forma explícita
                    # Ej: "5 años con Python", "3 years of Python experience", "Python (4 años)"
                    years_match = re.search(
                        rf"(\d+(?:\.\d+)?)\s*(?:años|anios|years?)\s*(?:de\s+experiencia\s*(?:en|con)?\s*)?{re.escape(skill_kw)}",
                        sentence,
                        re.IGNORECASE,
                    ) or re.search(
                        rf"{re.escape(skill_kw)}\s*[\(\[]?\s*(\d+(?:\.\d+)?)\s*(?:años|anios|years?)\s*[\)\]]?",
                        sentence,
                        re.IGNORECASE,
                    )
                    if years_match:
                        try:
                            extracted_years = float(years_match.group(1))
                        except ValueError:
                            extracted_years = None

                    # Intentar extraer nivel ÚNICAMENTE si está declarado de forma explícita
                    prof_match = re.search(
                        r"\b(b[aá]sico|basic|principiante|intermedio|intermediate|avanzado|advanced|experto|expert)\b",
                        sentence,
                        re.IGNORECASE,
                    )
                    if prof_match:
                        raw_p = prof_match.group(1).lower()
                        if "bás" in raw_p or "bas" in raw_p or "princ" in raw_p:
                            extracted_proficiency = "beginner"
                        elif "inter" in raw_p:
                            extracted_proficiency = "intermediate"
                        elif "avanz" in raw_p or "adv" in raw_p:
                            extracted_proficiency = "advanced"
                        elif "exp" in raw_p:
                            extracted_proficiency = "expert"

                    break

            display_name = skill_kw.capitalize() if len(skill_kw) > 3 else skill_kw.upper()
            found_skills.append(
                ExtractedSkill(
                    name=display_name,
                    category=category,
                    proficiency=extracted_proficiency,  # None si no está declarado (NUNCA INVENTAR)
                    years_experience=extracted_years,  # None si no está declarado (NUNCA INVENTAR)
                    source_fragment=source_fragment,
                    context_type=context_type,
                    verification_status="pending",  # Extracción != Verificación
                )
            )

        return found_skills

    @classmethod
    def _extract_experiences(cls, text: str) -> List[ExtractedExperience]:
        """
        Extrae bloques de experiencia laboral comprobables a partir de patrones estructurados.
        Garantía determinista:
        - Si el texto indica explícitamente ausencia de experiencia laboral ("sin experiencia laboral"),
          retorna lista vacía.
        - Si no se detectan patrones verificables de empleo con empresa y fechas, retorna lista vacía.
        - NUNCA fabrica empresas ficticias ("Empresa por verificar") ni inventa fechas fijas.
        - Preserva la precisión de fechas cuando solo existe año ("year") o mes ("month").
        """
        experiences: List[ExtractedExperience] = []

        # 1. Comprobar si el texto declara explícitamente no tener experiencia
        if EXPLICIT_NO_EXPERIENCE_PATTERN.search(text):
            return []

        # 2. Patrón de fechas con precisión de año e.g. "2020 - 2023", "2019 - Presente", "2021 a actual"
        pattern_year = re.compile(
            r"([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú0-9\s]{2,40})\s*[-–|]\s*([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú0-9\s]{2,40})\s*\n?\s*(\d{4})\s*[-–to/a]\s*(\d{4}|presente|actual)",
            re.IGNORECASE,
        )

        for match in pattern_year.finditer(text):
            title = match.group(1).strip()
            company = match.group(2).strip()
            start_year = match.group(3)
            end_val = match.group(4).lower()

            start_date = f"{start_year}-01-01"
            end_date = None if any(w in end_val for w in ("presente", "actual")) else f"{end_val}-12-31"

            experiences.append(
                ExtractedExperience(
                    company=company,
                    title=title,
                    start_date=start_date,
                    end_date=end_date,
                    date_precision="year",  # Preservar precisión: solo año
                    source_fragment=match.group(0).strip(),
                    description=f"Puesto: {title} en {company}",
                    skills_used=[],
                    verification_status="pending",
                )
            )

        # 3. Patrón de fechas con precisión de mes e.g. "03/2020 - 08/2023"
        pattern_month = re.compile(
            r"([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú0-9\s]{2,40})\s*[-–|]\s*([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú0-9\s]{2,40})\s*\n?\s*(\d{2})/(\d{4})\s*[-–to/a]\s*(?:(\d{2})/(\d{4})|presente|actual)",
            re.IGNORECASE,
        )

        for match in pattern_month.finditer(text):
            title = match.group(1).strip()
            company = match.group(2).strip()
            start_m, start_y = match.group(3), match.group(4)
            end_m, end_y = match.group(5), match.group(6)

            start_date = f"{start_y}-{start_m}-01"
            end_date = f"{end_y}-{end_m}-28" if end_y and end_m else None

            # Evitar duplicar si coincidió con pattern_year
            if not any(e.company.lower() == company.lower() and e.title.lower() == title.lower() for e in experiences):
                experiences.append(
                    ExtractedExperience(
                        company=company,
                        title=title,
                        start_date=start_date,
                        end_date=end_date,
                        date_precision="month",  # Preservar precisión: mes y año
                        source_fragment=match.group(0).strip(),
                        description=f"Puesto: {title} en {company}",
                        skills_used=[],
                        verification_status="pending",
                    )
                )

        # NUNCA inventar un empleo ficticio si no se detectaron patrones de empleo
        return experiences
