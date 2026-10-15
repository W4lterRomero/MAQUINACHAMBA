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

from backend.app.modules.execution.daemon import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launcher del Demonio de Ejecución Máquina de Chamba")
    parser.add_argument("--interval", type=float, default=3.0, help="Intervalo de sondeo en segundos (def: 3.0)")
    args = parser.parse_args()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDemonio detenido por el usuario.")
        sys.exit(0)
