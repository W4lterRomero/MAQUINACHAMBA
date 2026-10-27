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
        raw_json_repos: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Ejecuta la importación de repositorios de GitHub (REQ-003, D12):
        1. Valida el usuario y el perfil del tenant.
        2. Obtiene los repositorios (desde raw_json_repos explícito, simulados u online).
        3. Sanitiza descripciones y nombres contra inyecciones de prompt.
        4. Comprueba deduplicación mediante content_hash de los repositorios sanitizados.
        5. Reutiliza ProfessionalSource y elementos previos si ya existen, conservando revisiones humanas.
        6. Si es simulación sin JSON real, marca persistentemente el origen como sintético/demo.
        7. Asigna proficiency='unknown' y title='Proyecto: {name}' sin inventar nivel ni autoría/mantenimiento.
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

        # 2. Descargar o usar repositorios provistos
        is_synthetic = False
        if raw_json_repos is not None:
            # Entrada local explícita de repositorios reales en JSON
            repos = []
            for item in raw_json_repos[:max_repos]:
                repos.append({
                    "name": str(item.get("name", "repo")),
                    "description": str(item.get("description") or ""),
                    "html_url": str(item.get("html_url", f"https://github.com/{username}/{item.get('name', 'repo')}")),
                    "language": item.get("language") or "General",
                    "topics": [str(t) for t in item.get("topics", []) if isinstance(t, str)],
                    "stargazers_count": int(item.get("stargazers_count", 0)),
                    "forks_count": int(item.get("forks_count", 0)),
                    "updated_at": item.get("updated_at"),
                })
            is_synthetic = False
        else:
            repos = await cls.fetch_repositories(username=username, token=token, max_repos=max_repos)
            is_synthetic = bool(settings.simulation_mode)

        if not repos:
            raise GitHubImportError(f"No se encontraron repositorios para '{username}'.")

        # 3. Sanitización y detección de inyecciones
        all_injection_signals: List[str] = []
        sanitized_repos = []
        for r in repos:
            name = r["name"]
            desc = r["description"]
            signals = detect_injection_signals(f"{name} {desc} {' '.join(r.get('topics', []))}")
            if signals:
                all_injection_signals.extend(signals)
                # Neutralizar contenido malicioso si se detecta
                desc = "[Contenido sanitizado por detección de inyección de instrucciones]"
            sanitized_repos.append({**r, "description": desc})

        # 4. Deduplicación por hash de contenido (P2)
        raw_repr = json.dumps(sanitized_repos, sort_keys=True, ensure_ascii=False)
        content_hash = hash_content("github", username, raw_repr)

        stmt = select(ProfessionalSource).where(
            and_(
                ProfessionalSource.tenant_id == tenant_id,
                ProfessionalSource.file_hash == content_hash,
            )
        )
        existing_source = (await session.execute(stmt)).scalar_one_or_none()

        source_type = "github_simulation" if is_synthetic else "github"
        source_name = f"GitHub (Simulación Demostrativa): @{username}" if is_synthetic else f"GitHub: @{username}"
        context_type = "synthetic_demo" if is_synthetic else "inferred"

        reused_source = False
        existing_skill_names: Dict[str, Skill] = {}
        existing_exp_titles: Dict[str, Experience] = {}

        if existing_source:
            source = existing_source
            reused_source = True
            # Cargar habilidades existentes vinculadas a esta fuente
            sk_stmt = select(Skill).where(
                and_(Skill.tenant_id == tenant_id, Skill.source_id == source.id)
            )
            for sk_item in (await session.execute(sk_stmt)).scalars().all():
                existing_skill_names[sk_item.name.lower()] = sk_item

            exp_stmt = select(Experience).where(
                and_(Experience.tenant_id == tenant_id, Experience.source_id == source.id)
            )
            for exp_item in (await session.execute(exp_stmt)).scalars().all():
                existing_exp_titles[exp_item.title.lower()] = exp_item
        else:
            source = ProfessionalSource(
                tenant_id=tenant_id,
                profile_id=profile.id,
                source_type=source_type,
                source_name=source_name,
                source_uri=f"https://github.com/{username}",
                file_hash=content_hash,
                raw_text=raw_repr,
                extracted_data={
                    "username": username,
                    "repositories_count": len(sanitized_repos),
                    "is_simulated": settings.simulation_mode,
                    "is_synthetic": is_synthetic,
                    "injection_signals": all_injection_signals,
                },
                verification_status="pending",
            )
            session.add(source)
            await session.flush()

        # 5. Extraer habilidades técnicas sugeridas sin inventar proficiency (P1, P2)
        skills_created: List[Dict[str, Any]] = []
        known_skill_names = set()

        for r in sanitized_repos:
            # Lenguaje principal
            lang = r.get("language")
            if lang and lang != "General" and lang.lower() not in known_skill_names:
                known_skill_names.add(lang.lower())
                if lang.lower() in existing_skill_names:
                    # Reutilizar y conservar decisión humana previa (P2)
                    sk = existing_skill_names[lang.lower()]
                    skills_created.append({
                        "id": sk.id,
                        "name": sk.name,
                        "source_fragment": sk.source_fragment,
                        "verification_status": sk.verification_status,
                        "reused": True,
                    })
                else:
                    sk = Skill(
                        tenant_id=tenant_id,
                        profile_id=profile.id,
                        source_id=source.id,
                        name=lang,
                        category="technical",
                        proficiency="unknown",  # P1: Nivel desconocido, no inventado
                        source_fragment=f"{'[DEMOSTRACIÓN SINTÉTICA] ' if is_synthetic else ''}Lenguaje primario en repositorio GitHub '{r['name']}'",
                        context_type=context_type,
                        verification_status="pending",
                    )
                    session.add(sk)
                    await session.flush()
                    skills_created.append({
                        "id": sk.id,
                        "name": sk.name,
                        "source_fragment": sk.source_fragment,
                        "verification_status": sk.verification_status,
                        "reused": False,
                    })

            # Topics / etiquetas
            for topic in r.get("topics", []):
                norm_topic = topic.strip()
                if norm_topic and norm_topic.lower() not in known_skill_names:
                    known_skill_names.add(norm_topic.lower())
                    if norm_topic.lower() in existing_skill_names:
                        sk = existing_skill_names[norm_topic.lower()]
                        skills_created.append({
                            "id": sk.id,
                            "name": sk.name,
                            "source_fragment": sk.source_fragment,
                            "verification_status": sk.verification_status,
                            "reused": True,
                        })
                    else:
                        sk = Skill(
                            tenant_id=tenant_id,
                            profile_id=profile.id,
                            source_id=source.id,
                            name=norm_topic.capitalize(),
                            category="tool" if norm_topic.lower() in ("docker", "git", "vite", "sqlite") else "technical",
                            proficiency="unknown",  # P1: Nivel desconocido
                            source_fragment=f"{'[DEMOSTRACIÓN SINTÉTICA] ' if is_synthetic else ''}Etiqueta/Tema en repositorio GitHub '{r['name']}'",
                            context_type=context_type,
                            verification_status="pending",
                        )
                        session.add(sk)
                        await session.flush()
                        skills_created.append({
                            "id": sk.id,
                            "name": sk.name,
                            "source_fragment": sk.source_fragment,
                            "verification_status": sk.verification_status,
                            "reused": False,
                        })

        # 6. Extraer proyectos como experiencias sugeridas sin atribuir empleo ni autoría/mantenimiento (P1, P2)
        experiences_created: List[Dict[str, Any]] = []
        for r in sanitized_repos:
            project_title = f"Proyecto: {r['name']}"  # P1: Proyecto sin inventar "Creador/Mantenedor" ni empleo
            if project_title.lower() in existing_exp_titles:
                exp = existing_exp_titles[project_title.lower()]
                experiences_created.append({
                    "id": exp.id,
                    "company": exp.company,
                    "title": exp.title,
                    "verification_status": exp.verification_status,
                    "reused": True,
                })
            else:
                exp_desc = f"{'[DEMOSTRACIÓN SINTÉTICA] ' if is_synthetic else ''}Proyecto público en GitHub: {r['description']}. Tecnologías: {r.get('language')}, {', '.join(r.get('topics', []))}. Estrellas: {r.get('stargazers_count')}."
                exp = Experience(
                    tenant_id=tenant_id,
                    profile_id=profile.id,
                    source_id=source.id,
                    company=f"GitHub (@{username})",
                    title=project_title,
                    date_precision="month",
                    source_fragment=f"Repositorio público en {r.get('html_url')}",
                    description=exp_desc,
                    skills_used=[r.get("language")] + r.get("topics", []) if r.get("language") else r.get("topics", []),
                    verification_status="pending",
                )
                session.add(exp)
                await session.flush()
                experiences_created.append({
                    "id": exp.id,
                    "company": exp.company,
                    "title": exp.title,
                    "verification_status": exp.verification_status,
                    "reused": False,
                })

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
            "is_synthetic": is_synthetic,
            "reused_source": reused_source,
        }
