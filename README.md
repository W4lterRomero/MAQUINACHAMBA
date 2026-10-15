# Máquina de Chamba 🔧

Aplicación open source local-first para gestionar la búsqueda laboral, personalizar candidaturas y ejecutar postulaciones previamente autorizadas.

## Estado actual

🚧 **Fase 0 — Fundamentos** — En desarrollo

## Características principales

- **Perfil profesional verificado**: importa CV, GitHub y proyectos con revisión humana.
- **CV maestros especializados**: mantén varios CV (desarrollo, QA, soporte, etc.) basados en tu perfil verificado.
- **Descubrimiento de vacantes**: importación manual, CSV, APIs, alertas por correo.
- **Evaluación inteligente**: compatibilidad explicada con reglas deterministas + IA.
- **Personalización de candidaturas**: CV y mensajes adaptados sin inventar hechos.
- **Aprobación obligatoria**: revisas todo antes de que se envíe algo.
- **Ejecución programada**: apruebas hoy, se ejecuta mañana.
- **CRM integrado**: seguimiento completo del ciclo de candidatura.
- **BYOK**: usas tu propia API key con presupuesto configurable.
- **Seguridad desde el inicio**: simulación por defecto, aislamiento de datos, auditoría.

## Requisitos

- Python 3.12+
- Node.js 18+
- SQLite 3

## Instalación rápida

```bash
# Clonar el repositorio
git clone <url> maquinachamba
cd maquinachamba

# Backend
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt

# Configuración
cp .env.example .env
# Editar .env con tus valores

# Frontend
cd frontend
npm install
cd ..

# Iniciar
python -m backend.main
```

## Arquitectura

Ver [ARCHITECTURE.md](ARCHITECTURE.md) para la arquitectura completa.

## Seguridad

Ver [SECURITY.md](SECURITY.md) para el modelo de amenazas.

## Documentación

- [Modelo de datos](docs/data-model.md)
- [Estados y transiciones](docs/data-model.md#estados-de-candidatura-crm)
- [Decisiones de arquitectura](docs/adr/)
- [Especificación y requisitos](docs/requirements.md)

## Licencia

Pendiente de decisión del propietario. No publicar sin autorización.

## Desarrollo

Este proyecto usa [Graphify](https://github.com/Graphify-Labs/graphify) obligatoriamente durante el desarrollo para navegación eficiente del codebase. Graphify NO es requerido para ejecutar la aplicación.
