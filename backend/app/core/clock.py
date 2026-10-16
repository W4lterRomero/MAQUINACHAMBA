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
import time as time_module


class FileBackedClock(Clock):
    """
    Reloj de pruebas controlable y sincronizado entre múltiples procesos mediante archivo.
    Permite avanzar o fijar el instante UTC sin tocar bases de datos ni fixtures SQL.
    Falla de manera explícita si el archivo no existe, está vacío o es inválido (fail-closed).
    """

    def __init__(self, clock_file: Path, fallback_initial: Optional[datetime] = None):
        self.clock_file = Path(clock_file)
        if fallback_initial is not None and not self.clock_file.exists():
            self.set_time(fallback_initial)

    def now_utc(self) -> datetime:
        if not self.clock_file.exists():
            raise FileNotFoundError(f"Clock file does not exist: {self.clock_file}")
        try:
            text = self.clock_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValueError(f"Failed to read clock file {self.clock_file}: {e}") from e

        if not text:
            raise ValueError(f"Clock file is empty: {self.clock_file}")

        try:
            dt = datetime.fromisoformat(text)
        except Exception as e:
            raise ValueError(f"Invalid timestamp format in clock file {self.clock_file}: '{text}'") from e

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            dt = new_time.replace(tzinfo=timezone.utc)
        else:
            dt = new_time.astimezone(timezone.utc)
        self.clock_file.parent.mkdir(parents=True, exist_ok=True)
        # Escritura atómica con archivo temporal único, flush, fsync y reemplazo atómico
        tmp = self.clock_file.with_name(f".{self.clock_file.name}.{os.getpid()}.{time_module.time_ns()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(dt.isoformat())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.clock_file)

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
