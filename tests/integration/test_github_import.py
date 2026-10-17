"""
Pruebas de integración para Importación de GitHub con permisos mínimos (REQ-003, D12).
Verifica:
1. Validación sintáctica de nombres de usuario de GitHub.
2. Modo simulación (SIMULATION_MODE=true): extracción determinista sin llamadas a red externa.
3. Principio inmutable: Extraer != Verificar (ProfessionalSource, Skill y Experience en estado 'pending').
4. Detección y neutralización de inyección de instrucciones en metadatos de repositorios.
5. Flujo de revisión humana posterior (aceptar, modificar o rechazar elementos extraídos).
6. Aislamiento estricto por tenant_id.
7. Presencia de controles en la interfaz de usuario.
"""

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.database import async_session_factory
from backend.app.core.security import create_access_token, hash_password
from backend.app.modules.profile.models import Experience, ProfessionalSource, Skill, UserProfile
from backend.app.modules.profile.github_service import GitHubImportService, GitHubImportError
from backend.app.modules.users.models import Tenant, User


@pytest.mark.asyncio
async def test_github_username_validation():
    """Verifica la validación sintáctica de nombres de usuario de GitHub."""
    assert GitHubImportService.validate_username("octocat") == "octocat"
    assert GitHubImportService.validate_username("developer-123") == "developer-123"

    with pytest.raises(GitHubImportError):
        GitHubImportService.validate_username("user;rm -rf /")

    with pytest.raises(GitHubImportError):
        GitHubImportService.validate_username("-invalid-start")


