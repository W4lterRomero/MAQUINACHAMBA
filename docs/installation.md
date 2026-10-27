# Guía de Instalación Reproducible (REQ-032, D1)

Esta guía describe el procedimiento determinista para desplegar **Máquina de Chamba** de forma local en Linux, macOS o Windows (vía WSL2).

---

## 1. Requisitos del Sistema

| Componente | Versión Mínima | Justificación |
|---|:---:|---|
| **Python** | `3.12+` | Tipado moderno, `dataclasses`, `asyncio`, compatibilidad con Pydantic v2 y SQLAlchemy 2.0. |
| **SQLite** | `3.35+` | Soporte nativo de `WAL mode`, claves foráneas y queries deterministas. |
| **Git** | `2.30+` | Clonación del repositorio y control de versiones. |
| **Navegador** | Cualquier moderno | Chrome, Firefox, Safari o Edge para acceder al panel web local. |

---

## 2. Instalación en Un Solo Paso (Recomendada)

El repositorio incluye un script automatizado y seguro que verifica los requisitos, prepara el entorno virtual, instala dependencias, genera una clave criptográfica aleatoria para `.env` y comprueba la base de datos:

```bash
# 1. Clonar el repositorio
git clone <url-repositorio> maquinachamba
cd maquinachamba

# 2. Ejecutar el instalador reproducible
./scripts/install_reproducible.sh
```

Para verificar exclusivamente que tu sistema cumple con todos los requisitos sin realizar cambios:

```bash
./scripts/install_reproducible.sh --check-only
```

---

## 3. Instalación Manual Paso a Paso

Si prefieres realizar el despliegue de forma manual o en un entorno cerrado:

### 3.1. Crear y activar el entorno virtual
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
# Dependencias reproducibles fijadas (recomendado):
pip install -r backend/requirements.lock
# Alternativamente:
# pip install -r backend/requirements.txt
```

### 3.2. Configuración del archivo de entorno `.env`
Copia el archivo de ejemplo:
```bash
cp .env.example .env
```

Genera una clave secreta segura de 32 bytes y asígnala a `APP_SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Asegúrate de que los permisos de `.env` sean restringidos (`chmod 600 .env`):
```bash
chmod 600 .env
```

> [!IMPORTANT]
> `SIMULATION_MODE=true` debe mantenerse activo hasta que el usuario decida autorizar explícitamente operaciones externas reales.

### 3.3. Crear directorios de datos locales
```bash
mkdir -p data/documents data/vault data/cv_exports backups logs
```

### 3.4. Inicializar la base de datos con migraciones versionadas
```bash
# Aplicar migraciones con Alembic
alembic upgrade head
```

---

## 4. Ejecución del Servidor

Inicia el servidor local de desarrollo:

```bash
./venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

Abre tu navegador en:
**[http://127.0.0.1:8000/](http://127.0.0.1:8000/)**

---

## 5. Verificación de la Instalación

Ejecuta la suite de pruebas automatizadas para certificar la integridad de todos los módulos:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/ -v
```

---

## 6. Resolución de Problemas Frecuentes

1. **Error de versión de Python:**
   Si tu comando `python3` apunta a una versión anterior a 3.12, instala Python 3.12 usando el gestor de paquetes de tu distribución (ej. `sudo apt install python3.12 python3.12-venv`) y crea el entorno con `python3.12 -m venv venv`.
2. **Conflicto de puerto 8000:**
   Edita `APP_PORT` en `.env` (ej. `APP_PORT=8080`) e inicia uvicorn con `--port 8080`.
3. **Permisos de base de datos:**
   Verifica que la carpeta `data/` tenga permisos de lectura y escritura para tu usuario.
