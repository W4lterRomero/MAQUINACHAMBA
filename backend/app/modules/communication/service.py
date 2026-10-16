"""
Servicio del dominio de Comunicación y Estilo (REQ-006, D10).
Principios clave:
1. Determinismo primero: generación mediante reglas sin invocar IA ni dependencias externas.
2. No inventar datos: extraer != verificar. La generación se basa estrictamente
   en hechos profesionales con 'verification_status == verified'.
3. Aislamiento por tenant: todas las consultas y mutaciones filtran por tenant_id.
4. Sanitización estricta: ejemplos de usuario validados contra inyección de prompts.
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import detect_injection_signals
from backend.app.modules.communication.models import CommunicationProfile
from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy


class CommunicationServiceError(Exception):
    """Excepción base para el módulo de comunicación."""
    pass


class CommunicationService:
    """Servicio para gestión de perfiles de comunicación y generación determinista de mensajes."""

    VALID_TONES = {"formal", "semiformal", "casual"}
    VALID_CONCISENESS = {"concise", "standard", "detailed"}
    VALID_FOCUS_AREAS = {
        "technical_depth",
        "problem_solving",
        "leadership_and_collaboration",
        "general_match",
    }

    @classmethod
    async def get_or_create_profile(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: Optional[str] = None,
    ) -> CommunicationProfile:
        """
        Recupera el perfil de comunicación para el tenant o crea uno con valores por defecto.
        Garantiza aislamiento estricto por tenant_id.
        """
        stmt = (
            select(CommunicationProfile)
            .where(CommunicationProfile.tenant_id == tenant_id)
            .order_by(CommunicationProfile.created_at.asc())
        )
        res = await session.execute(stmt)
        comm_profile = res.scalars().first()

        if comm_profile is None:
            # Si no se pasó profile_id, buscar el perfil de usuario del tenant
            if not profile_id:
                prof_stmt = select(UserProfile.id).where(UserProfile.tenant_id == tenant_id)
                prof_res = await session.execute(prof_stmt)
                profile_id = prof_res.scalars().first()

            comm_profile = CommunicationProfile(
                tenant_id=tenant_id,
                profile_id=profile_id,
                tone="formal",
                language_preferences={
                    "primary_language": "es",
                    "bilingual_mode": "auto",
                    "prefer_spanish_for_latam": True,
                },
                writing_rules={
                    "conciseness": "standard",
                    "include_portfolio": True,
                    "highlight_key_metrics": True,
                    "custom_instructions": [],
                },
                questionnaire_answers={
                    "greeting_style": "default",
                    "focus_area": "technical_depth",
                    "mention_availability": True,
                    "mention_work_mode": True,
                },
                example_texts=[],
                status="approved",
                approved_at=datetime.now(timezone.utc),
            )
            session.add(comm_profile)
            await session.flush()

        return comm_profile

    @classmethod
    async def update_profile(
        cls,
        session: AsyncSession,
        tenant_id: str,
        tone: Optional[str] = None,
        language_preferences: Optional[Dict[str, Any]] = None,
        writing_rules: Optional[Dict[str, Any]] = None,
        questionnaire_answers: Optional[Dict[str, Any]] = None,
        example_texts: Optional[List[Dict[str, Any]]] = None,
        status: Optional[str] = None,
    ) -> CommunicationProfile:
        """
        Actualiza el perfil de comunicación del tenant aplicando validaciones y sanitización.
        """
        comm_profile = await cls.get_or_create_profile(session, tenant_id)

        if tone is not None:
            clean_tone = tone.strip().lower()
            if clean_tone not in cls.VALID_TONES:
                raise CommunicationServiceError(
                    f"Tono inválido '{tone}'. Valores permitidos: {sorted(cls.VALID_TONES)}"
                )
            comm_profile.tone = clean_tone

        if language_preferences is not None:
            comm_profile.language_preferences = dict(language_preferences)

        if writing_rules is not None:
            rules = dict(writing_rules)
            conciseness = rules.get("conciseness", "standard")
            if conciseness not in cls.VALID_CONCISENESS:
                rules["conciseness"] = "standard"
            comm_profile.writing_rules = rules

        if questionnaire_answers is not None:
            q_answers = dict(questionnaire_answers)
            focus = q_answers.get("focus_area", "technical_depth")
            if focus not in cls.VALID_FOCUS_AREAS:
                q_answers["focus_area"] = "technical_depth"
            comm_profile.questionnaire_answers = q_answers

        if example_texts is not None:
            sanitized_examples = []
            for item in example_texts:
                title = str(item.get("title", "")).strip()[:100]
                content = str(item.get("content", "")).strip()
                # Detección de inyección de prompts
                injections = detect_injection_signals(content)
                if injections:
                    raise CommunicationServiceError(
                        f"El ejemplo '{title}' contiene patrones de inyección o comandos no permitidos."
                    )
                # Sanitización de caracteres de control
                clean_content = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", content)[:5000]
                sanitized_examples.append({
                    "id": item.get("id") or str(uuid.uuid4())[:8],
                    "title": title or "Ejemplo de comunicación",
                    "content": clean_content,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
            comm_profile.example_texts = sanitized_examples

        if status is not None:
            clean_status = status.strip().lower()
            if clean_status in {"draft", "approved"}:
                comm_profile.status = clean_status
                if clean_status == "approved":
                    comm_profile.approved_at = datetime.now(timezone.utc)

        await session.flush()
        return comm_profile

    @classmethod
    def extract_example_style_signals(
        cls, example_texts: Optional[List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        """
        Extrae señales explícitas y revisables de estilo a partir de los ejemplos proporcionados por el usuario.
        REGLA DE ORO DE SEGURIDAD Y VERACIDAD:
        - NUNCA traslada hechos personales, nombres, empresas, cargos o contenidos literales de los ejemplos al mensaje generado.
        - Extrae únicamente parámetros estilísticos y estructurales observables:
          * bullet_cues (bool): si el usuario acostumbra usar viñetas ('-', '*', '•', '1.').
          * salutation_cue (Optional[str]): 'direct_recipient' | 'team_friendly' | 'formal' | None.
          * signoff_cue (Optional[str]): 'attentive' | 'cordial' | 'warm' | None.
          * paragraph_density (str): 'compact' | 'standard'.
        """
        signals: Dict[str, Any] = {
            "has_examples": False,
            "bullet_cues": False,
            "salutation_cue": None,
            "signoff_cue": None,
            "paragraph_density": "standard",
        }
        if not example_texts:
            return signals

        combined_text = "\n\n".join(
            str(ex.get("content", "")) for ex in example_texts if isinstance(ex, dict)
        )
        if not combined_text.strip():
            return signals

        signals["has_examples"] = True

        # 1. Señal de viñetas / estructura por puntos
        if re.search(r"(?m)^\s*([-*•]|\d+\.)\s+", combined_text):
            signals["bullet_cues"] = True

        # 2. Señal de saludo observable
        lower_text = combined_text.lower()
        if re.search(r"\b(estimado/a\s+[a-záéíóúñ]+|a\s+la\s+atención\s+de)\b", lower_text):
            signals["salutation_cue"] = "direct_recipient"
        elif re.search(r"\b(hola\s+equipo|buen\s+día\s+equipo|saludos\s+al\s+equipo)\b", lower_text):
            signals["salutation_cue"] = "team_friendly"
        elif re.search(r"\b(estimado\s+equipo|distinguido)\b", lower_text):
            signals["salutation_cue"] = "formal"

        # 3. Señal de despedida observable
        if re.search(r"\b(atentamente|le\s+saluda\s+atentamente|quedo\s+atento)\b", lower_text):
            signals["signoff_cue"] = "attentive"
        elif re.search(r"\b(saludos\s+cordiales|un\s+cordial\s+saludo|cordialmente)\b", lower_text):
            signals["signoff_cue"] = "cordial"
        elif re.search(r"\b(muchas\s+gracias|un\s+abrazo|saludos)\b", lower_text):
            signals["signoff_cue"] = "warm"

        # 4. Densidad de párrafos
        paras = [p.strip() for p in combined_text.split("\n\n") if p.strip()]
        if paras:
            avg_words = sum(len(p.split()) for p in paras) / len(paras)
            if avg_words < 30:
                signals["paragraph_density"] = "compact"

        return signals

    @classmethod
    def generate_deterministic_cover_letter(
        cls,
        comm_profile: CommunicationProfile,
        profile: UserProfile,
        cv: Optional[MasterCV],
        vacancy_title: str,
        company_name: str,
        verified_skills: List[str],
        verified_experience_summary: Optional[str] = None,
        portfolio_url: Optional[str] = None,
    ) -> Tuple[str, List[str], Dict[str, Any]]:
        """
        Generador determinista puro de carta / mensaje de candidatura.
        Reglas estrictas:
        - CERO llamadas a IA externa (determinismo primero, REQ-011).
        - No inventa hechos profesionales: cita únicamente habilidades y experiencias verificadas.
        - Modela tono, concisión, foco de valor y reglas de redacción.
        - Señales de estilo explícitas y revisables derivadas de ejemplos (sin copiar hechos ajenos).
        """
        rules_applied: List[str] = []

        example_signals = cls.extract_example_style_signals(comm_profile.example_texts)

        facts_used: Dict[str, Any] = {
            "candidate_name": profile.full_name,
            "candidate_city": profile.location_city,
            "candidate_country": profile.location_country,
            "vacancy_title": vacancy_title,
            "company_name": company_name,
            "verified_skills": list(verified_skills),
            "verified_experience": verified_experience_summary,
            "portfolio_url": portfolio_url,
            "example_style_signals": example_signals,
        }

        if example_signals["has_examples"]:
            rules_applied.append("example_style_signals_applied")
            if example_signals["bullet_cues"]:
                rules_applied.append("example_signal_bullet_cues")
            if example_signals["salutation_cue"]:
                rules_applied.append(f"example_signal_salutation_{example_signals['salutation_cue']}")
            if example_signals["signoff_cue"]:
                rules_applied.append(f"example_signal_signoff_{example_signals['signoff_cue']}")

        tone = comm_profile.tone if comm_profile.tone in cls.VALID_TONES else "formal"
        rules = comm_profile.writing_rules or {}
        conciseness = rules.get("conciseness", "standard")
        if conciseness not in cls.VALID_CONCISENESS:
            conciseness = "standard"

        q_answers = comm_profile.questionnaire_answers or {}
        greeting_style = q_answers.get("greeting_style", "default")
        focus_area = q_answers.get("focus_area", "technical_depth")
        mention_avail = q_answers.get("mention_availability", True)

        rules_applied.append(f"tone_{tone}")
        rules_applied.append(f"conciseness_{conciseness}")
        rules_applied.append(f"focus_{focus_area}")

        # 1. Saludo inicial (considera señal de saludo de ejemplos o cuestionario/tono)
        sal_cue = example_signals.get("salutation_cue")
        if sal_cue == "direct_recipient" or greeting_style == "recipient_direct":
            greeting = f"Estimado/a responsable de selección de {company_name},"
        elif sal_cue == "team_friendly" or greeting_style == "team_direct":
            greeting = f"Hola equipo de {company_name},"
        elif sal_cue == "formal":
            greeting = f"Estimado equipo de selección de {company_name},"
        elif tone == "formal":
            greeting = f"Estimado equipo de selección de {company_name},"
        elif tone == "semiformal":
            greeting = f"Estimado equipo de {company_name},"
        else:  # casual
            greeting = f"¡Hola equipo de {company_name}!"

        # 2. Párrafo de apertura
        if tone == "formal":
            opening = (
                f"Me dirijo a ustedes con el propósito de presentar formalmente mi candidatura "
                f"para la posición de {vacancy_title}."
            )
        elif tone == "semiformal":
            opening = (
                f"Les escribo con gran interés para postularme a la vacante de {vacancy_title} en {company_name}."
            )
        else:  # casual
            opening = (
                f"Les escribo porque me entusiasma mucho la oportunidad para la posición de {vacancy_title} en {company_name}."
            )

        # 3. Párrafo de competencias y experiencia verificada (NUNCA INVENTAR)
        has_experience = bool(verified_experience_summary)
        skills_phrase = ""
        if verified_skills:
            rules_applied.append("verified_skills_included")
            top_skills = verified_skills[:5]
            skills_str = ", ".join(top_skills[:-1]) + (" y " + top_skills[-1] if len(top_skills) > 1 else top_skills[0])

            # Si los ejemplos indican preferencia por viñetas
            if example_signals.get("bullet_cues") and len(top_skills) >= 1:
                rules_applied.append("bulleted_skills_formatted")
                bullets = "\n".join(f"• Competencia verificada: {sk}" for sk in top_skills)
                if has_experience:
                    skills_phrase = f"Cuento con las siguientes competencias técnicas verificadas, respaldadas por mi experiencia:\n{bullets}"
                else:
                    skills_phrase = f"Cuento con las siguientes competencias técnicas verificadas:\n{bullets}"
            else:
                if has_experience:
                    if tone == "formal":
                        skills_phrase = f"Cuento con competencias técnicas verificadas en {skills_str}, respaldadas por mi trayectoria laboral."
                    elif tone == "semiformal":
                        skills_phrase = f"En mi trayectoria profesional he trabajado con {skills_str}."
                    else:
                        skills_phrase = f"Tengo experiencia práctica comprobada trabajando con {skills_str}."
                else:
                    # Redacción estrictamente neutral: evitar exagerar a 'experiencia práctica comprobada' o 'me he especializado'
                    if tone == "formal":
                        skills_phrase = f"Cuento con competencias técnicas verificadas en {skills_str}."
                    elif tone == "semiformal":
                        skills_phrase = f"Cuento con conocimientos técnicos verificados en {skills_str}."
                    else:
                        skills_phrase = f"Manejo competencias técnicas verificadas en {skills_str}."
        else:
            rules_applied.append("no_verified_skills_found_fallback_to_specialization")
            spec = (cv.specialization if cv and cv.specialization else "desarrollo y tecnología")
            if tone == "formal":
                skills_phrase = f"Mi perfil está orientado a {spec}, con un sólido compromiso de rigor técnico y aprendizaje continuo."
            elif tone == "semiformal":
                skills_phrase = f"Mi especialidad es {spec}, enfocado en aportar valor desde el primer día."
            else:
                skills_phrase = f"Mi foco principal es {spec}, siempre con ganas de sumar y aprender."

        exp_phrase = ""
        if verified_experience_summary:
            rules_applied.append("verified_experience_included")
            if tone == "formal":
                exp_phrase = f" Asimismo, {verified_experience_summary}."
            elif tone == "semiformal":
                exp_phrase = f" Recientemente, {verified_experience_summary}."
            else:
                exp_phrase = f" En mi experiencia previa, {verified_experience_summary}."

        body_skills_exp = f"{skills_phrase}{exp_phrase}".strip()

        # 4. Foco de valor según cuestionario
        focus_phrase = ""
        if focus_area == "technical_depth":
            if tone == "formal":
                focus_phrase = (
                    "Mi prioridad técnica radica en el diseño de arquitecturas limpias, código mantenible "
                    "y soluciones escalables bajo altos estándares de calidad."
                )
            elif tone == "semiformal":
                focus_phrase = (
                    "Me enfoco en construir soluciones robustas y mantenibles, asegurando que cada entrega "
                    "sea sólida y aporte a la estabilidad del producto."
                )
            else:
                focus_phrase = (
                    "Me apasiona construir código limpio, resolver problemas de raíz y entregar herramientas "
                    "que realmente funcionen bien."
                )
        elif focus_area == "problem_solving":
            if tone == "formal":
                focus_phrase = (
                    "Destaco por una aproximación analítica frente a problemas complejos, priorizando el diagnóstico "
                    "preciso y la resolución sistemática orientada a objetivos de negocio."
                )
            elif tone == "semiformal":
                focus_phrase = (
                    "Mi fortaleza principal es resolver problemas complejos de forma metódica y práctica, "
                    "desbloqueando retos técnicos con rapidez."
                )
            else:
                focus_phrase = (
                    "Disfruto entrarle a problemas difíciles, encontrar la causa raíz y resolverlos sin dar rodeos."
                )
        elif focus_area == "leadership_and_collaboration":
            if tone == "formal":
                focus_phrase = (
                    "Promuevo la colaboración interdisciplinaria, la comunicación transparente y la alineación "
                    "estratégica entre el equipo técnico y las prioridades de la institución."
                )
            elif tone == "semiformal":
                focus_phrase = (
                    "Valoro especialmente el trabajo en equipo, la comunicación clara y el intercambio continuo "
                    "de conocimientos entre pares."
                )
            else:
                focus_phrase = (
                    "Me encanta hacer equipo, compartir lo que sé, aprender de los demás y mantener una vibra de trabajo abierta."
                )
        else:  # general_match
            if tone == "formal":
                focus_phrase = (
                    "Considero que mi perfil profesional y trayectoria se alinean de manera directa con las expectativas "
                    "del puesto."
                )
            elif tone == "semiformal":
                focus_phrase = (
                    "Considero que mi experiencia encaja naturalmente con los desafíos de esta posición."
                )
            else:
                focus_phrase = (
                    "Creo que mi perfil hace un muy buen clic con lo que están buscando para este rol."
                )

        # 5. Logística, disponibilidad y enlaces
        logistics = []
        if mention_avail and profile.availability:
            rules_applied.append("availability_mentioned")
            avail_str = "inmediata" if profile.availability in {"immediate", "inmediata"} else profile.availability
            if tone == "formal":
                logistics.append(f"Cuento con disponibilidad {avail_str} para incorporarme al equipo.")
            else:
                logistics.append(f"Tengo disponibilidad {avail_str} para iniciar.")

        if rules.get("include_portfolio") and portfolio_url:
            rules_applied.append("portfolio_url_included")
            if tone == "formal":
                logistics.append(f"Pueden consultar muestras verificables de mi trabajo en: {portfolio_url}")
            else:
                logistics.append(f"Pueden ver más sobre mis proyectos en: {portfolio_url}")

        logistics_phrase = " ".join(logistics)

        # 6. Cierre y despedida
        if tone == "formal":
            closing = (
                "Agradezco de antemano el tiempo dedicado a revisar mi postulación y quedo a su entera disposición "
                "para profundizar sobre mi trayectoria en una entrevista."
            )
            signoff = f"Le saluda atentamente,\n{profile.full_name}"
        elif tone == "semiformal":
            closing = (
                "Quedo a su disposición para conversar sobre cómo mi experiencia puede contribuir al equipo. "
                "Muchas gracias por su consideración."
            )
            signoff = f"Saludos cordiales,\n{profile.full_name}"
        else:  # casual
            closing = (
                "¡Muchas gracias por su tiempo y consideración! Me encantaría platicar pronto con el equipo."
            )
            signoff = f"Un saludo cordial,\n{profile.full_name}"

        # Ajuste de despedida según señales explícitas de ejemplos si existen
        signoff_cue = example_signals.get("signoff_cue")
        if signoff_cue == "attentive":
            signoff = f"Le saluda atentamente,\n{profile.full_name}"
        elif signoff_cue == "cordial":
            signoff = f"Saludos cordiales,\n{profile.full_name}"
        elif signoff_cue == "warm":
            signoff = f"Un saludo cordial,\n{profile.full_name}"

        # 7. Estructuración según concisión
        if conciseness == "concise":
            # 2 párrafos compactos
            p1 = f"{opening} {body_skills_exp}"
            p2 = f"{focus_phrase} {logistics_phrase} {closing}".strip()
            content = f"{greeting}\n\n{p1}\n\n{p2}\n\n{signoff}"
        elif conciseness == "detailed":
            # 4 párrafos desarrollados
            p1 = opening
            p2 = body_skills_exp
            p3 = f"{focus_phrase} {logistics_phrase}".strip()
            p4 = closing
            content = f"{greeting}\n\n{p1}\n\n{p2}\n\n{p3}\n\n{p4}\n\n{signoff}"
        else:  # standard
            # 3 párrafos equilibrados
            p1 = opening
            p2 = f"{body_skills_exp} {focus_phrase}".strip()
            p3 = f"{logistics_phrase} {closing}".strip() if logistics_phrase else closing
            content = f"{greeting}\n\n{p1}\n\n{p2}\n\n{p3}\n\n{signoff}"

        return content, rules_applied, facts_used

    @classmethod
    async def preview_message(
        cls,
        session: AsyncSession,
        tenant_id: str,
        vacancy_id: Optional[str] = None,
        custom_vacancy_title: Optional[str] = None,
        custom_company_name: Optional[str] = None,
        override_tone: Optional[str] = None,
        override_conciseness: Optional[str] = None,
        override_focus_area: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Genera la vista previa interactiva en tiempo real del mensaje determinista
        según el perfil de comunicación y los hechos verificados del usuario.
        """
        comm_profile = await cls.get_or_create_profile(session, tenant_id)

        # Si hay overrides temporales de vista previa, clonar objeto ligero
        active_tone = override_tone or comm_profile.tone
        active_writing_rules = dict(comm_profile.writing_rules or {})
        if override_conciseness:
            active_writing_rules["conciseness"] = override_conciseness

        active_questionnaire = dict(comm_profile.questionnaire_answers or {})
        if override_focus_area:
            active_questionnaire["focus_area"] = override_focus_area

        # Obtener perfil de usuario
        prof_stmt = select(UserProfile).where(UserProfile.tenant_id == tenant_id)
        prof_res = await session.execute(prof_stmt)
        profile = prof_res.scalars().first()
        if not profile:
            raise CommunicationServiceError("No existe un perfil de usuario registrado para este tenant.")

        # Obtener CV maestro activo
        cv_stmt = (
            select(MasterCV)
            .where(MasterCV.tenant_id == tenant_id, MasterCV.is_active.is_(True))
            .order_by(MasterCV.updated_at.desc())
        )
        cv_res = await session.execute(cv_stmt)
        cv = cv_res.scalars().first()

        # Obtener habilidades verificadas (verification_status == 'verified')
        skills_stmt = (
            select(Skill.name)
            .where(Skill.tenant_id == tenant_id, Skill.verification_status == "verified")
            .order_by(Skill.created_at.asc())
        )
        skills_res = await session.execute(skills_stmt)
        verified_skills = [s for s in skills_res.scalars().all()]

        # Obtener experiencia verificada más reciente
        exp_stmt = (
            select(Experience)
            .where(Experience.tenant_id == tenant_id, Experience.verification_status == "verified")
            .order_by(Experience.start_date.desc().nullslast())
        )
        exp_res = await session.execute(exp_stmt)
        top_exp = exp_res.scalars().first()
        verified_exp_summary = None
        if top_exp:
            verified_exp_summary = f"me he desempeñado como {top_exp.title} en {top_exp.company}"

        # Resolver datos de vacante
        vacancy_title = custom_vacancy_title
        company_name = custom_company_name

        if vacancy_id:
            vac_stmt = select(Vacancy).where(Vacancy.tenant_id == tenant_id, Vacancy.id == vacancy_id)
            vac_res = await session.execute(vac_stmt)
            vac = vac_res.scalars().first()
            if vac:
                vacancy_title = vac.job_title
                company_name = vac.company_name

        if not vacancy_title:
            vacancy_title = "Desarrollador de Software"
        if not company_name:
            company_name = "Empresa Tecnológica"

        portfolio_url = (profile.job_preferences or {}).get("portfolio_url")

        # Mock de comm_profile temporal para aplicar overrides si existen
        temp_comm = CommunicationProfile(
            tenant_id=tenant_id,
            tone=active_tone,
            writing_rules=active_writing_rules,
            questionnaire_answers=active_questionnaire,
            example_texts=comm_profile.example_texts,
        )

        letter, rules_applied, facts_used = cls.generate_deterministic_cover_letter(
            comm_profile=temp_comm,
            profile=profile,
            cv=cv,
            vacancy_title=vacancy_title,
            company_name=company_name,
            verified_skills=verified_skills,
            verified_experience_summary=verified_exp_summary,
            portfolio_url=portfolio_url,
        )

        return {
            "preview_text": letter,
            "tone": active_tone,
            "rules_applied": rules_applied,
            "facts_used": facts_used,
            "verified_skills_count": len(verified_skills),
            "has_verified_experience": verified_exp_summary is not None,
        }

    @classmethod
    async def generate_cover_letter_for_candidacy(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile: UserProfile,
        cv: MasterCV,
        vacancy: Vacancy,
    ) -> str:
        """
        Conecta el estilo configurado del perfil de comunicación al borrador
        de la candidatura previo a aprobación (REQ-006, REQ-014).
        """
        comm_profile = await cls.get_or_create_profile(session, tenant_id)

        # Obtener habilidades verificadas
        skills_stmt = (
            select(Skill.name)
            .where(Skill.tenant_id == tenant_id, Skill.verification_status == "verified")
            .order_by(Skill.created_at.asc())
        )
        skills_res = await session.execute(skills_stmt)
        verified_skills = [s for s in skills_res.scalars().all()]

        # Si el CV tiene secciones con lista de habilidades verificadas específicas, priorizarlas
        if cv and cv.sections and "skills" in cv.sections:
            cv_skills = cv.sections["skills"]
            if isinstance(cv_skills, list) and cv_skills:
                verified_skills = [s for s in cv_skills if s in verified_skills] or verified_skills

        # Experiencia verificada
        exp_stmt = (
            select(Experience)
            .where(Experience.tenant_id == tenant_id, Experience.verification_status == "verified")
            .order_by(Experience.start_date.desc().nullslast())
        )
        exp_res = await session.execute(exp_stmt)
        top_exp = exp_res.scalars().first()
        verified_exp_summary = None
        if top_exp:
            verified_exp_summary = f"me he desempeñado como {top_exp.title} en {top_exp.company}"

        portfolio_url = (profile.job_preferences or {}).get("portfolio_url")

        letter, _, _ = cls.generate_deterministic_cover_letter(
            comm_profile=comm_profile,
            profile=profile,
            cv=cv,
            vacancy_title=vacancy.job_title,
            company_name=vacancy.company_name,
            verified_skills=verified_skills,
            verified_experience_summary=verified_exp_summary,
            portfolio_url=portfolio_url,
        )

        return letter
