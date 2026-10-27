"""
Configuración de base de datos con SQLAlchemy 2.0 async.
Soporta SQLite (MVP) y PostgreSQL (futuro).
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import MetaData, String, DateTime, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)

from backend.app.core.config import get_settings

# Naming convention for constraints (helps with migrations)
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=convention)

    # Columnas comunes en todas las tablas
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


def _create_engine():
    """Crea el engine async según la configuración."""
    import os
    settings = get_settings()
    url = settings.database_url

    # Normalizar url de SQLite para async si viene sin driver explícito
    if url.startswith("sqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

    # Asegurar que el directorio de datos existe si es una ruta local SQLite
    if "sqlite" in url:
        db_path = url.split(":///")[-1] if ":///" in url else None
        if db_path and not db_path.startswith(":memory:"):
            dir_name = os.path.dirname(os.path.abspath(db_path))
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

    # SQLite needs special handling for async
    connect_args = {}
    if "sqlite" in url:
        connect_args["check_same_thread"] = False

    engine = create_async_engine(
        url,
        echo=(settings.app_env == "development"),
        connect_args=connect_args,
    )

    # Enable WAL mode for SQLite (better concurrent read performance)
    if "sqlite" in url:
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


engine = _create_engine()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def import_all_models():
    """Importa todos los modelos para registrarlos en Base.metadata."""
    import backend.app.modules.users.models  # noqa: F401
    import backend.app.modules.profile.models  # noqa: F401
    import backend.app.modules.cv.models  # noqa: F401
    import backend.app.modules.communication.models  # noqa: F401
    import backend.app.modules.vacancies.models  # noqa: F401
    import backend.app.modules.evaluation.models  # noqa: F401
    import backend.app.modules.approvals.models  # noqa: F401
    import backend.app.modules.crm.models  # noqa: F401
    import backend.app.modules.secrets.models  # noqa: F401
    import backend.app.modules.audit.models  # noqa: F401
    import backend.app.modules.portals.models  # noqa: F401


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency para obtener una sesión de base de datos."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Inicializa la base de datos creando todas las tablas registradas."""
    import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Cierra la conexión a la base de datos."""
    await engine.dispose()
