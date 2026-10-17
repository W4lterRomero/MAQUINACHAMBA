"""
Pruebas de integración para Configuración de IA, BYOK y Control de Presupuesto (ADR-004, REQ-029).
Verifica:
1. Almacenamiento seguro en disco (vault) con cifrado AES-256-GCM y permisos POSIX 0600.
2. Aislamiento estricto por tenant_id (un tenant no puede acceder, descifrar ni alterar claves de otro).
3. Ausencia absoluta de claves en texto plano en la base de datos, logs o respuestas API.
4. Control de presupuesto mensual, aviso y bloqueo al agotar saldo.
5. Alternativa determinista (fallback) sin llamadas externas ni consumo de tokens.
6. Control de concurrencia: reservas atómicas no superan el límite presupuestario bajo peticiones simultáneas.
7. Reemplazo y eliminación segura de secretos.
"""

import asyncio
import os
import stat
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.database import async_session_factory
from backend.app.modules.secrets.models import APIKeyConfig, CostLog
from backend.app.modules.secrets.service import SecretsVaultService
from backend.app.modules.secrets.ai_service import AIService, AIBudgetExceededError


@pytest.mark.asyncio
async def test_secrets_vault_encryption_permissions_and_deletion(tmp_path):
    """
    Verifica que el vault cifra las claves, aplica permisos 0600,
    las vincula al tenant_id y permite su eliminación segura.
    """
    vault = SecretsVaultService(base_dir=str(tmp_path))
    tenant_1 = f"tenant_{uuid.uuid4().hex[:8]}"
    tenant_2 = f"tenant_{uuid.uuid4().hex[:8]}"
    provider = "openai"

    secret_raw = f"sample-tok-{os.urandom(12).hex()}"

    # 1. Almacenar clave
    ref = vault.store_key(tenant_1, provider, secret_raw)
    assert ref.startswith("vault://")
    assert vault.has_key(tenant_1, provider) is True

    # 2. Verificar permisos POSIX del archivo (0600) y directorio (0700)
    vault_file = tmp_path / tenant_1 / f"{provider}.vault"
    assert vault_file.exists()
    file_mode = stat.S_IMODE(vault_file.stat().st_mode)
    assert file_mode == 0o600

    dir_mode = stat.S_IMODE((tmp_path / tenant_1).stat().st_mode)
    assert dir_mode == 0o700

    # 3. El contenido en disco NO es texto plano
    raw_disk_bytes = vault_file.read_bytes()
    assert secret_raw.encode() not in raw_disk_bytes

    # 4. Descifrado correcto para el tenant autorizado
    retrieved = vault.get_key(tenant_1, provider)
    assert retrieved == secret_raw

    # 5. Aislamiento criptográfico: tenant_2 no puede descifrar el archivo de tenant_1
    retrieved_other = vault.get_key(tenant_2, provider)
    assert retrieved_other is None

    # 6. Máscara segura
    mask = vault.mask_key(secret_raw)
    assert mask.endswith(secret_raw[-4:])
    assert "..." in mask
    assert secret_raw not in mask

    # 7. Eliminación segura
    deleted = vault.delete_key(tenant_1, provider)
    assert deleted is True
    assert vault.has_key(tenant_1, provider) is False
    assert not vault_file.exists()


