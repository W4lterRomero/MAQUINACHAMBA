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