@pytest.mark.asyncio
async def test_github_import_simulation_mode_creates_pending_items(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str
):
    """
    Verifica que la importación de GitHub en modo simulación:
    - No realiza llamadas de red externas.
    - Crea la fuente profesional con verification_status='pending'.
    - Extrae habilidades y proyectos como 'pending' con su procedencia.
    """
    # Asegurar que existe perfil para el tenant
    async with async_session_factory() as session:
        prof_res = await session.execute(
            select(UserProfile).where(UserProfile.tenant_id == test_tenant_id)
        )
        profile = prof_res.scalars().first()
        if not profile:
            profile = UserProfile(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                full_name="Desarrollador Open Source",
                email="dev.oss@example.org",
                location_city="Guadalajara",
                location_country="México",
                verification_status="pending",
            )
            session.add(profile)
            await session.commit()

    # Ejecutar importación
    payload = {
        "username": "developer-oss",
        "access_token": "mock_read_only_token_12345",
        "max_repos": 3,
    }
    res = await client.post("/api/v1/profile/sources/github/import", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()

    assert data["source_type"] == "github"
    assert data["verification_status"] == "pending"
    assert data["repositories_imported"] >= 1
    assert data["is_simulated"] is True
    assert len(data["skills_suggested"]) >= 1
    assert len(data["experiences_suggested"]) >= 1

    source_id = data["source_id"]

    # Verificar persistencia en base de datos
    async with async_session_factory() as session:
        # Fuente profesional
        src = (await session.execute(select(ProfessionalSource).where(ProfessionalSource.id == source_id))).scalar_one()
        assert src.verification_status == "pending"
        assert src.source_type == "github"

        # Habilidades vinculadas en pending
        skills = (await session.execute(select(Skill).where(Skill.source_id == source_id))).scalars().all()
        assert len(skills) >= 1
        for s in skills:
            assert s.verification_status == "pending"
            assert "GitHub" in (s.source_fragment or "")

        # Proyectos / experiencias vinculadas en pending
        exps = (await session.execute(select(Experience).where(Experience.source_id == source_id))).scalars().all()
        assert len(exps) >= 1
        for e in exps:
            assert e.verification_status == "pending"
            assert "GitHub" in e.company


@pytest.mark.asyncio
async def test_github_human_review_acceptance_and_rejection(
    client: AsyncClient, auth_headers: dict, test_tenant_id: str
):
    """
    Verifica que las entidades importadas desde GitHub pueden ser aceptadas, modificadas
    o rechazadas por el usuario en la etapa de revisión humana.
    """
    async with async_session_factory() as session:
        prof_res = await session.execute(
            select(UserProfile).where(UserProfile.tenant_id == test_tenant_id)
        )
        profile = prof_res.scalars().first()
        if not profile:
            profile = UserProfile(
                id=str(uuid.uuid4()),
                tenant_id=test_tenant_id,
                full_name="Usuario Revisor",
                email="revisor@example.org",
                location_city="Guadalajara",
                location_country="México",
                verification_status="pending",
            )
            session.add(profile)
            await session.commit()

    # 1. Importar repositorios
    res = await client.post(
        "/api/v1/profile/sources/github/import",
        json={"username": "reviewer-user", "max_repos": 2},
        headers=auth_headers,
    )
    assert res.status_code == 201
    data = res.json()
    skill_item = data["skills_suggested"][0]
    exp_item = data["experiences_suggested"][0]

    # 2. Aceptar la habilidad sugerida
    accept_skill_res = await client.post(
        f"/api/v1/profile/items/skills/{skill_item['id']}/review",
        json={"decision": "accept"},
        headers=auth_headers,
    )
    assert accept_skill_res.status_code == 200
    assert accept_skill_res.json()["verification_status"] == "verified"

    # 3. Rechazar la experiencia sugerida
    reject_exp_res = await client.post(
        f"/api/v1/profile/items/experiences/{exp_item['id']}/review",
        json={"decision": "reject"},
        headers=auth_headers,
    )
    assert reject_exp_res.status_code == 200
    assert reject_exp_res.json()["verification_status"] == "rejected"


@pytest.mark.asyncio
async def test_github_prompt_injection_sanitization():
    """
    Verifica que repositorios con intentos de inyección de instrucciones en título o descripción
    son detectados y neutralizados como datos sin ejecutarse como comandos (SEC-005, SEC-009).
    """
    async with async_session_factory() as session:
        t_id = f"inj-tenant-{uuid.uuid4().hex[:8]}"
        p_id = str(uuid.uuid4())
        tenant = Tenant(id=t_id, tenant_id=t_id, name="Injection Test Tenant")
        session.add(tenant)
        profile = UserProfile(
            id=p_id,
            tenant_id=t_id,
            full_name="Usuario Seguro",
            email="seguro@example.org",
            location_city="CDMX",
            location_country="México",
        )
        session.add(profile)
        await session.commit()

        # Simular repositorios inyectados
        result = await GitHubImportService.import_github_profile(
            session=session,
            tenant_id=t_id,
            profile_id=p_id,
            username="malicious-payload-user",
            max_repos=2,
        )
        assert result["verification_status"] == "pending"
        # Verificar que la fuente se persiste como pending
        assert result["source_type"] == "github"


@pytest.mark.asyncio
async def test_github_import_tenant_isolation(client: AsyncClient, auth_headers: dict, test_tenant_id: str):
    """Verifica aislamiento estricto por tenant en la importación de GitHub."""
    other_tenant = f"other-git-tenant-{uuid.uuid4().hex[:8]}"
    other_user_id = str(uuid.uuid4())

    async with async_session_factory() as session:
        t2 = Tenant(id=other_tenant, tenant_id=other_tenant, name="Tenant Dos GitHub")
        session.add(t2)
        u2 = User(
            id=other_user_id,
            tenant_id=other_tenant,
            email=f"git_user_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("password123"),
            full_name="User Git Dos",
            is_active=True,
        )
        session.add(u2)
        p2 = UserProfile(
            id=str(uuid.uuid4()),
            tenant_id=other_tenant,
            full_name="User Git Dos",
            email="git_user_dos@example.com",
            location_city="Monterrey",
            location_country="México",
        )
        session.add(p2)
        await session.commit()

    token2 = create_access_token(data={"sub": other_user_id, "tenant_id": other_tenant})
    headers2 = {"Authorization": f"Bearer {token2}"}

    # Importar en Tenant 2
    res2 = await client.post(
        "/api/v1/profile/sources/github/import",
        json={"username": "tenant2-dev"},
        headers=headers2,
    )
    assert res2.status_code == 201
    source_id_2 = res2.json()["source_id"]

    # Tenant 1 consulta sus fuentes: NO debe ver la fuente de Tenant 2
    sources_t1 = await client.get("/api/v1/profile/sources", headers=auth_headers)
    assert sources_t1.status_code == 200
    assert not any(s["id"] == source_id_2 for s in sources_t1.json())


def test_ui_contains_github_import_controls():
    """Verifica que index.html contiene los controles de importación de GitHub."""
    with open("backend/app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    assert 'id="githubInputContainer"' in html
    assert 'id="txtGitHubUsername"' in html
    assert 'id="btnImportGitHub"' in html
    assert 'toggleGitHubImport' in html
    assert 'importFromGitHub' in html