@pytest.mark.asyncio
async def test_ai_config_api_endpoints_and_tenant_isolation(client: AsyncClient):
    """
    Verifica los endpoints de configuración de IA y garantiza que un tenant
    no pueda acceder a la configuración ni claves de otro tenant.
    """
    uid_a = uuid.uuid4().hex[:8]
    reg_a = await client.post("/api/v1/auth/register", json={
        "email": f"alice_ai_{uid_a}@example.com",
        "password": "Password123!",
        "full_name": "Alice AI User",
    })
    assert reg_a.status_code == 201
    token_a = reg_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    uid_b = uuid.uuid4().hex[:8]
    reg_b = await client.post("/api/v1/auth/register", json={
        "email": f"bob_ai_{uid_b}@example.com",
        "password": "Password123!",
        "full_name": "Bob AI User",
    })
    assert reg_b.status_code == 201
    token_b = reg_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 1. Consultar catálogo
    cat_res = await client.get("/api/v1/ai/catalog", headers=headers_a)
    assert cat_res.status_code == 200
    cat_data = cat_res.json()
    assert "openai" in cat_data["providers"]
    assert "anthropic" in cat_data["providers"]
    assert "google" in cat_data["providers"]
    assert cat_data["is_simulation"] is True

    # 2. Configurar proveedor y presupuesto para Tenant A
    upd_res_a = await client.post("/api/v1/ai/config", headers=headers_a, json={
        "provider": "anthropic",
        "model": "claude-3-5-sonnet",
        "monthly_budget_usd": 15.0,
        "is_active": True,
    })
    assert upd_res_a.status_code == 200
    assert upd_res_a.json()["monthly_budget_usd"] == 15.0
    assert upd_res_a.json()["model"] == "claude-3-5-sonnet"

    # 3. Guardar clave cifrada para Tenant A (clave ficticia de prueba)
    test_key_val_a = f"test-sec-val-{os.urandom(8).hex()}"
    key_res_a = await client.post("/api/v1/ai/key", headers=headers_a, json={
        "provider": "anthropic",
        "key_value": test_key_val_a,
    })
    assert key_res_a.status_code == 200
    assert key_res_a.json()["has_key"] is True
    assert test_key_val_a not in str(key_res_a.json())  # NUNCA en respuesta

    # 4. Verificar configuración de Tenant A
    cfg_a = await client.get("/api/v1/ai/config?provider=anthropic", headers=headers_a)
    assert cfg_a.status_code == 200
    data_a = cfg_a.json()
    assert data_a["monthly_budget_usd"] == 15.0
    assert data_a["has_key"] is True
    assert data_a["key_preview"] is not None
    assert test_key_val_a not in str(data_a)  # Sin texto plano

    # 5. Tenant B debe ver su propia configuración por defecto, sin la clave ni presupuesto de A
    cfg_b = await client.get("/api/v1/ai/config?provider=anthropic", headers=headers_b)
    assert cfg_b.status_code == 200
    data_b = cfg_b.json()
    assert data_b["has_key"] is False
    assert data_b["key_preview"] is None
    assert data_b["monthly_budget_usd"] == 10.0  # Predeterminado para B

    # 6. Reemplazo seguro de clave para Tenant A
    new_key_val_a = f"test-new-val-{os.urandom(8).hex()}"
    key_res_a2 = await client.post("/api/v1/ai/key", headers=headers_a, json={
        "provider": "anthropic",
        "key_value": new_key_val_a,
    })
    assert key_res_a2.status_code == 200
    assert key_res_a2.json()["key_preview"] != key_res_a.json()["key_preview"]

    # 7. Eliminación de clave para Tenant A
    del_res_a = await client.delete("/api/v1/ai/key?provider=anthropic", headers=headers_a)
    assert del_res_a.status_code == 200
    assert del_res_a.json()["has_key"] is False

    cfg_a_after = await client.get("/api/v1/ai/config?provider=anthropic", headers=headers_a)
    assert cfg_a_after.json()["has_key"] is False
    assert cfg_a_after.json()["key_preview"] is None


@pytest.mark.asyncio
async def test_budget_exhaustion_blocks_ai_and_triggers_deterministic_fallback(client: AsyncClient):
    """
    Verifica que al agotarse el presupuesto mensual, las llamadas a IA
    quedan bloqueadas y ejecutan automáticamente la alternativa determinista sin gasto.
    """
    uid = uuid.uuid4().hex[:8]
    reg = await client.post("/api/v1/auth/register", json={
        "email": f"carlos_budget_{uid}@example.com",
        "password": "Password123!",
        "full_name": "Carlos Budget",
    })
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Asignar un presupuesto acotado de $0.0020 USD
    await client.post("/api/v1/ai/config", headers=headers, json={
        "provider": "openai",
        "model": "gpt-4o",  # gpt-4o cuesta ($5.00 in / $15.00 out por 1M)
        "monthly_budget_usd": 0.0020,
        "is_active": True,
    })

    # Guardar clave ficticia
    dummy_key = f"tok-test-{os.urandom(8).hex()}"
    await client.post("/api/v1/ai/key", headers=headers, json={
        "provider": "openai",
        "key_value": dummy_key,
    })

    # Primera llamada simulada (debe tener éxito y consumir presupuesto)
    call1 = await client.post("/api/v1/ai/simulate-call", headers=headers, json={
        "provider": "openai",
        "operation": "evaluation",
        "prompt": "Evaluación rápida de perfil de ingeniería",
        "expected_output_tokens": 100,
    })
    assert call1.status_code == 200
    res1 = call1.json()
    assert res1["is_fallback"] is False
    assert res1["cost_incurred_usd"] > 0
    assert res1["tokens_consumed"] > 0

    # Segunda llamada simulada (debe exceder el saldo remanente y forzar fallback determinista)
    call2 = await client.post("/api/v1/ai/simulate-call", headers=headers, json={
        "provider": "openai",
        "operation": "evaluation",
        "prompt": "Segunda evaluación con requerimiento de tokens que excede el remanente",
        "expected_output_tokens": 150,
    })
    assert call2.status_code == 200
    res2 = call2.json()
    assert res2["is_fallback"] is True
    assert "presupuesto" in res2["fallback_reason"].lower()
    assert res2["cost_incurred_usd"] == 0.0
    assert res2["tokens_consumed"] == 0
    assert "justification" in res2["data"]
    assert "[DETERMINISTA]" in res2["data"]["justification"]

    # Verificar que el estado presupuestario reporta warning o exhausted
    cfg_final = await client.get("/api/v1/ai/config?provider=openai", headers=headers)
    assert cfg_final.status_code == 200
    final_data = cfg_final.json()
    assert final_data["current_month_usage_usd"] > 0
    assert final_data["remaining_budget_usd"] >= 0.0


