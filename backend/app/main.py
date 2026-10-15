"""
Máquina de Chamba — Punto de entrada principal de la aplicación.
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path


import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import get_settings
from backend.app.core.database import init_db, close_db
from backend.app.core.security import redact_secrets

# Configurar logging con redacción de secretos
class SecureFormatter(logging.Formatter):
    """Formatter que redacta secretos automáticamente."""
    def format(self, record):
        message = super().format(record)
        return redact_secrets(message)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(SecureFormatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("maquinachamba")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: inicializar y cerrar recursos."""
    settings = get_settings()
    logger.info("Iniciando Máquina de Chamba...")
    logger.info(f"  Entorno: {settings.app_env}")
    logger.info(f"  Simulación: {'ACTIVADA' if settings.simulation_mode else 'DESACTIVADA'}")
    logger.info(f"  Zona horaria: {settings.app_timezone}")

    # Inicializar base de datos
    await init_db()
    logger.info("  Base de datos inicializada.")

    yield

    # Cleanup
    await close_db()
    logger.info("Máquina de Chamba detenida.")


def create_app() -> FastAPI:
    """Factory de la aplicación FastAPI."""
    settings = get_settings()

    app = FastAPI(
        title="Máquina de Chamba",
        description="Gestión de búsqueda laboral con aprobación obligatoria",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS - solo localhost en desarrollo
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "simulation_mode": settings.simulation_mode,
        }

    # Registrar routers
    from backend.app.api import api_router
    app.include_router(api_router, prefix="/api/v1")

    # Servir interfaz web mínima local (REQ-001, REQ-014, REQ-026, REQ-031)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", response_class=FileResponse)
        @app.get("/app", response_class=FileResponse)
        async def serve_ui():
            return FileResponse(str(static_dir / "index.html"))

    return app



app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "backend.app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=(settings.app_env == "development"),
    )
