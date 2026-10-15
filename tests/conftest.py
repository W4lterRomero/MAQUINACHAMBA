"""
Configuración compartida de pruebas para Máquina de Chamba.
"""

import asyncio
import os
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Forzar modo simulación y base de datos de test
os.environ["SIMULATION_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_maquinachamba.db"
os.environ["APP_ENV"] = "development"
os.environ["APP_SECRET_KEY"] = "test-secret-key-not-for-production"

from backend.app.core.database import Base, engine, init_db, close_db
from backend.app.main import app


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def setup_db():
    """Initialize test database."""
    await init_db()
    yield
    await close_db()
    # Clean up test database
    import os
    try:
        os.remove("./test_maquinachamba.db")
    except FileNotFoundError:
        pass


@pytest_asyncio.fixture
async def client(setup_db) -> AsyncClient:
    """HTTP client for API testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def test_tenant_id() -> str:
    """ID de tenant para pruebas."""
    return "test-tenant-00000000-0000-0000-0000-000000000001"
