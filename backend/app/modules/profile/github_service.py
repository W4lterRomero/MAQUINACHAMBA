"""
Servicio de importación y análisis de proyectos desde GitHub (REQ-003, D12).
Reglas y directrices de seguridad:
1. Permisos mínimos: solo lectura de repositorios públicos o PAT con alcance read-only.
2. Contenido importado = datos, NUNCA instrucciones. Detección y filtrado de prompt injection.
3. Principio inmutable: Extraer != Verificar. Todo dato inicia con verification_status='pending'.
4. Modo simulación (SIMULATION_MODE=true): sin llamadas externas a api.github.com, datos reproducibles.
5. Aislamiento estricto por tenant_id y cero secretos en logs o base de datos.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Dict, List, Optional
import httpx

from backend.app.core.config import get_settings
from backend.app.core.security import detect_injection_signals, hash_content, redact_secrets
from backend.app.modules.profile.models import Experience, ProfessionalSource, Skill, UserProfile
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession


class GitHubImportError(Exception):
    """Error al importar repositorios desde GitHub."""
    pass


class GitHubImportService:
    """Servicio de importación segura de repositorios y proyectos de GitHub."""

    @classmethod
    def validate_username(cls, username: str) -> str:
        """Valida que el nombre de usuario de GitHub sea sintácticamente seguro."""
        clean_user = username.strip()
        if not re.match(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$", clean_user):
            raise GitHubImportError(
                f"Nombre de usuario de GitHub inválido ('{clean_user}'). Solo caracteres alfanuméricos y guiones simples."
            )
        return clean_user

    @classmethod
    async def fetch_repositories(
        cls,
        username: str,
        token: Optional[str] = None,
        max_repos: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Obtiene los repositorios del usuario.
        En SIMULATION_MODE=true devuelve repositorios sintéticos reproducibles sin acceso a red.
        En modo real consulta api.github.com con cabeceras de permisos mínimos.
        """
        valid_user = cls.validate_username(username)
        settings = get_settings()

        # Modo Simulación (por defecto): Cero llamadas externas
        if settings.simulation_mode:
            return cls._generate_simulated_repos(valid_user, max_repos)

        # Modo Real: Llamada HTTP acotada con timeout
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MaquinaDeChamba-LocalFirst/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"

        url = f"https://api.github.com/users/{valid_user}/repos?sort=pushed&per_page={min(max_repos, 10)}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    raise GitHubImportError(f"Usuario '{valid_user}' no encontrado en GitHub.")
                if resp.status_code == 401 or resp.status_code == 403:
                    raise GitHubImportError("Acceso no autorizado a GitHub o cuota de API excedida.")
                if resp.status_code != 200:
                    raise GitHubImportError(f"Error al conectar con GitHub API (HTTP {resp.status_code}).")

                data = resp.json()
                if not isinstance(data, list):
                    raise GitHubImportError("Respuesta inesperada de GitHub API.")

                repos = []
                for item in data[:max_repos]:
                    repos.append({
                        "name": str(item.get("name", "repo")),
                        "description": str(item.get("description") or ""),
                        "html_url": str(item.get("html_url", f"https://github.com/{valid_user}")),
                        "language": item.get("language") or "General",
                        "topics": [str(t) for t in item.get("topics", []) if isinstance(t, str)],
                        "stargazers_count": int(item.get("stargazers_count", 0)),
                        "forks_count": int(item.get("forks_count", 0)),
                        "updated_at": item.get("updated_at"),
                    })
                return repos
        except httpx.RequestError as exc:
            raise GitHubImportError(f"Fallo de red al conectar con GitHub API: {str(exc)}")

    @classmethod
    def _generate_simulated_repos(cls, username: str, max_repos: int) -> List[Dict[str, Any]]:
        """Genera repositorios simulados para pruebas y operación offline sin red."""
        catalog = [
            {
                "name": "fastapi-modular-backend",
                "description": "API REST asíncrona local-first construida con FastAPI, SQLAlchemy 2.0 y SQLite WAL.",
                "html_url": f"https://github.com/{username}/fastapi-modular-backend",
                "language": "Python",
                "topics": ["python", "fastapi", "sqlalchemy", "sqlite", "asyncio"],
                "stargazers_count": 14,
                "forks_count": 2,
                "updated_at": "2026-10-01T12:00:00Z",
            },
            {
                "name": "react-tailwind-dashboard",
                "description": "Panel de administración modular con TypeScript, Vite y componentes accesibles.",
                "html_url": f"https://github.com/{username}/react-tailwind-dashboard",
                "language": "TypeScript",
                "topics": ["typescript", "react", "vite", "tailwind"],
                "stargazers_count": 8,
                "forks_count": 1,
                "updated_at": "2026-09-20T10:30:00Z",
            },
            {
                "name": "docker-ci-automation",
                "description": "Pipelines de CI/CD reproducibles con GitHub Actions, Docker y linters estáticos.",
                "html_url": f"https://github.com/{username}/docker-ci-automation",
                "language": "Shell",
                "topics": ["docker", "ci-cd", "github-actions", "bash"],
                "stargazers_count": 5,
                "forks_count": 0,
                "updated_at": "2026-08-15T09:15:00Z",
            },
        ]
        return catalog[:max_repos]

    @classmethod
    async def import_github_profile(
        cls,
        session: AsyncSession,
        tenant_id: str,
        profile_id: str,
        username: str,
        token: Optional[str] = None,
        max_repos: int = 5,
    ) -> Dict[str, Any]:
        """
        Ejecuta la importación de repositorios de GitHub:
        1. Valida el usuario y el perfil del tenant.
        2. Obtiene los repositorios (simulados u online).
        3. Sanitiza descripciones contra inyecciones de prompt.
        4. Registra la fuente profesional inmutable (source_type='github').
        5. Crea habilidades y proyectos sugeridos en estado 'pending'.
        """
        settings = get_settings()

        # 1. Verificar perfil
        prof_res = await session.execute(
            select(UserProfile).where(
                and_(UserProfile.id == profile_id, UserProfile.tenant_id == tenant_id)
            )
        )
        profile = prof_res.scalar_one_or_none()
        if not profile:
            raise ValueError(f"Perfil {profile_id} no encontrado en el tenant.")

        # 2. Descargar o simular repositorios
        repos = await cls.fetch_repositories(username=username, token=token, max_repos=max_repos)
        if not repos:
            raise GitHubImportError(f"No se encontraron repositorios públicos para '{username}'.")

        # 3. Sanitización y detección de inyecciones
        all_injection_signals: List[str] = []
        sanitized_repos = []
        for r in repos:
            name = r["name"]
            desc = r["description"]
            signals = detect_injection_signals(f"{name} {desc}")
            if signals:
                all_injection_signals.extend(signals)
                # Neutralizar contenido malicioso si se detecta
                desc = "[Contenido sanitizado por detección de inyección de instrucciones]"
            sanitized_repos.append({**r, "description": desc})

        # 4. Crear ProfessionalSource
        raw_repr = json.dumps(sanitized_repos, ensure_ascii=False)
        content_hash = hash_content("github", username, raw_repr)
        source = ProfessionalSource(
            tenant_id=tenant_id,
            profile_id=profile.id,
            source_type="github",
            source_name=f"GitHub: @{username}",
            source_uri=f"https://github.com/{username}",
            file_hash=content_hash,
            raw_text=raw_repr,
            extracted_data={
                "username": username,
                "repositories_count": len(sanitized_repos),
                "is_simulated": settings.simulation_mode,
                "injection_signals": all_injection_signals,
            },
            verification_status="pending",
        )
        session.add(source)
        await session.flush()

        # 5. Extraer habilidades técnicas sugeridas (estado pending)
        skills_created: List[Dict[str, Any]] = []
        known_skill_names = set()

        for r in sanitized_repos:
            # Lenguaje principal
            lang = r.get("language")
            if lang and lang != "General" and lang.lower() not in known_skill_names:
                known_skill_names.add(lang.lower())
                sk = Skill(
                    tenant_id=tenant_id,
                    profile_id=profile.id,
                    source_id=source.id,
                    name=lang,
                    category="technical",
                    proficiency="intermediate",
                    source_fragment=f"Lenguaje primario en repositorio GitHub '{r['name']}'",
                    context_type="declared",
                    verification_status="pending",
                )
                session.add(sk)
                await session.flush()
                skills_created.append({"id": sk.id, "name": sk.name, "source_fragment": sk.source_fragment})

            # Topics / etiquetas
            for topic in r.get("topics", []):
                norm_topic = topic.strip()
                if norm_topic and norm_topic.lower() not in known_skill_names:
                    known_skill_names.add(norm_topic.lower())
                    sk = Skill(
                        tenant_id=tenant_id,
                        profile_id=profile.id,
                        source_id=source.id,
                        name=norm_topic.capitalize(),
                        category="tool" if norm_topic.lower() in ("docker", "git", "vite", "sqlite") else "technical",
                        proficiency="intermediate",
                        source_fragment=f"Etiqueta/Tema en repositorio GitHub '{r['name']}'",
                        context_type="interest",
                        verification_status="pending",
                    )
                    session.add(sk)
                    await session.flush()
                    skills_created.append({"id": sk.id, "name": sk.name, "source_fragment": sk.source_fragment})

        # 6. Extraer proyectos relevantes como experiencias sugeridas (estado pending)
        experiences_created: List[Dict[str, Any]] = []
        for r in sanitized_repos:
            exp_desc = f"Proyecto Open Source: {r['description']}. Tecnologías: {r.get('language')}, {', '.join(r.get('topics', []))}. Estrellas: {r.get('stargazers_count')}."
            exp = Experience(
                tenant_id=tenant_id,
                profile_id=profile.id,
                source_id=source.id,
                company=f"GitHub / {username}",
                title=f"Creador / Mantenedor de {r['name']}",
                date_precision="month",
                source_fragment=f"Repositorio público en {r.get('html_url')}",
                description=exp_desc,
                skills_used=[r.get("language")] + r.get("topics", []) if r.get("language") else r.get("topics", []),
                verification_status="pending",
            )
            session.add(exp)
            await session.flush()
            experiences_created.append({"id": exp.id, "company": exp.company, "title": exp.title})

        return {
            "source_id": source.id,
            "source_name": source.source_name,
            "source_uri": source.source_uri,
            "source_type": source.source_type,
            "verification_status": source.verification_status,
            "repositories_imported": len(sanitized_repos),
            "repositories": sanitized_repos,
            "skills_suggested": skills_created,
            "experiences_suggested": experiences_created,
            "has_injection_signals": len(all_injection_signals) > 0,
            "is_simulated": settings.simulation_mode,
        }