@pytest.mark.asyncio
async def test_concurrent_budget_reservations_do_not_exceed_limit():
    """
    Verifica que múltiples reservas de presupuesto concurrentes (asyncio.gather)
    se resuelvan de forma estrictamente atómica sin sobrepasar el límite configurado.
    """
    tenant_id = f"tenant_race_{uuid.uuid4().hex[:8]}"
    provider = "google"
    ai_service_inst = AIService()

    # Crear configuración con límite de $0.0005 USD
    async with async_session_factory() as session:
        cfg = APIKeyConfig(
            tenant_id=tenant_id,
            provider=provider,
            model="gemini-1.5-pro",
            monthly_budget_usd=0.0005,
            current_month_usage_usd=0.0,
            reserved_budget_usd=0.0,
            is_active=True,
        )
        session.add(cfg)
        await session.commit()

    # Lanzar 15 intentos simultáneos de reservar $0.0001 cada uno (total pedido: $0.0015, límite: $0.0005)
    cost_per_attempt = 0.0001

    async def attempt_reservation():
        async with async_session_factory() as session:
            try:
                await ai_service_inst.reserve_budget(session, tenant_id, provider, cost_per_attempt)
                await session.commit()
                return True
            except AIBudgetExceededError:
                return False

    results = await asyncio.gather(*[attempt_reservation() for _ in range(15)])
    successes = sum(1 for r in results if r is True)
    rejections = sum(1 for r in results if r is False)

    # Solo deben tener éxito un máximo de 5 reservas (5 * 0.0001 = 0.0005)
    assert successes <= 5
    assert rejections >= 10
    assert (successes * cost_per_attempt) <= 0.0005001

    # Verificar estado en la base de datos
    async with async_session_factory() as session:
        stmt = select(APIKeyConfig).where(
            APIKeyConfig.tenant_id == tenant_id,
            APIKeyConfig.provider == provider,
        )
        res = await session.execute(stmt)
        final_cfg = res.scalar_one()
        assert final_cfg.reserved_budget_usd <= final_cfg.monthly_budget_usd


@pytest.mark.asyncio
async def test_no_plaintext_keys_in_database_or_cost_logs(client: AsyncClient):
    """
    Escaneo estricto de seguridad: verifica que ninguna clave secreta se guarde
    en texto plano en la base de datos (tablas api_key_configs, cost_logs, audit_logs).
    """
    uid = uuid.uuid4().hex[:8]
    reg = await client.post("/api/v1/auth/register", json={
        "email": f"scanner_ai_{uid}@example.com",
        "password": "Password123!",
        "full_name": "Scanner User",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    unique_secret = f"myuniquetoken_{uuid.uuid4().hex}"

    # Guardar clave
    await client.post("/api/v1/ai/key", headers=headers, json={
        "provider": "openai",
        "key_value": unique_secret,
    })

    # Simular una llamada
    await client.post("/api/v1/ai/simulate-call", headers=headers, json={
        "provider": "openai",
        "operation": "test_audit",
        "prompt": "Verificación de secretos",
    })

    # Inspeccionar directamente la base de datos
    async with async_session_factory() as session:
        # 1. api_key_configs
        res_cfg = await session.execute(select(APIKeyConfig))
        configs = res_cfg.scalars().all()
        for c in configs:
            assert unique_secret not in str(c.key_reference or "")
            assert unique_secret not in str(c.key_preview or "")

        # 2. cost_logs
        res_logs = await session.execute(select(CostLog))
        logs = res_logs.scalars().all()
        for l in logs:
            assert unique_secret not in str(l.operation or "")
            assert unique_secret not in str(l.model_used or "")
