"""
Servicio de reportes consolidados y notificación por correo electrónico (REQ-028, D8).
Reglas y directrices de seguridad:
1. En SIMULATION_MODE=true: Cero llamadas SMTP o sockets de red salientes.
2. Todo envío simulado se rotula con [SIMULACIÓN] y se registra de forma local.
3. Métricas y reportes estrictamente aislados por tenant_id.
4. Generación determinista de reportes en texto plano y HTML estructurado.
"""

from datetime import date, datetime, timedelta, timezone
import json
import logging
import smtplib
from typing import Any, Dict, List, Optional
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import case, func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.modules.approvals.models import Approval, Candidacy, ScheduledTask
from backend.app.modules.crm.models import CRMEntry
from backend.app.modules.profile.models import Skill, UserProfile
from backend.app.modules.secrets.models import APIKeyConfig
from backend.app.modules.users.models import Tenant, User
from backend.app.modules.vacancies.models import Vacancy

logger = logging.getLogger("maquinachamba.email")


class EmailReportService:
    """Generador de reportes consolidados y despachador de correo electrónico."""

    @classmethod
    async def get_dashboard_metrics(
        cls,
        session: AsyncSession,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Obtiene las métricas consolidadas del tenant para el dashboard y reportes."""
        settings = get_settings()
        # 1. Conteo de vacantes
        vac_stmt = select(
            func.count(Vacancy.id),
            func.coalesce(func.sum(case((Vacancy.is_active.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Vacancy.is_active.is_(False), 1), else_=0)), 0),
        ).where(Vacancy.tenant_id == tenant_id)
        vac_res = await session.execute(vac_stmt)
        vac_total, vac_active, vac_closed = vac_res.first() or (0, 0, 0)

        # Vacantes sospechosas
        suspect_stmt = select(Vacancy).where(Vacancy.tenant_id == tenant_id)
        suspect_res = await session.execute(suspect_stmt)
        all_vacs = suspect_res.scalars().all()
        vac_suspect = sum(1 for v in all_vacs if any(s.get("severity") == "high" for s in (v.fraud_signals or [])))

        # 2. Conteo de candidaturas por estado
        cand_stmt = select(
            Candidacy.status,
            func.count(Candidacy.id)
        ).where(Candidacy.tenant_id == tenant_id).group_by(Candidacy.status)
        cand_res = await session.execute(cand_stmt)
        cand_counts: Dict[str, int] = {row[0]: row[1] for row in cand_res.all()}

        # 3. Tareas programadas para la ventana de mañana
        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        sched_stmt = select(func.count(ScheduledTask.id)).where(
            and_(
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.scheduled_date == tomorrow,
                ScheduledTask.status.in_(["pending", "executing"]),
            )
        )
        sched_res = await session.execute(sched_stmt)
        scheduled_for_tomorrow = sched_res.scalar_one_or_none() or 0

        # Tareas con atención requerida
        intervention_stmt = select(func.count(ScheduledTask.id)).where(
            and_(
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status.in_(["result_uncertain", "missed_window", "failed"]),
            )
        )
        intervention_res = await session.execute(intervention_stmt)
        tasks_requiring_intervention = intervention_res.scalar_one_or_none() or 0

        # 4. Habilidades verificadas vs pendientes
        skill_stmt = select(
            func.coalesce(func.sum(case((Skill.verification_status == "verified", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Skill.verification_status == "pending", 1), else_=0)), 0),
        ).where(Skill.tenant_id == tenant_id)
        skill_res = await session.execute(skill_stmt)
        skills_verified, skills_pending = skill_res.first() or (0, 0)

        # 5. Presupuesto de IA
        ai_stmt = select(APIKeyConfig).where(
            and_(APIKeyConfig.tenant_id == tenant_id, APIKeyConfig.is_active.is_(True))
        )
        ai_res = await session.execute(ai_stmt)
        ai_cfg = ai_res.scalars().first()
        ai_info = {
            "configured": ai_cfg is not None,
            "provider": ai_cfg.provider if ai_cfg else "none",
            "model": ai_cfg.model if ai_cfg else "none",
            "monthly_budget_usd": ai_cfg.monthly_budget_usd if ai_cfg else 0.0,
            "current_month_usage_usd": ai_cfg.current_month_usage_usd if ai_cfg else 0.0,
            "remaining_budget_usd": max(0.0, ai_cfg.monthly_budget_usd - ai_cfg.current_month_usage_usd) if ai_cfg else 0.0,
        }

        return {
            "vacancies": {
                "total": vac_total or 0,
                "active": vac_active or 0,
                "suspect": vac_suspect,
                "closed": vac_closed or 0,
            },
            "candidacies": {
                "total": sum(cand_counts.values()),
                "pending_approval": cand_counts.get("pending_approval", 0),
                "approved": cand_counts.get("approved", 0),
                "scheduled": cand_counts.get("scheduled", 0),
                "sent_confirmed": cand_counts.get("sent_confirmed", 0),
                "requires_intervention": cand_counts.get("requires_intervention", 0),
                "rejected_by_user": cand_counts.get("rejected_by_user", 0),
                "other": sum(v for k, v in cand_counts.items() if k not in ["pending_approval", "approved", "scheduled", "sent_confirmed", "requires_intervention", "rejected_by_user"]),
            },
            "execution": {
                "scheduled_for_tomorrow": scheduled_for_tomorrow,
                "tasks_requiring_intervention": tasks_requiring_intervention,
            },
            "profile": {
                "verified_skills": skills_verified or 0,
                "pending_review_skills": skills_pending or 0,
            },
            "ai_budget": ai_info,
            "simulation_mode": settings.simulation_mode,
        }

    @classmethod
    async def build_daily_report(
        cls,
        session: AsyncSession,
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Genera el contenido del reporte diario consolidado en texto y HTML."""
        settings = get_settings()
        metrics = await cls.get_dashboard_metrics(session, tenant_id)

        # Obtener datos del tenant y usuario
        u_stmt = select(User).where(User.tenant_id == tenant_id)
        u_res = await session.execute(u_stmt)
        user = u_res.scalars().first()
        user_name = user.full_name if user else "Candidato"
        user_email = user.email if user else "usuario@localhost"

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sim_tag = "[SIMULACIÓN] " if settings.simulation_mode else ""
        subject = f"{sim_tag}Máquina de Chamba — Resumen Diario de Búsqueda Laboral ({today_str})"

        # Texto plano
        text_lines = [
            f"=== MÁQUINA DE CHAMBA — REPORTE DIARIO ({today_str}) ===",
            f"Destinatario: {user_name} <{user_email}>",
            f"Modo: {'SIMULACIÓN (Sin envíos reales)' if settings.simulation_mode else 'PRODUCCIÓN'}",
            "",
            "1. POSTULACIONES PROGRAMADAS PARA MAÑANA:",
            f"   - Tareas listas para ventana de despacho: {metrics['execution']['scheduled_for_tomorrow']}",
            f"   - Candidaturas pendientes de tu aprobación: {metrics['candidacies']['pending_approval']}",
            "",
            "2. ESTADO DEL CRM DE BÚSQUEDA LABORAL:",
            f"   - Total de candidaturas activas: {metrics['candidacies']['total']}",
            f"   - Postulaciones enviadas confirmadas: {metrics['candidacies']['sent_confirmed']}",
            f"   - Tareas con atención requerida: {metrics['execution']['tasks_requiring_intervention']}",
            "",
            "3. RADAR DE VACANTES:",
            f"   - Vacantes activas disponibles: {metrics['vacancies']['active']}",
            f"   - Vacantes sospechosas / riesgo detectado: {metrics['vacancies']['suspect']}",
            f"   - Vacantes cerradas o expiradas: {metrics['vacancies']['closed']}",
            "",
            "4. PRESUPUESTO DE IA (BYOK):",
            f"   - Proveedor activo: {metrics['ai_budget']['provider'].upper()} ({metrics['ai_budget']['model']})",
            f"   - Saldo mensual restante: ${metrics['ai_budget']['remaining_budget_usd']:.4f} USD de ${metrics['ai_budget']['monthly_budget_usd']:.2f} USD",
            "",
            "Recuerda que todas las postulaciones de mañana deben haber sido aprobadas previamente por ti.",
            "Accede a tu panel local: http://127.0.0.1:8000/",
        ]
        text_body = "\n".join(text_lines)

        # HTML estructurado
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0b1120; color: #e2e8f0; padding: 20px; }}
          .card {{ background: #131c2e; border: 1px solid #1e293b; border-radius: 8px; padding: 20px; max-width: 600px; margin: 0 auto; }}
          .header {{ border-bottom: 1px solid #1e293b; padding-bottom: 15px; margin-bottom: 20px; }}
          .stat-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }}
          .stat-box {{ background: rgba(255,255,255,0.03); border: 1px solid #1e293b; border-radius: 6px; padding: 12px; }}
          .stat-num {{ font-size: 20px; font-weight: bold; color: #38bdf8; margin-top: 4px; }}
          .badge-sim {{ background: rgba(16, 185, 129, 0.2); color: #34d399; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
        </style></head>
        <body>
          <div class="card">
            <div class="header">
              <span class="badge-sim">{'SIMULACIÓN LOCAL' if settings.simulation_mode else 'OPERACIÓN REAL'}</span>
              <h2 style="margin: 10px 0 0 0; color: #f8fafc;">Resumen Diario de Búsqueda Laboral</h2>
              <div style="font-size: 13px; color: #94a3b8;">Fecha: {today_str} | Candidato: {user_name}</div>
            </div>
            <div class="stat-grid">
              <div class="stat-box">
                <div style="font-size: 12px; color: #94a3b8;">Despacho de Mañana</div>
                <div class="stat-num">{metrics['execution']['scheduled_for_tomorrow']}</div>
              </div>
              <div class="stat-box">
                <div style="font-size: 12px; color: #94a3b8;">Pendientes Aprobación</div>
                <div class="stat-num">{metrics['candidacies']['pending_approval']}</div>
              </div>
              <div class="stat-box">
                <div style="font-size: 12px; color: #94a3b8;">Postulaciones Enviadas</div>
                <div class="stat-num">{metrics['candidacies']['sent_confirmed']}</div>
              </div>
              <div class="stat-box">
                <div style="font-size: 12px; color: #94a3b8;">Saldo IA Restante</div>
                <div class="stat-num">${metrics['ai_budget']['remaining_budget_usd']:.2f} USD</div>
              </div>
            </div>
            <div style="font-size: 13px; color: #94a3b8; border-top: 1px solid #1e293b; padding-top: 15px;">
              Vacantes activas: <strong>{metrics['vacancies']['active']}</strong> | Sospechosas: <strong>{metrics['vacancies']['suspect']}</strong> | Habilidades verificadas: <strong>{metrics['profile']['verified_skills']}</strong>
            </div>
          </div>
        </body>
        </html>
        """

        return {
            "subject": subject,
            "recipient": user_email,
            "user_name": user_name,
            "date": today_str,
            "text_body": text_body,
            "html_body": html_body,
            "metrics": metrics,
            "is_simulation": settings.simulation_mode,
        }

    @classmethod
    async def send_daily_report(
        cls,
        session: AsyncSession,
        tenant_id: str,
        custom_recipient: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Envía el reporte diario por correo.
        En SIMULATION_MODE=true registra el envío sin conectar a servidores SMTP externos.
        """
        settings = get_settings()
        report_data = await cls.build_daily_report(session, tenant_id)
        recipient = custom_recipient or report_data["recipient"]

        if settings.simulation_mode:
            logger.info(
                f"[SIMULACIÓN] Reporte diario de correo generado para {recipient}: {report_data['subject']}"
            )
            return {
                "status": "simulated_success",
                "message": "Reporte diario generado exitosamente en modo simulación (cero envíos a red externa).",
                "subject": report_data["subject"],
                "recipient": recipient,
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
                "is_simulation": True,
                "preview_text": report_data["text_body"][:300] + "...",
            }

        # Modo Real (cuando SIMULATION_MODE=false)
        # Requiere variables de entorno configuradas
        smtp_host = os.getenv("EMAIL_SMTP_HOST")
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        smtp_user = os.getenv("EMAIL_ADDRESS")
        smtp_pass = os.getenv("EMAIL_APP_PASSWORD")

        if not (smtp_host and smtp_user and smtp_pass):
            raise ValueError(
                "Servidor SMTP no configurado en variables de entorno (EMAIL_SMTP_HOST, EMAIL_ADDRESS, EMAIL_APP_PASSWORD)."
            )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = report_data["subject"]
        msg["From"] = smtp_user
        msg["To"] = recipient

        msg.attach(MIMEText(report_data["text_body"], "plain", "utf-8"))
        msg.attach(MIMEText(report_data["html_body"], "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10.0) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        return {
            "status": "success",
            "message": f"Reporte diario enviado exitosamente a {recipient}.",
            "subject": report_data["subject"],
            "recipient": recipient,
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
            "is_simulation": False,
        }
