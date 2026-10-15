# Contribuir a Máquina de Chamba

## Configuración del entorno

### Requisitos
- Python 3.12+
- Node.js 18+
- SQLite 3
- Git

### Setup

```bash
# Clonar
git clone <url>
cd maquinachamba

# Backend
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt

# Configuración
cp .env.example .env
# Editar .env

# Frontend
cd frontend && npm install && cd ..

# Verificar
pytest tests/
```

### Graphify (desarrollo)

Graphify es obligatorio durante el desarrollo.

```bash
pip install graphifyy
# Construir índice inicial
graphify .
# Consultar antes de investigar
graphify query "¿dónde está el módulo de aprobaciones?"
```

## Reglas del proyecto

1. **Todo cambio necesita prueba**: no se acepta código sin test.
2. **Aislamiento por tenant**: toda query filtra por `tenant_id`.
3. **Sin secretos en código**: `.env` para configuración sensible.
4. **Aprobación obligatoria**: ningún envío sin registro de aprobación.
5. **Simulación por defecto**: `SIMULATION_MODE=true` siempre en tests.
6. **Cambios pequeños y revisables**: PRs focalizados.

## Estructura de commits

```
tipo(módulo): descripción breve

Cuerpo explicativo si es necesario.

Refs: #issue
```

Tipos: `feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `chore`

## Flujo de revisión

1. Branch desde `main`.
2. Implementar con pruebas.
3. Actualizar Graphify si cambió la estructura.
4. PR con checklist:
   - [ ] Tests pasan
   - [ ] Sin secretos en el código
   - [ ] Documentación actualizada
   - [ ] Graphify actualizado (si aplica)
