#!/usr/bin/env python3
"""
Launcher de ejecución programada para Máquina de Chamba (REQ-031).
Inicia el servicio en segundo plano de manera autónoma sin requerir el IDE.

Uso:
    python scripts/run_execution_daemon.py [--interval SECONDS]
"""

import argparse
import asyncio
import os
import sys

# Asegurar que el directorio raíz del proyecto esté en el PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import time
from backend.app.modules.execution.daemon import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launcher del Demonio de Ejecución Máquina de Chamba")
    parser.add_argument("--interval", type=float, default=3.0, help="Intervalo de sondeo en segundos (def: 3.0)")
    parser.add_argument("--once", action="store_true", help="Ejecutar una sola iteración y salir")
    parser.add_argument("--max-iterations", type=int, default=None, help="Número máximo de iteraciones antes de salir")
    parser.add_argument("--window-start", type=str, default=None, help="Hora inicio de ventana permitida (HH:MM)")
    parser.add_argument("--window-end", type=str, default=None, help="Hora fin de ventana permitida (HH:MM)")
    args = parser.parse_args()

    win_start = time.fromisoformat(args.window_start) if args.window_start else None
    win_end = time.fromisoformat(args.window_end) if args.window_end else None

    try:
        asyncio.run(
            main(
                interval=args.interval,
                once=args.once,
                max_iterations=args.max_iterations,
                window_start=win_start,
                window_end=win_end,
            )
        )
    except KeyboardInterrupt:
        print("\nDemonio detenido por el usuario.")
        sys.exit(0)
