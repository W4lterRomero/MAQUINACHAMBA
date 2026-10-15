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
# Importar todos los modelos para registro en Base.metadata
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.profile.models import UserProfile, Skill, Experience
from backend.app.modules.cv.models import MasterCV
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.evaluation.models import Evaluation
from backend.app.modules.approvals.models import Candidacy, Approval, ScheduledTask, ExecutionLog
from backend.app.modules.crm.models import CRMEntry, CRMEvent
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


@pytest_asyncio.fixture
async def auth_user_and_headers(test_tenant_id: str):
    """Crea un usuario autenticado para pruebas y retorna su modelo, headers y token."""
    from backend.app.core.database import async_session_factory
    from backend.app.core.security import create_access_token, hash_password
    from sqlalchemy import select

    async with async_session_factory() as session:
        t_res = await session.execute(select(Tenant).where(Tenant.id == test_tenant_id))
        if not t_res.scalar_one_or_none():
            tenant = Tenant(id=test_tenant_id, tenant_id=test_tenant_id, name="Test Tenant")
            session.add(tenant)
            await session.flush()

        user_id = "test-user-00000000-0000-0000-0000-000000000001"
        u_res = await session.execute(select(User).where(User.id == user_id))
        user = u_res.scalar_one_or_none()
        if not user:
            user = User(
                id=user_id,
                tenant_id=test_tenant_id,
                email="testuser@example.com",
                hashed_password=hash_password("securepassword123"),
                full_name="Usuario de Prueba",
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

    token = create_access_token(
        data={"sub": user.id, "email": user.email, "tenant_id": user.tenant_id}
    )
    headers = {"Authorization": f"Bearer {token}"}
    return user, headers, token


@pytest_asyncio.fixture
async def auth_headers(auth_user_and_headers):
    """Headers con Bearer token autenticado para el tenant de test."""
    _, headers, _ = auth_user_and_headers
    return headers

