"""
Prueba unitaria para deduplicación unificada entre Remotive, Gemini, ChatGPT y Manual.
Verifica que todos los orígenes utilicen la misma canonicalización y hash criptográfico.
"""

import pytest
from backend.app.modules.vacancies.ingestion import canonicalize_url, compute_dedup_hash


def test_canonicalize_url_strips_tracking_params_and_normalizes():
    """Verifica que diversos formatos de URL de diferentes orígenes se canonicalicen al mismo string."""
    url_remotive = "https://remotive.com/job/senior-python-dev-12345?utm_source=jobboard&utm_medium=api"
    url_gemini = "https://remotive.com/job/senior-python-dev-12345/?ref=gemini_digest"
    url_chatgpt = "HTTPS://REMOTIVE.COM/job/senior-python-dev-12345?fbclid=xyz&trk=email"
    url_clean = "https://remotive.com/job/senior-python-dev-12345"

    canon_remotive = canonicalize_url(url_remotive)
    canon_gemini = canonicalize_url(url_gemini)
    canon_chatgpt = canonicalize_url(url_chatgpt)
    canon_clean = canonicalize_url(url_clean)

    assert canon_remotive == url_clean
    assert canon_gemini == url_clean
    assert canon_chatgpt == url_clean
    assert canon_clean == url_clean


def test_compute_dedup_hash_cross_source_uniformity():
    """Verifica que Remotive, Gemini y ChatGPT generen exactamente el mismo dedup_hash."""
    url_remotive = "https://remotive.com/job/python-dev?utm_source=remotive"
    url_chatgpt = "https://remotive.com/job/python-dev?ref=chatgpt"
    url_gemini = "https://remotive.com/job/python-dev/"

    canon_remotive = canonicalize_url(url_remotive)
    canon_chatgpt = canonicalize_url(url_chatgpt)
    canon_gemini = canonicalize_url(url_gemini)

    company_a = "TechCorp Inc."
    company_b = "techcorp inc"
    company_c = "  TECHCORP INC.  "

    title_a = "Senior Backend Engineer (Python)"
    title_b = "senior backend engineer python"
    title_c = "Senior Backend Engineer - Python"

    hash_remotive = compute_dedup_hash(canon_remotive, company_a, title_a)
    hash_chatgpt = compute_dedup_hash(canon_chatgpt, company_b, title_b)
    hash_gemini = compute_dedup_hash(canon_gemini, company_c, title_c)

    assert hash_remotive == hash_chatgpt
    assert hash_chatgpt == hash_gemini
    assert len(hash_remotive) == 64
