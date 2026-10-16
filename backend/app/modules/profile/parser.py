"""
Extractor seguro de documentos de CV para Máquina de Chamba (Hito 2.1).
Principio: Extracción separada de verificación (extraer != verificar).
Soporta: PDF, DOCX, TXT.
Defensas fail-closed:
- Límite estricto de tamaño (5 MB).
- Validación de tipo y magic bytes.
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


MAX_CV_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

# Diccionario curado de tecnologías y habilidades para extracción determinista
KNOWN_SKILLS = {
    "python": ("technical", 3.0),
    "fastapi": ("technical", 2.0),
    "django": ("technical", 2.0),
    "flask": ("technical", 2.0),
    "sqlite": ("technical", 2.0),
    "postgresql": ("technical", 3.0),
    "mysql": ("technical", 2.0),
    "docker": ("technical", 2.0),
    "kubernetes": ("technical", 2.0),
    "linux": ("technical", 3.0),
    "git": ("technical", 3.0),
    "bash": ("technical", 2.0),
    "react": ("technical", 2.0),
    "typescript": ("technical", 2.0),
    "javascript": ("technical", 3.0),
    "aws": ("technical", 2.0),
    "gcp": ("technical", 2.0),
    "azure": ("technical", 2.0),
    "rest": ("technical", 3.0),
    "graphql": ("technical", 2.0),
    "ci/cd": ("technical", 2.0),
    "redis": ("technical", 2.0),
    "sqlalchemy": ("technical", 2.0),
    "pytest": ("technical", 2.0),
    "scrum": ("soft", 2.0),
    "agile": ("soft", 2.0),
    "liderazgo": ("soft", 3.0),
    "comunicación": ("soft", 3.0),
    "inglés": ("language", 3.0),
    "español": ("language", 5.0),
}


class InvalidDocumentError(ValueError):
    """Excepción lanzada cuando un archivo es corrupto, no soportado o inválido."""
    pass


@dataclass
class ExtractedSkill:
    name: str
    category: str = "technical"
    proficiency: str = "intermediate"
    years_experience: float = 1.0
    verification_status: str = "pending"  # Siempre pending por defecto


@dataclass
class ExtractedExperience:
    company: str
    title: str
    start_date: str  # YYYY-MM-DD
    end_date: Optional[str] = None  # None = actual
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
    """Parser local y seguro para documentos curriculares."""

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

        # 3. Extracción de texto según formato
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

        # 5. Extracción determinista de entidades
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
    def _extract_pdf(cls, file_bytes: bytes) -> str:
        """Extrae texto de PDF con comprobación estricta de formato y capturas de error."""
        if not file_bytes.startswith(b"%PDF"):
            raise InvalidDocumentError("Formato PDF inválido o corrupto: cabecera %PDF ausente.")

        if pypdf is None:
            # Fallback seguro por streams de texto
            try:
                text_chunks = re.findall(rb"\((.*?)\)", file_bytes)
                extracted = " ".join(c.decode("utf-8", errors="ignore") for c in text_chunks)
                if not extracted.strip():
                    raise InvalidDocumentError("No se pudo extraer texto del PDF.")
                return extracted
            except Exception as e:
                raise InvalidDocumentError(f"Error procesando PDF: {e}") from e

        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            if len(reader.pages) == 0:
                raise InvalidDocumentError("El documento PDF no contiene páginas legibles.")
            pages_text = []
            for i, page in enumerate(reader.pages):
                try:
                    pt = page.extract_text()
                    if pt:
                        pages_text.append(pt)
                except Exception as page_err:
                    raise InvalidDocumentError(f"Página {i+1} corrupta en archivo PDF: {page_err}") from page_err
            combined = "\n".join(pages_text).strip()
            if not combined:
                # Si el PDF es válido pero sin capa de texto seleccionable
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
        """Extrae texto de DOCX validando integridad de contenedor ZIP y XML."""
        if not file_bytes.startswith(b"PK\x03\x04"):
            raise InvalidDocumentError("Formato DOCX inválido o corrupto: cabecera ZIP ausente.")

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
                return combined or "Documento DOCX sin texto."
            except Exception as e:
                raise InvalidDocumentError(f"Archivo DOCX corrupto o estructura no válida: {e}") from e

        # Fallback estándar vía zipfile y document.xml
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if "word/document.xml" not in z.namelist():
                    raise InvalidDocumentError("Estructura DOCX inválida: word/document.xml ausente.")
                xml_content = z.read("word/document.xml").decode("utf-8", errors="ignore")
                # Extraer texto de etiquetas <w:t>
                text_parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml_content)
                return " ".join(text_parts).strip()
        except zipfile.BadZipFile as bz:
            raise InvalidDocumentError(f"Archivo DOCX corrupto: {bz}") from bz
        except Exception as e:
            raise InvalidDocumentError(f"Error procesando DOCX: {e}") from e

    @classmethod
    def _extract_text(cls, file_bytes: bytes) -> str:
        """Extrae texto plano rechazando secuencias binarias o ejecutables."""
        if b"\x00" in file_bytes[:1024]:
            raise InvalidDocumentError("El archivo contiene secuencias binarias no permitidas para texto.")
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_bytes.decode("latin-1")
            except Exception as e:
                raise InvalidDocumentError(f"No se pudo decodificar el archivo de texto: {e}") from e

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        """Sanitiza caracteres de control y secuencias sospechosas asegurando que sea tratado como datos."""
        # Eliminar caracteres de control nulos o invisibles excepto saltos de línea y tabuladores
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
        return cleaned.strip()

    @classmethod
    def _extract_contact_info(cls, text: str) -> Dict[str, Any]:
        """Extrae de forma determinista sugerencias de datos de contacto."""
        info: Dict[str, Any] = {}

        # Email
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
        if email_match:
            info["email"] = email_match.group(0).lower()

        # Teléfono simple
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
        """Extrae habilidades mediante coincidencia exacta o por límites de palabra."""
        found_skills: List[ExtractedSkill] = []
        lower_text = f" {text.lower()} "

        for skill_kw, (category, years) in KNOWN_SKILLS.items():
            # Buscar coincidencia respetando fronteras de palabras
            pattern = rf"(?:\b|\s){re.escape(skill_kw)}(?:\b|\s)"
            if re.search(pattern, lower_text):
                found_skills.append(
                    ExtractedSkill(
                        name=skill_kw.capitalize() if len(skill_kw) > 3 else skill_kw.upper(),
                        category=category,
                        proficiency="intermediate",
                        years_experience=years,
                        verification_status="pending",  # Extracción != Verificación
                    )
                )

        return found_skills

    @classmethod
    def _extract_experiences(cls, text: str) -> List[ExtractedExperience]:
        """Extrae bloques de experiencia laboral utilizando patrones heurísticos deterministas."""
        experiences: List[ExtractedExperience] = []

        # Buscar bloques con patrones de año e.g. "2020 - 2023", "2019 - Presente"
        pattern = re.compile(
            r"([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú\s]{2,40})\s*[-–|]\s*([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóú\s]{2,40})\s*\n?\s*(\d{4})\s*[-–to/a]\s*(\d{4}|presente|actual)",
            re.IGNORECASE,
        )

        for match in pattern.finditer(text):
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
                    description=f"Puesto: {title} en {company}",
                    skills_used=[],
                    verification_status="pending",  # Extracción != Verificación
                )
            )

        # Si no se detectan por patrón estricto, crear una experiencia preliminar pendiente si hay texto
        if not experiences and len(text.strip()) > 50:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) >= 2:
                experiences.append(
                    ExtractedExperience(
                        company="Empresa por verificar",
                        title=lines[0][:60],
                        start_date="2022-01-01",
                        end_date=None,
                        description=lines[1][:250] if len(lines) > 1 else "",
                        skills_used=[],
                        verification_status="pending",
                    )
                )

        return experiences
