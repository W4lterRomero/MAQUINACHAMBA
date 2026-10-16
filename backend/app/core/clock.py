"""
Abstracción de reloj para control de tiempo y programación durable.
Permite probar cambios de día calendario en pruebas automatizadas
sin esperar una noche real.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional


class Clock(ABC):
    """Interfaz abstracta de reloj."""

    @abstractmethod
    def now_utc(self) -> datetime:
        """Retorna el instante actual en UTC con timezone aware."""
        pass

    def now(self, tz_name: str) -> datetime:
        """Retorna el instante actual convertido a la zona horaria indicada."""
        tz = ZoneInfo(tz_name)
        return self.now_utc().astimezone(tz)

    def today(self, tz_name: str) -> date:
        """Retorna la fecha local actual en la zona horaria indicada."""
        return self.now(tz_name).date()

    def tomorrow(self, tz_name: str) -> date:
        """Retorna la fecha del día siguiente en la zona horaria indicada."""
        return self.today(tz_name) + timedelta(days=1)


class SystemClock(Clock):
    """Reloj de sistema que utiliza la hora real del equipo."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class MockClock(Clock):
    """
    Reloj simulado controlable para pruebas de ciclo diario,
    ventanas perdidas y cambios de horario.
    """

    def __init__(self, initial_time: Optional[datetime] = None):
        if initial_time is None:
            # Miércoles 15 de Octubre de 2026, 10:00 UTC por defecto
            self._current_utc = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)
        else:
            if initial_time.tzinfo is None:
                self._current_utc = initial_time.replace(tzinfo=timezone.utc)
            else:
                self._current_utc = initial_time.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._current_utc

    def set_time(self, new_time: datetime) -> None:
        """Fija el reloj en un instante específico."""
        if new_time.tzinfo is None:
            self._current_utc = new_time.replace(tzinfo=timezone.utc)
        else:
            self._current_utc = new_time.astimezone(timezone.utc)

    def advance(self, delta: timedelta) -> None:
        """Avanza el reloj según el timedelta indicado."""
        self._current_utc += delta

    def advance_seconds(self, seconds: float) -> None:
        """Avanza el reloj N segundos."""
        self._current_utc += timedelta(seconds=seconds)

    def advance_hours(self, hours: float) -> None:
        """Avanza el reloj N horas."""
        self._current_utc += timedelta(hours=hours)

    def advance_days(self, days: int) -> None:
        """Avanza el reloj N días calendario."""
        self._current_utc += timedelta(days=days)


import os
from pathlib import Path


class FileBackedClock(Clock):
    """
    Reloj de pruebas controlable y sincronizado entre múltiples procesos mediante archivo.
    Permite avanzar o fijar el instante UTC sin tocar bases de datos ni fixtures SQL.
    """

    def __init__(self, clock_file: Path, fallback_initial: Optional[datetime] = None):
        self.clock_file = Path(clock_file)
        if not self.clock_file.exists():
            init_dt = fallback_initial or datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)
            self.set_time(init_dt)

    def now_utc(self) -> datetime:
        try:
            if self.clock_file.exists():
                text = self.clock_file.read_text(encoding="utf-8").strip()
                if text:
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc)
        except Exception:
            pass
        return datetime.now(timezone.utc)

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            dt = new_time.replace(tzinfo=timezone.utc)
        else:
            dt = new_time.astimezone(timezone.utc)
        self.clock_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.clock_file.with_suffix(".tmp")
        tmp.write_text(dt.isoformat(), encoding="utf-8")
        tmp.replace(self.clock_file)

    def advance(self, delta: timedelta) -> None:
        curr = self.now_utc()
        self.set_time(curr + delta)

    def advance_seconds(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))

    def advance_hours(self, hours: float) -> None:
        self.advance(timedelta(hours=hours))

    def advance_days(self, days: int) -> None:
        self.advance(timedelta(days=days))


# Instancia singleton predeterminada
_default_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """Retorna el reloj activo. Si MAQUINACHAMBA_TEST_CLOCK_FILE está configurado, usa FileBackedClock."""
    env_file = os.environ.get("MAQUINACHAMBA_TEST_CLOCK_FILE")
    if env_file:
        return FileBackedClock(Path(env_file))
    return _default_clock


def set_clock(clock: Clock) -> None:
    """Configura el reloj activo (utilizado en tests)."""
    global _default_clock
    _default_clock = clock
