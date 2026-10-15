"""
Servicio de programación de tareas en cola durable.
Aprobar hoy programa para el ciclo del día siguiente en la zona horaria del usuario.
Gestiona el cálculo de instantes UTC, ventanas de ejecución y ventanas perdidas.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

from backend.app.core.clock import Clock, get_clock
from backend.app.core.security import hash_content
from backend.app.modules.approvals.models import ScheduledTask


class SchedulingService:
    """Calcula ventanas de ejecución, instantes UTC y gestiona estados de programación."""

    @classmethod
    def calculate_next_day_execution(
        cls,
        user_timezone: str,
        execution_window_start: time = time(9, 0),
        clock: Optional[Clock] = None,
    ) -> Tuple[date, time, datetime]:
        """
        Calcula la fecha de ejecución (día siguiente calendario en la zona del usuario)
        y el instante exacto en UTC.
        Returns: (scheduled_date, scheduled_time, execution_instant_utc)
        """
        c = clock or get_clock()
        tz = ZoneInfo(user_timezone)

        # Fecha local de hoy en la zona horaria del usuario
        local_today = c.today(user_timezone)
        # Siguiente día calendario exacto
        scheduled_date = local_today + timedelta(days=1)
        scheduled_time = execution_window_start

        # Combinar fecha y hora en la zona del usuario
        local_target_dt = datetime.combine(scheduled_date, scheduled_time, tzinfo=tz)

        # Convertir a UTC
        execution_instant_utc = local_target_dt.astimezone(timezone.utc)

        return scheduled_date, scheduled_time, execution_instant_utc

    @classmethod
    def generate_idempotency_key(
        cls,
        tenant_id: str,
        candidacy_id: str,
        scheduled_date: date,
        version: int = 1,
    ) -> str:
        """
        Genera una clave de idempotencia determinista para evitar duplicar
        la misma postulación en una misma fecha y versión.
        """
        return hash_content(
            "idempotency-v1",
            tenant_id,
            candidacy_id,
            scheduled_date.isoformat(),
            str(version),
        )

    @classmethod
    def is_premature(
        cls,
        task: ScheduledTask,
        execution_window_start: Optional[time] = None,
        clock: Optional[Clock] = None,
    ) -> bool:
        """
        Determina si el instante actual es previo al inicio programado de la tarea.
        Comprueba tanto el instante UTC como la hora de inicio de la ventana local.
        """
        c = clock or get_clock()
        now_utc = c.now_utc()

        # 1. Comprobación estricta por instante UTC
        if task.execution_instant_utc is not None:
            inst_utc = task.execution_instant_utc
            if inst_utc.tzinfo is None:
                inst_utc = inst_utc.replace(tzinfo=timezone.utc)
            if now_utc < inst_utc:
                return True

        # 2. Comprobación local respecto a fecha y hora programada
        if task.execution_timezone and task.scheduled_date:
            tz = ZoneInfo(task.execution_timezone)
            current_local = c.now(task.execution_timezone)
            win_start = execution_window_start or task.scheduled_time or time(9, 0)
            task_date = task.scheduled_date
            if isinstance(task_date, datetime):
                task_date = task_date.date()
            window_start_dt = datetime.combine(task_date, win_start, tzinfo=tz)
            if current_local < window_start_dt:
                return True

        return False

    @classmethod
    def is_missed_window(
        cls,
        task: ScheduledTask,
        execution_window_end: time = time(18, 0),
        clock: Optional[Clock] = None,
    ) -> bool:
        """
        Determina si una tarea pendiente perdió su ventana de ejecución
        (por ejemplo, si el equipo estuvo apagado y la hora local ya superó la ventana).
        """
        if task.status not in ("pending", "executing"):
            return False

        c = clock or get_clock()
        tz = ZoneInfo(task.execution_timezone)
        current_local = c.now(task.execution_timezone)

        task_date = task.scheduled_date
        if isinstance(task_date, datetime):
            task_date = task_date.date()
        window_end_dt = datetime.combine(task_date, execution_window_end, tzinfo=tz)

        # Si el momento actual ya rebasó la hora límite del día programado
        return current_local > window_end_dt
