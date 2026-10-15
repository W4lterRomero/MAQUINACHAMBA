"""
Router para cola de tareas y ciclo de ejecución durable (REQ-031, REQ-019, REQ-020).
Permite operar y ejecutar tareas sin mantener abierto el IDE.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session, get_tenant_id
from backend.app.core.clock import get_clock
from backend.app.modules.approvals.models import ScheduledTask
from backend.app.modules.execution.worker import DurableExecutionWorker

router = APIRouter(prefix="/execution", tags=["Execution & Tasks"])


@router.get("/tasks")
async def list_scheduled_tasks(
    status_filter: Optional[str] = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """Lista las tareas programadas en la cola con su estado actual."""
    where_conds = [ScheduledTask.tenant_id == tenant_id]
    if status_filter:
        where_conds.append(ScheduledTask.status == status_filter)

    stmt = select(ScheduledTask).where(*where_conds).order_by(ScheduledTask.scheduled_date.asc())
    res = await session.execute(stmt)
    tasks = res.scalars().all()

    return [
        {
            "id": t.id,
            "candidacy_id": t.candidacy_id,
            "scheduled_date": t.scheduled_date.isoformat(),
            "scheduled_time": t.scheduled_time.isoformat() if t.scheduled_time else None,
            "execution_instant_utc": t.execution_instant_utc.isoformat(),
            "status": t.status,
            "idempotency_key": t.idempotency_key,
            "locked_by": t.locked_by,
            "attempts": t.attempts,
        }
        for t in tasks
    ]


@router.post("/process-cycle", status_code=status.HTTP_200_OK)
async def process_execution_cycle(
    worker_id: str = "api-worker-1",
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(get_tenant_id),
):
    """
    REQ-031: Ejecuta un ciclo completo del worker durable:
    1. Recupera tareas huérfanas o interrumpidas (recover_stale_tasks).
    2. Busca tareas pendientes cuyo instante programado ya haya vencido.
    3. Reclama atómicamente con lease y fencing token.
    4. Ejecuta respetando el modo simulación y registrando evidencia auditable.
    """
    clock = get_clock()
    worker = DurableExecutionWorker(worker_id=worker_id, clock=clock)

    # 1. Recuperación de tareas interrumpidas para este tenant
    recovered = await DurableExecutionWorker.recover_stale_tasks(
        session=session,
        clock=clock,
        tenant_id=tenant_id,
    )

    # 2. Buscar tareas pendientes vencidas
    now = clock.now_utc()
    stmt = (
        select(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.status == "pending",
            ScheduledTask.locked_by.is_(None),
            ScheduledTask.execution_instant_utc <= now,
        )
        .order_by(ScheduledTask.execution_instant_utc.asc())
    )
    res = await session.execute(stmt)
    due_tasks = res.scalars().all()

    executed_results = []
    for t in due_tasks:
        claimed = await worker.claim_task(session=session, task_id=t.id, tenant_id=tenant_id)
        if not claimed:
            continue

        success, message = await worker.execute_task(session=session, task=claimed)
        executed_results.append({
            "task_id": claimed.id,
            "success": success,
            "message": message,
        })

    return {
        "worker_id": worker_id,
        "recovered_tasks": recovered,
        "processed_tasks": executed_results,
    }
