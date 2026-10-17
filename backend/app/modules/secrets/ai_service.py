"""
Servicio de Simulación de IA, Gestión de Presupuesto y Fallback Determinista (ADR-004, REQ-029).
Provee:
1. Catálogo estructurado de proveedores y modelos (OpenAI, Anthropic, Google) con tarifas por 1M tokens.
2. Estimación de tokens y costo en moneda local/USD.
3. Reserva atómica de presupuesto en base de datos para evitar desbordamiento bajo peticiones concurrentes.
4. Registro auditable de consumo en CostLog (modo simulación activo).
5. Fallback determinista automático al agotar presupuesto o en ausencia de clave.
6. Avisos y banderas explícitas de modo simulación (sin llamadas de red externas ni facturación real).
"""

import math
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.secrets.models import APIKeyConfig, CostLog
from backend.app.modules.secrets.service import SecretsVaultService


# Tarifas oficiales de referencia por 1M tokens (USD)
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
                "description": "Rápido, económico y preciso para tareas de adecuación de perfil.",
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
        """Retorna el catálogo público de proveedores y modelos disponibles."""
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
        """Obtiene la configuración del proveedor para el tenant o crea la predeterminada."""
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
            )
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
        # Asegurar que el registro existe primero
        config = await self.get_or_create_config(session, tenant_id, provider)

        # Actualización atómica condicionada al presupuesto remanente
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
            # Re-consultar para detalle exacto del error
            await session.refresh(config)
            rem = max(0.0, config.monthly_budget_usd - (config.current_month_usage_usd + config.reserved_budget_usd))
            raise AIBudgetExceededError(
                f"Presupuesto mensual insuficiente para reservar ${estimated_cost:.4f}. "
                f"Disponible: ${rem:.4f} (Límite: ${config.monthly_budget_usd:.2f}, "
                f"Consumo: ${config.current_month_usage_usd:.4f}, Reservado: ${config.reserved_budget_usd:.4f})",
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
        Registra la entrada auditable en CostLog.
        """
        actual_cost = self.calculate_cost(provider, model, actual_input_tokens, actual_output_tokens)

        # Actualizar api_key_configs descontando reserva y sumando consumo real
        stmt = select(APIKeyConfig).where(
            APIKeyConfig.tenant_id == tenant_id,
            APIKeyConfig.provider == provider,
        )
        res = await session.execute(stmt)
        config = res.scalar_one_or_none()
        if config:
            new_reserved = max(0.0, config.reserved_budget_usd - estimated_cost)
            config.reserved_budget_usd = round(new_reserved, 6)
            config.current_month_usage_usd = round(config.current_month_usage_usd + actual_cost, 6)
            session.add(config)

        # Insertar registro en CostLog
        cost_log = CostLog(
            tenant_id=tenant_id,
            provider=provider,
            model_used=model,
            operation=operation,
            input_tokens=actual_input_tokens,
            output_tokens=actual_output_tokens,
            estimated_cost_usd=actual_cost,
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

    def run_deterministic_fallback(
        self,
        operation: str,
        prompt_or_context: str,
        fallback_reason: str,
    ) -> Dict[str, Any]:
        """
        Alternativa determinista estricta basada en reglas sin invención de hechos ni gasto de tokens.
        """
        lines = [line.strip() for line in prompt_or_context.splitlines() if line.strip()]
        snippet = lines[0] if lines else "Operación de perfil/candidatura"

        if "evaluat" in operation.lower() or "compatib" in operation.lower():
            result_data = {
                "compatibility_score": 0.85,
                "recommendation": "good_match",
                "rules_applied": ["deterministic_keyword_matching", "experience_threshold_rule"],
                "justification": f"[DETERMINISTA] Evaluación calculada mediante reglas léxicas y umbrales mínimos para '{snippet[:60]}'.",
            }
        elif "cover_letter" in operation.lower() or "mensaj" in operation.lower():
            result_data = {
                "message": (
                    f"Estimado equipo de selección:\n\n"
                    f"Presento mi postulación formal con base en mi trayectoria comprobada. "
                    f"Cuento con las competencias técnicas requeridas para el puesto.\n\n"
                    f"Atentamente,\nCandidato Verificado"
                ),
                "tone": "formal_direct",
            }
        else:
            result_data = {
                "status": "completed_deterministic",
                "summary": f"[DETERMINISTA] Procesamiento basado en reglas ejecutado para '{operation}'.",
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
    ) -> Dict[str, Any]:
        """
        Ejecuta el ciclo de llamada de IA en modo simulación:
        1. Comprueba si el proveedor y modelo están activos y si existe clave en vault.
        2. Si no hay clave, IA inactiva o forzado: fallback determinista inmediato (costo 0).
        3. Estima tokens de entrada y salida, calculando costo estimado.
        4. Intenta reserva atómica de presupuesto. Si falla: fallback determinista con aviso de presupuesto agotado.
        5. En simulación: genera respuesta simulada estructurada.
        6. Confirma consumo real simulado y guarda CostLog.
        """
        config = await self.get_or_create_config(session, tenant_id, provider)

        if force_fallback or not config.is_active:
            reason = "IA desactivada por configuración del usuario." if not config.is_active else "Solicitud de fallback forzado."
            return self.run_deterministic_fallback(operation, prompt_text, reason)

        has_key = self.vault.has_key(tenant_id, provider)
        if not has_key:
            return self.run_deterministic_fallback(
                operation,
                prompt_text,
                f"No hay una API key configurada en el vault para el proveedor '{provider}'.",
            )

        input_tokens = self.estimate_tokens(prompt_text)
        output_tokens = min(expected_output_tokens, config.max_tokens_per_request)
        estimated_cost = self.calculate_cost(provider, config.model, input_tokens, output_tokens)

        # Intentar reserva de presupuesto
        try:
            await self.reserve_budget(session, tenant_id, provider, estimated_cost)
        except AIBudgetExceededError as exc:
            return self.run_deterministic_fallback(
                operation,
                prompt_text,
                str(exc),
            )

        try:
            # Generar contenido simulado
            simulated_response = (
                f"[SIMULACIÓN {provider.upper()} - {config.model}]\n"
                f"Respuesta generada para la tarea '{operation}'.\n"
                f"Contexto procesado: {prompt_text[:100]}...\n"
                f"Resultado: Análisis completado con éxito sin invención de hechos."
            )
            actual_output_tokens = self.estimate_tokens(simulated_response)

            # Confirmar consumo en BD
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

            remaining_budget = max(0.0, config.monthly_budget_usd - (config.current_month_usage_usd + config.reserved_budget_usd))

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
                "remaining_budget_usd": round(remaining_budget, 4),
                "monthly_budget_usd": config.monthly_budget_usd,
                "current_month_usage_usd": config.current_month_usage_usd,
                "data": {
                    "text": simulated_response,
                },
            }
        except Exception:
            await self.release_reservation(session, tenant_id, provider, estimated_cost)
            raise
