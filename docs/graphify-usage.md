# Uso de Graphify en Máquina de Chamba

## Información

- **Herramienta**: [Graphify](https://github.com/Graphify-Labs/graphify)
- **Paquete**: `graphifyy` (PyPI)
- **Versión instalada**: 0.9.2
- **Propósito**: Navegación eficiente del codebase durante desarrollo
- **Requerido para usuario final**: NO

## Instalación

```bash
pip install graphifyy
# Verificar
graphify --version
python3 -c "import graphify; print('OK')"
```

## Configuración del proyecto

### Exclusiones obligatorias

Los siguientes archivos/carpetas están excluidos del índice:
- `.env` y archivos con secretos
- `*.db`, `*.sqlite3` — bases de datos
- `data/`, `backups/` — datos de usuario
- `node_modules/`, `venv/`, `__pycache__/`
- CV reales, documentos personales (`.pdf`, `.docx` en carpetas de usuario)
- `graphify-out/` — outputs propios de Graphify

### Construir índice

```bash
cd /home/walt/Escritorio/MAQUINACHAMBA
graphify .
```

### Consultas útiles

```bash
# Localizar módulos
graphify query "¿dónde se implementa la política de autorización?"
graphify query "¿qué módulos dependen de approvals?"
graphify query "¿cómo fluye una candidatura desde descubrimiento hasta ejecución?"

# Encontrar conexiones
graphify path "AuthorizationPolicy" "ScheduledTask"

# Actualizar tras cambios
graphify --update .
```

## Métricas

### Medición exploratoria local de Work — 2026-10-15

Evidencia: `docs/graphify-baseline.json` (incluye hashes del código e índice).
Se usó `graphify.benchmark.run_benchmark` sobre `backend/graphify-out/graph.json`,
con `corpus_words=1872`, contado desde los archivos Python listados por
`git ls-files backend` en su estado de trabajo. Consultas: `hash_content`,
`validate_url`, `AuthorizationPolicy`, `ScheduledTask`.

- Índice parcial AST: 77 nodos, 68 aristas.
- Corpus completo estimado por la herramienta: 2496 tokens.
- Contexto recuperado estimado promedio: 687 tokens (4 consultas con resultados).
- Cociente heurístico: 3.6x frente a cargar todo ese corpus.
- Tiempo local de la función de benchmark: 0.0051 segundos; no mide indexación ni tiempo de una tarea de desarrollo.

**No es ahorro real demostrado**: Graphify aproxima el corpus por palabras y
el contexto por caracteres, con métodos distintos. No cuenta lectura posterior
del código, razonamiento, respuestas, mantenimiento del índice ni calidad.
Tampoco compara contra búsqueda dirigida con `rg`. No se realizaron llamadas
a proveedores de IA ni se midió facturación. Mantener pendiente la comparación
de tareas equivalentes y el ahorro neto antes de declarar cumplido ese requisito.

### Línea base (Fase 0)

| Métrica | Sin Graphify | Con Graphify | Notas |
|---------|:---:|:---:|-------|
| Archivos cargados para localizar módulo | Pendiente | Pendiente | Medir en tarea equivalente |
| Tokens de entrada estimados | Pendiente | Pendiente | |
| Tokens de salida estimados | Pendiente | Pendiente | |
| Tiempo de respuesta | Pendiente | Pendiente | |
| Calidad del resultado | Pendiente | Pendiente | |
| Costo del índice | — | Pendiente | |

*Las métricas se completarán cuando el codebase tenga suficiente contenido para una comparación significativa.*

## Actualización

```bash
pip install --upgrade graphifyy
```

## Notas

- Graphify es una herramienta de DESARROLLO. No es necesaria para ejecutar la aplicación.
- El grafo no sustituye la lectura del código ni las pruebas.
- Confirmar siempre los hallazgos contra el código fuente.
- No indexar secretos, credenciales ni datos personales.
