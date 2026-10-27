"""
Pruebas unitarias para filtrado de vacantes de muestra, sin enlace válido o no verificadas (P0 Item 8).
Garantiza que solo oportunidades reales, con enlace verificado y estado confirmado puedan pasar
a la etapa de preparación y postulación.
"""

import pytest
from backend.app.modules.vacancies.models import Vacancy
from backend.app.modules.vacancies.service import VacancyIntegrityService


def test_sample_vacancy_is_rejected():
    """Vacante explícitamente marcada como muestra (is_real_search_result=False o source=sample) es rechazada."""
    vac = Vacancy(
        company_name="Empresa Ficticia",
        job_title="Software Engineer",
        source="sample",
        source_url="https://jobs.github.com/positions/123",
        is_real_search_result=False,
        verification_status="verified",
        is_active=True,
    )
    is_ready, reason = VacancyIntegrityService.validate_vacancy_readiness_for_application(vac)
    assert is_ready is False
    assert "muestra" in reason.lower()


def test_invalid_or_example_url_is_rejected():
    """Vacante con dominio example.com, localhost o URL inválida es rechazada."""
    vac_example = Vacancy(
        company_name="Acme Corp",
        job_title="Backend Developer",
        source="remotive",
        source_url="https://example.com/unverified-search-job/123",
        is_real_search_result=True,
        verification_status="verified",
        is_active=True,
    )
    is_ready, reason = VacancyIntegrityService.validate_vacancy_readiness_for_application(vac_example)
    assert is_ready is False
    assert "ejemplo" in reason.lower() or "example.com" in reason.lower()

    vac_no_url = Vacancy(
        company_name="Acme Corp",
        job_title="Backend Developer",
        source="remotive",
        source_url="",
        is_real_search_result=True,
        verification_status="verified",
        is_active=True,
    )
    is_ready2, reason2 = VacancyIntegrityService.validate_vacancy_readiness_for_application(vac_no_url)
    assert is_ready2 is False
    assert "enlace" in reason2.lower()


def test_unverified_vacancy_is_rejected():
    """Vacante con verification_status != 'verified' es rechazada según el principio extraer != verificar."""
    vac_pending = Vacancy(
        company_name="Tech Solutions",
        job_title="Fullstack Developer",
        source="remotive",
        source_url="https://remotive.com/job/tech-solutions-fullstack",
        is_real_search_result=True,
        verification_status="pending_verification",
        is_active=True,
    )
    is_ready, reason = VacancyIntegrityService.validate_vacancy_readiness_for_application(vac_pending)
    assert is_ready is False
    assert "verificaci" in reason.lower() or "verificado" in reason.lower()


def test_real_verified_vacancy_is_accepted():
    """Vacante real con URL pública válida y estado confirmado es aceptada para postulación."""
    vac_valid = Vacancy(
        company_name="Globex Tech",
        job_title="Lead Python Architect",
        source="remotive",
        source_url="https://remotive.com/remote-jobs/software-dev/lead-python-architect-9988",
        is_real_search_result=True,
        verification_status="verified",
        is_active=True,
        fraud_signals=[],
    )
    is_ready, reason = VacancyIntegrityService.validate_vacancy_readiness_for_application(vac_valid)
    assert is_ready is True
    assert "lista para postulación" in reason.lower()
