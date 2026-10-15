# Changelog

Todos los cambios notables del proyecto se documentan aquí.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/).

## [Sin publicar]

### Fase 0 — Fundamentos

#### Agregado
- Estructura inicial del proyecto.
- Documentación de arquitectura (ARCHITECTURE.md).
- Modelo de seguridad (SECURITY.md).
- Modelo de datos completo (docs/data-model.md).
- Decisiones de arquitectura (docs/adr/).
- Matriz de requisitos (docs/requirements.md).
- Configuración con Pydantic Settings.
- Base de datos async con SQLAlchemy 2.0.
- Política central de autorización.
- Validación de URLs anti-SSRF.
- Detección de prompt injection.
- Redacción de secretos en logs.
- Modelos ORM para aprobaciones, tareas y ejecución.
- FastAPI app con health check.
- Graphify instalado (v0.9.2) e instrucciones en AGENTS.md.
- Pruebas iniciales de seguridad y configuración.
- CI con GitHub Actions.
