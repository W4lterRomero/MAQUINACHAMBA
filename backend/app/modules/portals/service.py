"""
Servicio de gestión y verificación de autorizaciones persistentes por portal y por acción (P0 - Bloqueador 4 y 6).
Separa estrictamente los privilegios:
- read_email: Lectura de bandeja para sincronización de alertas
- search_jobs: Búsqueda y lectura de ofertas en portales externos
- register_account: Creación de cuenta en portal externo
- submit_application: Despacho final de postulación
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.modules.portals.models import PortalAuthorization

VALID_PORTAL_ACTIONS = {"read_email", "search_jobs", "register_account", "submit_application"}


class PortalAuthorizationService:
    """
    Gestiona el ciclo de vida de autorizaciones persistidas por tenant, portal y acción.
    Ninguna acción sensible (crear cuenta, postular) puede ejecutarse sin un registro activo en DB.
    """

    @staticmethod
    async def grant_authorization(
        session: AsyncSession,
        tenant_id: str,
        portal_name: str,
        action: str,
        authorized_by: str = "user",
        notes: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> PortalAuthorization:
        """Otorga o reactiva una autorización persistida para un portal y acción específica."""
        action_clean = action.strip().lower()
        if action_clean not in VALID_PORTAL_ACTIONS:
            raise ValueError(f"Acción '{action}' inválida. Acciones permitidas: {sorted(list(VALID_PORTAL_ACTIONS))}")

        portal_clean = portal_name.strip().lower()

        # Buscar si ya existe un registro
        stmt = select(PortalAuthorization).where(
            and_(
                PortalAuthorization.tenant_id == tenant_id,
                PortalAuthorization.portal_name == portal_clean,
                PortalAuthorization.action == action_clean,
            )
        )
        res = await session.execute(stmt)
        record = res.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if record:
            record.authorized = True
            record.authorized_by = authorized_by
            record.notes = notes
            record.expires_at = expires_at
            record.revoked_at = None
            record.updated_at = now
        else:
            record = PortalAuthorization(
                tenant_id=tenant_id,
                portal_name=portal_clean,
                action=action_clean,
                authorized=True,
                authorized_by=authorized_by,
                notes=notes,
                expires_at=expires_at,
                revoked_at=None,
            )
            session.add(record)

        await session.commit()
        await session.refresh(record)
        return record

    @staticmethod
    async def revoke_authorization(
        session: AsyncSession,
        tenant_id: str,
        auth_id: str,
    ) -> bool:
        """Revoca de forma inmutable una autorización persistida."""
        record = await session.get(PortalAuthorization, auth_id)
        if not record or record.tenant_id != tenant_id:
            return False

        record.authorized = False
        record.revoked_at = datetime.now(timezone.utc)
        await session.commit()
        return True

    @staticmethod
    async def list_authorizations(
        session: AsyncSession,
        tenant_id: str,
        portal_name: Optional[str] = None,
        action: Optional[str] = None,
    ) -> List[PortalAuthorization]:
        """Lista todas las autorizaciones persistidas para un tenant."""
        conditions = [PortalAuthorization.tenant_id == tenant_id]
        if portal_name:
            conditions.append(PortalAuthorization.portal_name == portal_name.strip().lower())
        if action:
            conditions.append(PortalAuthorization.action == action.strip().lower())

        stmt = (
            select(PortalAuthorization)
            .where(and_(*conditions))
            .order_by(PortalAuthorization.created_at.desc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def verify_authorization(
        session: AsyncSession,
        tenant_id: str,
        portal_name: str,
        action: str,
        simulation_mode: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """
        Verificación estricta en base de datos.
        Comprueba que exista un registro persistido activo, vigente y específico para la acción solicitada.
        """
        sim_mode = simulation_mode if simulation_mode is not None else get_settings().simulation_mode
        action_clean = action.strip().lower()
        portal_clean = portal_name.strip().lower()

        if action_clean not in VALID_PORTAL_ACTIONS:
            return False, f"Acción solicitada '{action}' no es reconocida."

        stmt = select(PortalAuthorization).where(
            and_(
                PortalAuthorization.tenant_id == tenant_id,
                PortalAuthorization.portal_name == portal_clean,
                PortalAuthorization.action == action_clean,
            )
        )
        res = await session.execute(stmt)
        record = res.scalar_one_or_none()

        if not record:
            return False, (
                f"No existe autorización persistida para la acción '{action_clean}' en el portal '{portal_clean}'. "
                "Un parámetro booleano o flag en memoria no es evidencia suficiente."
            )

        now = datetime.now(timezone.utc)
        if not record.is_active(now=now):
            if record.revoked_at is not None:
                return False, f"La autorización para '{action_clean}' en '{portal_clean}' fue revocada."
            if record.expires_at is not None and record.expires_at < now:
                return False, f"La autorización para '{action_clean}' en '{portal_clean}' ha expirado."
            return False, f"La autorización para '{action_clean}' en '{portal_clean}' está deshabilitada."

        # Regla P0 de Seguridad: Mantener envíos desactivados mientras SIMULATION_MODE esté activo
        if action_clean == "submit_application" and sim_mode:
            return False, "Modo simulación activo. Envíos externos reales se mantienen estrictamente desactivados."

        return True, f"Acción '{action_clean}' en '{portal_clean}' autorizada mediante registro persistido."
