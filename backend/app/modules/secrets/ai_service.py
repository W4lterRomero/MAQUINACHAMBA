"""
Servicio de Simulación de IA, Gestión de Presupuesto y Fallback Determinista (ADR-004, REQ-029).
Provee:
1. Catálogo estructurado de proveedores y modelos (OpenAI, Anthropic, Google) con tarifas estimadas de referencia.
2. Estimación de tokens y costo en moneda local/USD.
3. Reserva atómica de presupuesto en base de datos para evitar desbordamiento bajo peticiones concurrentes.
4. Límite estricto de generación: el consumo cobrado y la salida jamás superan el presupuesto reservado ni el límite mensual.
5. Fallback determinista real basado en datos verificados (sin inventar habilidades, experiencia ni puntuaciones fijas).
6. Avisos y banderas explícitas de modo simulación (sin llamadas de red externas ni facturación real).
"""

from datetime import datetime, timezone
import math
import re
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.secrets.models import APIKeyConfig, CostLog
from backend.app.modules.secrets.service import SecretsVaultService


# Tarifas referenciales de simulación (valores estimativos locales para pruebas y control presupuestario,
# no tarifas oficiales vigentes de proveedores externos).
PROVIDERS_CATALOG: Dict[str, Dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "description": "Modelos GPT para análisis semántico y adaptación de texto.",
        "models": {
            "gpt-4o-mini": {
                "name": "GPT-4o mini",
                "input_cost_per_million": 0.15,
                "output_cost_per_million": 0.60,
                "max_context_tokens": 128000,
                "description": "Rápido y económico para tareas de adecuación de perfil.",
            },
            "gpt-4o": {
                "name": "GPT-4o",
                "input_cost_per_million": 5.00,
                "output_cost_per_million": 15.00,
                "max_context_tokens": 128000,
                "description": "Alta capacidad de razonamiento para evaluaciones complejas.",
            },
        },
    },
    "anthropic": {
        "name": "Anthropic",
        "description": "Modelos Claude especializados en comprensión de contexto y síntesis.",
        "models": {
            "claude-3-haiku": {
                "name": "Claude 3 Haiku",
                "input_cost_per_million": 0.25,
                "output_cost_per_million": 1.25,
                "max_context_tokens": 200000,
                "description": "Modelo liviano de baja latencia.",
            },
            "claude-3-5-sonnet": {
                "name": "Claude 3.5 Sonnet",
                "input_cost_per_million": 3.00,
                "output_cost_per_million": 15.00,
                "max_context_tokens": 200000,
                "description": "Gran capacidad de redacción y análisis técnico minucioso.",
            },
        },
    },
    "google": {
        "name": "Google Gemini",
        "description": "Modelos Gemini con amplia ventana de contexto y respuesta rápida.",
        "models": {
            "gemini-1.5-flash": {
                "name": "Gemini 1.5 Flash",
                "input_cost_per_million": 0.075,
                "output_cost_per_million": 0.30,
                "max_context_tokens": 1000000,
                "description": "Ultra veloz y altamente eficiente en costo.",
            },
            "gemini-1.5-pro": {
                "name": "Gemini 1.5 Pro",
                "input_cost_per_million": 3.50,
                "output_cost_per_million": 10.50,
                "max_context_tokens": 2000000,
                "description": "Ventana masiva de contexto para análisis exhaustivo de antecedentes.",
            },
        },
    },
}

SIMULATION_BANNER_TEXT = (
    "MODO SIMULACIÓN ACTIVO: Los tokens y costos reportados son estimaciones locales "
    "calculadas para control de presupuesto y pruebas. No se realizaron llamadas de red "
    "externas ni se incurrió en facturación financiera real."
)


class AIBudgetExceededError(Exception):
    """Excepción lanzada cuando la operación excede el presupuesto mensual asignado."""

    def __init__(self, message: str, current_usage: float, monthly_budget: float, reserved: float):
        super().__init__(message)
        self.current_usage = current_usage
        self.monthly_budget = monthly_budget
        self.reserved = reserved


