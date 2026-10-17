"""
Servicio determinista de detección de vacantes cerradas y señales de fraude laboral (REQ-009, D5).
Aplica reglas estrictas para alertar al usuario antes de preparar o postular a una vacante sospechosa o expirada.
"""

from datetime import date, datetime, timezone
import re
from typing import Any, Dict, List, Optional, Tuple


FRAUD_PATTERNS = [
    # 1. Cobro previo o inversión para trabajar (alta gravedad)
    {
        "id": "upfront_payment",
        "severity": "high",
        "title": "Solicitud de pago o depósito anticipado",
        "patterns": [
            r"pago\s+por\s+adelantado",
            r"dep[oó]sito\s+(?:previo|de\s+garant[ií]a|inicial)",
            r"cuota\s+de\s+(?:inscripci[oó]n|recuperaci[oó]n)",
            r"comprar\s+(?:material|herramientas|kit)",
            r"kit\s+de\s+(?:inicio|bienvenida)\s+con\s+costo",
            r"pagar\s+por\s+capacitaci[oó]n",
            r"transferir\s+para\s+gastos\s+administrativos",
            r"inversi[oó]n\s+inicial\s+requerida",
        ],
    },
    # 2. Medios de pago anómalos o de alto riesgo (alta gravedad)
    {
        "id": "unconventional_payment",
        "severity": "high",
        "title": "Pago o transacciones mediante criptomonedas o tarjetas de regalo",
        "patterns": [
            r"pago\s+en\s+(?:bitcoin|crypto|usdt|criptomonedas?)",
            r"tarjetas?\s+de\s+regalo(?:\s+amazon)?",
            r"recibir\s+transferencias?\s+a\s+su\s+cuenta\s+personal",
            r"triangulaci[oó]n\s+de\s+fondos",
        ],
    },
    # 3. Canales informales exclusivos y ausencia de proceso corporativo (mediana gravedad)
    {
        "id": "suspicious_contact_channel",
        "severity": "medium",
        "title": "Canal de contacto informal exclusivo sin dominio corporativo",
        "patterns": [
            r"contactar\s+(?:exclusivamente|solo)\s+por\s+telegram",
            r"escribir\s+solo\s+al\s+telegram",
            r"sin\s+entrevista(?:\s+previa)?,\s+contrataci[oó]n\s+inmediata",
            r"sin\s+revisi[oó]n\s+de\s+antecedentes",
        ],
    },
    # 4. Promesas irreales de ingresos sin requisitos (alta gravedad)
    {
        "id": "unrealistic_income_promise",
        "severity": "high",
        "title": "Promesa de ingresos desproporcionados sin experiencia requerida",
        "patterns": [
            r"gana\s+\$?\d+[\d,.]*\s+(?:al\s+d[ií]a|diarios?)\s+sin\s+experiencia",
            r"ingresos\s+garantizados\s+de\s+\$?\d+[\d,.]*\s+trabajando\s+\d+\s+horas",
            r"hazte\s+rico\s+desde\s+casa",
        ],
    },
]

CLOSED_VACANCY_PATTERNS = [
    r"vacante\s+cerrada",
    r"convocatoria\s+(?:cerrada|finalizada|concluida)",
    r"posici[oó]n\s+(?:cerrada|cubierta)",
    r"ya\s+no\s+(?:se\s+aceptan|recibimos)\s+(?:postulaciones|candidaturas|cv)",
    r"proceso\s+de\s+selecci[oó]n\s+finalizado",
    r"plazo\s+de\s+postulaci[oó]n\s+vencido",
]


class VacancyIntegrityService:
    """Servicio de validación de integridad, detección de vacantes cerradas y señales de fraude (REQ-009)."""

    @classmethod
    def analyze_vacancy(
        cls,
        title: str,
        description: str,
        source_url: str = "",
        salary_min: Optional[float] = None,
        salary_max: Optional[float] = None,
        salary_currency: Optional[str] = "USD",
        closing_date: Optional[date] = None,
        reference_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """
        Analiza el texto y metadatos de la vacante para determinar:
        - Señales de fraude detectadas (con severidad, fragmento de evidencia y regla).
        - Estado de cierre / expiración (con motivo detallado).
        """
        text_corpus = f"{title}\n{description}"
        today = reference_date or datetime.now(timezone.utc).date()

        # 1. Detección de fraude
        detected_signals: List[Dict[str, str]] = []
        for group in FRAUD_PATTERNS:
            for pat in group["patterns"]:
                match = re.search(pat, text_corpus, re.IGNORECASE)
                if match:
                    detected_signals.append({
                        "id": group["id"],
                        "severity": group["severity"],
                        "title": group["title"],
                        "matched_text": match.group(0),
                        "evidence_fragment": text_corpus[max(0, match.start() - 25):min(len(text_corpus), match.end() + 25)].strip(),
                    })
                    break  # Solo una coincidencia por grupo para evitar redundancia

        # Detección adicional de salario absurdo (> $250,000 USD/mes para un puesto sin calificación o general)
        if salary_max and (salary_currency or "USD").upper() == "USD" and salary_max >= 250000:
            if "director" not in title.lower() and "vp" not in title.lower() and "chief" not in title.lower():
                detected_signals.append({
                    "id": "unrealistic_income_promise",
                    "severity": "high",
                    "title": "Salario extremadamente anómalo para el puesto declarado",
                    "matched_text": f"${salary_max:,.2f} {salary_currency}",
                    "evidence_fragment": f"Salario máximo declarado de ${salary_max:,.2f} USD.",
                })

        # 2. Detección de vacante cerrada o expirada
        is_closed = False
        closure_reason = None

        if closing_date and closing_date < today:
            is_closed = True
            closure_reason = f"Fecha límite de postulación vencida ({closing_date.isoformat()} < {today.isoformat()})."
        else:
            for pat in CLOSED_VACANCY_PATTERNS:
                match = re.search(pat, text_corpus, re.IGNORECASE)
                if match:
                    is_closed = True
                    closure_reason = f"El texto de la publicación indica cierre: '{match.group(0)}'."
                    break

        is_fraud_suspect = any(s["severity"] == "high" for s in detected_signals)

        return {
            "fraud_signals": detected_signals,
            "is_fraud_suspect": is_fraud_suspect,
            "has_warnings": len(detected_signals) > 0,
            "is_closed": is_closed,
            "closure_reason": closure_reason,
            "is_active": not is_closed,
        }
