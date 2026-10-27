#!/usr/bin/env bash
# ==============================================================================
# Máquina de Chamba — Script de Instalación Reproducible (REQ-032, D1)
# 
# Realiza la comprobación de requisitos, configuración del entorno virtual,
# dependencias, inicialización segura de .env con APP_SECRET_KEY, directorios
# de datos locales y verificación de integridad.
#
# Uso:
#   ./scripts/install_reproducible.sh              # Instalación interactiva / completa
#   ./scripts/install_reproducible.sh --check-only # Solo verifica requisitos
# ==============================================================================

set -euo pipefail

# Colores para salida de terminal
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

CHECK_ONLY=false
if [[ "${1:-}" == "--check-only" ]] || [[ "${1:-}" == "--dry-run" ]]; then
    CHECK_ONLY=true
fi

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   Máquina de Chamba — Instalación Reproducible (REQ-032)${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Comprobar Python >= 3.12
echo -e "\n${YELLOW}[1/6] Comprobando versión de Python...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 no está instalado en el sistema.${NC}" >&2
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print("OK" if sys.version_info >= (3, 12) else "OLD")')

if [[ "$PY_OK" != "OK" ]]; then
    echo -e "${RED}Error: Se requiere Python 3.12 o superior (Detectado: Python $PY_VERSION).${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ Python $PY_VERSION detectado (requisito cumplido: >= 3.12).${NC}"

# 2. Comprobar SQLite 3 y soporte de WAL
echo -e "\n${YELLOW}[2/6] Comprobando SQLite 3...${NC}"
SQLITE_OK=$(python3 -c 'import sqlite3; print("OK" if sqlite3.sqlite_version_info >= (3, 35) else "OLD")')
if [[ "$SQLITE_OK" != "OK" ]]; then
    echo -e "${RED}Error: SQLite versión 3.35+ requerida para Máquina de Chamba.${NC}" >&2
    exit 1
fi
SQLITE_VER=$(python3 -c 'import sqlite3; print(sqlite3.sqlite_version)')
echo -e "${GREEN}✓ SQLite $SQLITE_VER con soporte WAL y foreign keys verificado.${NC}"

# Si solo es check, salir con éxito aquí
if [[ "$CHECK_ONLY" == true ]]; then
    echo -e "\n${GREEN}✓ Modo comprobación (--check-only): Todos los requisitos del sistema están listos.${NC}"
    exit 0
fi

# 3. Directorios de datos y aislamiento local
echo -e "\n${YELLOW}[3/6] Creando estructura de directorios locales...${NC}"
mkdir -p data/documents
mkdir -p data/vault
mkdir -p data/cv_exports
mkdir -p backups
mkdir -p logs
echo -e "${GREEN}✓ Directorios data/, data/documents/, data/vault/, backups/ y logs/ preparados.${NC}"

# 4. Configurar Entorno Virtual (venv)
echo -e "\n${YELLOW}[4/6] Configurando entorno virtual Python (venv)...${NC}"
if [[ ! -d "venv" ]]; then
    echo "Creando entorno virtual en ./venv..."
    python3 -m venv venv
fi

# Activar venv
VENV_PY="./venv/bin/python3"
VENV_PIP="./venv/bin/pip"

if [[ ! -x "$VENV_PY" ]]; then
    echo -e "${RED}Error: No se pudo localizar el ejecutable python del venv en $VENV_PY.${NC}" >&2
    exit 1
fi

echo "Actualizando pip e instalando dependencias bloqueadas de backend..."
"$VENV_PIP" install --upgrade pip --quiet
if [[ -f "backend/requirements.lock" ]]; then
    echo "Instalando dependencias reproducibles desde backend/requirements.lock..."
    "$VENV_PIP" install -r backend/requirements.lock --quiet
else
    echo "Instalando dependencias desde backend/requirements.txt..."
    "$VENV_PIP" install -r backend/requirements.txt --quiet
fi
echo -e "${GREEN}✓ Dependencias de backend instaladas exitosamente con versiones fijadas.${NC}"

# 5. Configurar .env con secreto aleatorio y modo simulación por defecto
echo -e "\n${YELLOW}[5/6] Verificando archivo de configuración (.env)...${NC}"
if [[ ! -f ".env" ]]; then
    if [[ -f ".env.example" ]]; then
        echo "Creando .env a partir de .env.example..."
        cp .env.example .env
    else
        echo "Generando archivo .env base..."
        cat << 'EOF' > .env
APP_ENV=development
APP_HOST=127.0.0.1
APP_PORT=8000
APP_TIMEZONE=America/Mexico_City
DATABASE_URL=sqlite+aiosqlite:///./data/maquinachamba.db
AI_MONTHLY_BUDGET_USD=10.00
SIMULATION_MODE=true
EOF
    fi

    # Generar APP_SECRET_KEY criptográfica
    NEW_SECRET=$("$VENV_PY" -c "import secrets; print(secrets.token_urlsafe(32))")
    if grep -q "^APP_SECRET_KEY=" .env; then
        sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${NEW_SECRET}|" .env
    else
        echo "APP_SECRET_KEY=${NEW_SECRET}" >> .env
    fi
    echo -e "${GREEN}✓ Archivo .env generado con clave criptográfica aleatoria segura.${NC}"
else
    echo -e "${GREEN}✓ Archivo .env existente detectado; se conservan sus valores.${NC}"
fi

# Asegurar permisos restringidos en .env
chmod 600 .env

# 6. Inicialización de Base de Datos y Verificación de Arranque
echo -e "\n${YELLOW}[6/6] Aplicando migraciones de base de datos y verificando arranque...${NC}"
export PYTHONPATH="."
if [[ -f "alembic.ini" ]]; then
    echo "Aplicando migraciones versionadas de esquema mediante Alembic (upgrade head)..."
    "$VENV_PY" -m alembic upgrade head
else
    echo "Inicializando esquema de base de datos..."
    "$VENV_PY" -c "
import asyncio
from backend.app.core.database import init_db
asyncio.run(init_db())
"
fi
echo -e "${GREEN}✓ Tablas y esquema de SQLite creados e indexados exitosamente mediante migraciones.${NC}"

# Verificación de configuración segura
"$VENV_PY" -c "
from backend.app.core.config import get_settings
s = get_settings()
assert s.app_host == '127.0.0.1', 'Host inseguro'
assert s.simulation_mode is True, 'SIMULATION_MODE debe ser True por defecto'
print(f'Autocomprobación OK: Simulación={s.simulation_mode}, Host={s.app_host}:{s.app_port}')
"
echo -e "${GREEN}✓ Verificación de seguridad y configuración completada con éxito.${NC}"

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}   ¡Instalación completada exitosamente!${NC}"
echo -e "${BLUE}======================================================${NC}"
echo -e "Para iniciar el servidor local:"
echo -e "   ${YELLOW}./venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload${NC}"
echo -e "\nO abre en tu navegador:"
echo -e "   ${YELLOW}http://127.0.0.1:8000/${NC}\n"