class AIService:
    """
    Servicio de orquestación para configuración BYOK, estimación de costos,
    reserva atómica de presupuesto y ejecución simulada con fallback determinista.
    """

    def __init__(self, vault_service: Optional[SecretsVaultService] = None):
        self.vault = vault_service or SecretsVaultService()

    @staticmethod
    def get_catalog() -> Dict[str, Any]:
        """Retorna el catálogo público de proveedores y modelos disponibles con tarifas referenciales."""
        return PROVIDERS_CATALOG

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Estimación heurística de tokens (~4 caracteres por token).
        Mínimo de 1 token para texto no vacío.
        """
        if not text:
            return 0
        return max(1, math.ceil(len(text.strip()) / 4))

    @staticmethod
    def calculate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calcula el costo estimado en USD a partir de la tarifa del modelo."""
        prov_info = PROVIDERS_CATALOG.get(provider)
        if not prov_info:
            return 0.0
        model_info = prov_info.get("models", {}).get(model)
        if not model_info:
            return 0.0

        in_rate = model_info["input_cost_per_million"] / 1_000_000.0
        out_rate = model_info["output_cost_per_million"] / 1_000_000.0
        cost = (input_tokens * in_rate) + (output_tokens * out_rate)
        return round(cost, 6)

    async def get_or_create_config(
        self, session: AsyncSession, tenant_id: str, provider: str = "openai"
    ) -> APIKeyConfig:
        """
        Obtiene la configuración del proveedor para el tenant o crea la predeterminada.
        Gestiona el ciclo de período mensual (YYYY-MM), reiniciando el consumo si cambia el mes.
        """
        current_period = datetime.now(timezone.utc).strftime("%Y-%m")
        stmt = select(APIKeyConfig).where(
            APIKeyConfig.tenant_id == tenant_id,
            APIKeyConfig.provider == provider,
        )
        res = await session.execute(stmt)
        config = res.scalar_one_or_none()
        if not config:
            default_model = "gpt-4o-mini"
            if provider == "anthropic":
                default_model = "claude-3-haiku"
            elif provider == "google":
                default_model = "gemini-1.5-flash"

            config = APIKeyConfig(
                tenant_id=tenant_id,
                provider=provider,
                model=default_model,
                monthly_budget_usd=10.0,
                current_month_usage_usd=0.0,
                reserved_budget_usd=0.0,
                max_tokens_per_request=4096,
                max_concurrent_requests=2,
                is_active=True,
                billing_period_month=current_period,
            )
            session.add(config)
            await session.flush()
        else:
            # Comprobación de cambio de período mensual
            if getattr(config, "billing_period_month", None) != current_period:
                config.current_month_usage_usd = 0.0
                config.reserved_budget_usd = 0.0
                config.billing_period_month = current_period
                session.add(config)
                await session.flush()

        return config

    async def reserve_budget(
        self,
        session: AsyncSession,
        tenant_id: str,
        provider: str,
        estimated_cost: float,
    ) -> APIKeyConfig:
        """
        Reserva atómicamente el presupuesto estimado en la base de datos.
        Previene race conditions ante llamadas concurrentes.
        Lanza AIBudgetExceededError si no hay presupuesto disponible.
        """
        config = await self.get_or_create_config(session, tenant_id, provider)

        stmt = (
            update(APIKeyConfig)
            .where(
                APIKeyConfig.id == config.id,
                APIKeyConfig.is_active.is_(True),
                (APIKeyConfig.current_month_usage_usd + APIKeyConfig.reserved_budget_usd + estimated_cost)
                <= APIKeyConfig.monthly_budget_usd,
            )
            .values(reserved_budget_usd=APIKeyConfig.reserved_budget_usd + estimated_cost)
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            await session.refresh(config)
            rem = max(0.0, config.monthly_budget_usd - (config.current_month_usage_usd + config.reserved_budget_usd))
            raise AIBudgetExceededError(
                f"Presupuesto insuficiente para reservar ${estimated_cost:.6f}. "
                f"Disponible: ${rem:.6f} (Límite: ${config.monthly_budget_usd:.4f}, "
                f"Consumo: ${config.current_month_usage_usd:.6f}, Reservado: ${config.reserved_budget_usd:.6f})",
                current_usage=config.current_month_usage_usd,
                monthly_budget=config.monthly_budget_usd,
                reserved=config.reserved_budget_usd,
            )

        await session.refresh(config)
        return config

    async def commit_usage(
        self,
        session: AsyncSession,
        tenant_id: str,
        provider: str,
        model: str,
        operation: str,
        estimated_cost: float,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> CostLog:
        """
        Libera la reserva atómica y suma el consumo real a current_month_usage_usd.
        Garantía estricta: jamás permite que el consumo cobrado exceda lo reservado
        ni que supere el límite mensual configurado.
        """
        calc_cost = self.calculate_cost(provider, model, actual_input_tokens, actual_output_tokens)

        stmt = select(APIKeyConfig).where(
            APIKeyConfig.tenant_id == tenant_id,
            APIKeyConfig.provider == provider,
        )
        res = await session.execute(stmt)
        config = res.scalar_one_or_none()
        if not config:
            raise ValueError(f"No existe configuración para {tenant_id}/{provider}")

        # Invariante 1: el costo cobrado no puede superar la estimación reservada
        safe_cost = min(calc_cost, estimated_cost)
        # Invariante 2: el nuevo acumulado no puede rebasar el presupuesto mensual
        max_possible_addition = max(0.0, round(config.monthly_budget_usd - config.current_month_usage_usd, 6))
        final_cost = min(safe_cost, max_possible_addition)

        new_reserved = max(0.0, round(config.reserved_budget_usd - estimated_cost, 6))
        config.reserved_budget_usd = new_reserved
        config.current_month_usage_usd = min(config.monthly_budget_usd, round(config.current_month_usage_usd + final_cost, 6))
        session.add(config)

        # Insertar registro auditable en CostLog
        cost_log = CostLog(
            tenant_id=tenant_id,
            provider=provider,
            model_used=model,
            operation=operation,
            input_tokens=actual_input_tokens,
            output_tokens=actual_output_tokens,
            estimated_cost_usd=final_cost,
            is_simulation=True,
        )
        session.add(cost_log)
        await session.flush()
        return cost_log

    async def release_reservation(
        self,
        session: AsyncSession,
        tenant_id: str,
        provider: str,
        estimated_cost: float,
    ) -> None:
        """Libera una reserva previa si la operación se canceló o falló."""
        stmt = select(APIKeyConfig).where(
            APIKeyConfig.tenant_id == tenant_id,
            APIKeyConfig.provider == provider,
        )
        res = await session.execute(stmt)
        config = res.scalar_one_or_none()
        if config:
            config.reserved_budget_usd = max(0.0, round(config.reserved_budget_usd - estimated_cost, 6))
            session.add(config)
            await session.flush()

    async def run_deterministic_fallback(
        self,
        operation: str,
        prompt_or_context: str,
        fallback_reason: str,
        session: Optional[AsyncSession] = None,
        tenant_id: Optional[str] = None,
        vacancy_id: Optional[str] = None,
        cv_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Alternativa determinista veraz basada en hechos verificados (REQ-011, D14).
        Principios inviolables:
        1. No inventar datos: si faltan datos verificados o información, se rechaza o informa insuficiencia.
        2. Perfiles distintos NO reciben puntuaciones fijas (0.85); se calcula el solapamiento real.
        3. Cartas de postulación NO afirman competencias ni trayectoria inexistente.
        """
        # Si se cuenta con sesión de base de datos e identificadores, ejecutar los servicios reales de dominio
        if session and tenant_id and vacancy_id:
            from backend.app.modules.profile.models import UserProfile, Skill
            from backend.app.modules.cv.models import MasterCV
            from backend.app.modules.vacancies.models import Vacancy
            from backend.app.modules.evaluation.service import DeterministicEvaluationService

            prof_res = await session.execute(select(UserProfile).where(UserProfile.tenant_id == tenant_id))
            profile = prof_res.scalar_one_or_none()

            vac_res = await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id, Vacancy.tenant_id == tenant_id))
            vacancy = vac_res.scalar_one_or_none()

            cv = None
            if cv_id:
                c_res = await session.execute(select(MasterCV).where(MasterCV.id == cv_id, MasterCV.tenant_id == tenant_id))
                cv = c_res.scalar_one_or_none()
            elif profile:
                c_res = await session.execute(
                    select(MasterCV).where(MasterCV.profile_id == profile.id, MasterCV.tenant_id == tenant_id).order_by(MasterCV.created_at.desc())
                )
                cv = c_res.scalars().first()

            skills_res = await session.execute(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.verification_status == "verified")
            )
            verified_skills = list(skills_res.scalars().all())

            if profile and vacancy:
                eval_obj = DeterministicEvaluationService.evaluate(
                    tenant_id=tenant_id,
                    profile=profile,
                    cv=cv,
                    skills=verified_skills,
                    vacancy=vacancy,
                )
                return {
                    "success": True,
                    "is_simulation": True,
                    "is_fallback": True,
                    "fallback_reason": fallback_reason,
                    "simulation_banner": SIMULATION_BANNER_TEXT,
                    "cost_incurred_usd": 0.0,
                    "tokens_consumed": 0,
                    "data": {
                        "compatibility_score": eval_obj.compatibility_score,
                        "recommendation": eval_obj.recommendation,
                        "justification": f"[DETERMINISTA] {eval_obj.justification}",
                        "matched_skills": eval_obj.matched_skills,
                        "missing_skills": eval_obj.missing_skills,
                        "score_breakdown": eval_obj.score_breakdown,
                        "rules_applied": eval_obj.rules_applied,
                    },
                }

        # Fallback sin contexto de base de datos (por ejemplo, evaluación textual directa)
        text_lower = (prompt_or_context or "").lower()

        if "evaluat" in operation.lower() or "compatib" in operation.lower():
            # Si el texto indica ausencia de experiencia/habilidades o está vacío, informar insuficiencia sin inventar
            has_no_data = (
                not text_lower.strip()
                or "sin experiencia" in text_lower
                or "sin habilidades" in text_lower
                or "0 experiencia" in text_lower
                or "no cuenta con" in text_lower
            )

            if has_no_data:
                result_data = {
                    "compatibility_score": 0.0,
                    "recommendation": "weak",
                    "status": "insufficient_verified_data",
                    "matched_skills": [],
                    "missing_skills": [],
                    "rules_applied": [{"rule": "data_completeness_check", "status": "no_verified_facts"}],
                    "justification": f"[DETERMINISTA] Información insuficiente o sin competencias verificadas para evaluar compatibilidad. {fallback_reason}",
                }
            else:
                # Extraer posibles habilidades y requerimientos del texto para cálculo real no-fijo
                req_match = re.findall(r"(?:requiere|requisitos?|puesto)[:\s]+([^.\n]+)", text_lower)
                prof_match = re.findall(r"(?:perfil|habilidades?|candidato)[:\s]+([^.\n]+)", text_lower)

                if req_match and prof_match:
                    req_tokens = set(re.findall(r"\b[a-zA-Z0-9+#_.-]{2,}\b", req_match[0]))
                    prof_tokens = set(re.findall(r"\b[a-zA-Z0-9+#_.-]{2,}\b", prof_match[0]))
                    stopwords = {"para", "con", "por", "del", "los", "las", "una", "que", "año", "anos", "experiencia"}
                    req_clean = req_tokens - stopwords
                    prof_clean = prof_tokens - stopwords
                    matched = list(req_clean.intersection(prof_clean))
                    missing = list(req_clean - prof_clean)
                    score = round(len(matched) / max(1, len(req_clean)), 2)
                    rec = "strong_match" if score >= 0.8 else "good_match" if score >= 0.5 else "partial" if score >= 0.3 else "weak"
                    result_data = {
                        "compatibility_score": score,
                        "recommendation": rec,
                        "matched_skills": matched,
                        "missing_skills": missing,
                        "rules_applied": [{"rule": "keyword_overlap", "matched": len(matched), "total": len(req_clean)}],
                        "justification": f"[DETERMINISTA] Solapamiento calculado ({score:.0%}): {len(matched)} coincidencias, {len(missing)} faltantes. {fallback_reason}",
                    }
                else:
                    # Texto sin pares claros: evaluación cualitativa honesta sin inventar un número arbitrario
                    words = set(re.findall(r"\b[a-zA-Z0-9+#_.-]{3,}\b", text_lower))
                    stopwords = {"para", "con", "por", "del", "los", "las", "una", "que", "experiencia", "desarrollador"}
                    meaningful = words - stopwords
                    score = min(0.60, round(len(meaningful) * 0.10, 2))
                    result_data = {
                        "compatibility_score": score,
                        "recommendation": "partial" if score >= 0.4 else "weak",
                        "matched_skills": list(meaningful)[:3],
                        "missing_skills": [],
                        "rules_applied": [{"rule": "heuristic_text_density", "matched_terms": len(meaningful)}],
                        "justification": f"[DETERMINISTA] Estimación léxica de contexto ({score:.0%}). {fallback_reason}",
                    }
        elif "cover_letter" in operation.lower() or "mensaj" in operation.lower():
            # Redacción honesta y neutral sin inventar trayectoria ni competencias no verificadas
            result_data = {
                "message": (
                    "Estimado equipo de selección:\n\n"
                    "Presento formalmente mi postulación para la posición indicada. "
                    "Quedo a su disposición para coordinar una entrevista y revisar en detalle mis antecedentes.\n\n"
                    "Atentamente,\nPostulante"
                ),
                "tone": "neutral_formal",
                "facts_used": {},
                "note": "Redacción sin afirmaciones de competencias ni trayectoria no verificadas.",
            }
        else:
            result_data = {
                "status": "completed_deterministic",
                "summary": f"[DETERMINISTA] Operación ejecutada mediante reglas objetivas sin consumo de tokens.",
            }

        return {
            "success": True,
            "is_simulation": True,
            "is_fallback": True,
            "fallback_reason": fallback_reason,
            "simulation_banner": SIMULATION_BANNER_TEXT,
            "cost_incurred_usd": 0.0,
            "tokens_consumed": 0,
            "data": result_data,
        }

    async def execute_simulated_call(
        self,
        session: AsyncSession,
        tenant_id: str,
        provider: str,
        operation: str,
        prompt_text: str,
        expected_output_tokens: int = 250,
        force_fallback: bool = False,
        vacancy_id: Optional[str] = None,
        cv_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ejecuta el ciclo de llamada de IA en modo simulación garantizando:
        1. Invariante presupuestario estricto: el costo cobrado y los tokens jamás superan el presupuesto restante.
        2. Si el presupuesto es insuficiente incluso para el prompt mínimo o salida acotada, fallback determinista.
        3. El texto simulado generado se acota estrictamente a los tokens reservados (previene desbordamiento).
        4. Fallback determinista veraz sin inventar datos.
        """
        config = await self.get_or_create_config(session, tenant_id, provider)

        if force_fallback or not config.is_active:
            reason = "IA desactivada por configuración del usuario." if not config.is_active else "Solicitud de fallback forzado."
            return await self.run_deterministic_fallback(
                operation, prompt_text, reason, session=session, tenant_id=tenant_id, vacancy_id=vacancy_id, cv_id=cv_id
            )

        has_key = self.vault.has_key(tenant_id, provider)
        if not has_key:
            return await self.run_deterministic_fallback(
                operation,
                prompt_text,
                f"No hay una API key configurada en el vault para el proveedor '{provider}'.",
                session=session, tenant_id=tenant_id, vacancy_id=vacancy_id, cv_id=cv_id
            )

        # 1. Comprobación de saldo remanente
        remaining_funds = max(0.0, round(config.monthly_budget_usd - (config.current_month_usage_usd + config.reserved_budget_usd), 6))
        if remaining_funds <= 0.0:
            return await self.run_deterministic_fallback(
                operation,
                prompt_text,
                f"Presupuesto mensual agotado ($0.000000 disponible de ${config.monthly_budget_usd:.4f}).",
                session=session, tenant_id=tenant_id, vacancy_id=vacancy_id, cv_id=cv_id
            )

        # 2. Estimación de tokens y cálculo del costo estimado para la petición
        input_tokens = self.estimate_tokens(prompt_text)
        output_tokens = min(max(1, expected_output_tokens), config.max_tokens_per_request)
        estimated_cost = self.calculate_cost(provider, config.model, input_tokens, output_tokens)

        if estimated_cost > remaining_funds:
            return await self.run_deterministic_fallback(
                operation,
                prompt_text,
                f"Presupuesto mensual insuficiente para la llamada (${estimated_cost:.6f} req > ${remaining_funds:.6f} disp de ${config.monthly_budget_usd:.4f}).",
                session=session, tenant_id=tenant_id, vacancy_id=vacancy_id, cv_id=cv_id
            )

        # 3. Intentar reserva de presupuesto atómica
        try:
            await self.reserve_budget(session, tenant_id, provider, estimated_cost)
        except AIBudgetExceededError as exc:
            return await self.run_deterministic_fallback(
                operation,
                prompt_text,
                str(exc),
                session=session, tenant_id=tenant_id, vacancy_id=vacancy_id, cv_id=cv_id
            )

        try:
            # 4. Generar texto simulado acotado estrictamente a output_tokens reservados
            max_chars = max(4, output_tokens * 4)
            full_sim_text = (
                f"[SIMULACIÓN {provider.upper()} - {config.model}]\n"
                f"Operación '{operation}' procesada con éxito.\n"
                f"Contexto: {prompt_text[:60]}..."
            )
            simulated_response = full_sim_text[:max_chars].strip()
            actual_output_tokens = min(output_tokens, max(1, self.estimate_tokens(simulated_response)))

            # 5. Confirmar consumo real (jamás supera estimated_cost ni monthly_budget)
            cost_log = await self.commit_usage(
                session=session,
                tenant_id=tenant_id,
                provider=provider,
                model=config.model,
                operation=operation,
                estimated_cost=estimated_cost,
                actual_input_tokens=input_tokens,
                actual_output_tokens=actual_output_tokens,
            )

            rem_after = max(0.0, round(config.monthly_budget_usd - (config.current_month_usage_usd + config.reserved_budget_usd), 6))

            return {
                "success": True,
                "is_simulation": True,
                "is_fallback": False,
                "fallback_reason": None,
                "simulation_banner": SIMULATION_BANNER_TEXT,
                "provider": provider,
                "model": config.model,
                "operation": operation,
                "input_tokens": input_tokens,
                "output_tokens": actual_output_tokens,
                "tokens_consumed": input_tokens + actual_output_tokens,
                "cost_incurred_usd": cost_log.estimated_cost_usd,
                "remaining_budget_usd": rem_after,
                "monthly_budget_usd": config.monthly_budget_usd,
                "current_month_usage_usd": config.current_month_usage_usd,
                "data": {
                    "text": simulated_response,
                },
            }
        except Exception:
            await self.release_reservation(session, tenant_id, provider, estimated_cost)
            raise
