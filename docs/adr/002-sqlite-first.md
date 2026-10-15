# ADR-002: SQLite primero, PostgreSQL después

## Estado
Aceptada

## Contexto
La aplicación necesita persistencia durable. La decisión del propietario indica SQLite para MVP y PostgreSQL para evolución.

## Decisión
SQLite con WAL mode como base de datos principal del MVP. Migraciones con Alembic para evolución a PostgreSQL.

## Justificación
1. **Zero dependencies**: SQLite no requiere instalar ni configurar un servidor de BD.
2. **Local-first**: un solo archivo, fácil de respaldar y restaurar.
3. **WAL mode**: soporta lecturas concurrentes sin bloqueo.
4. **SQLAlchemy abstraction**: el ORM permite migrar a PostgreSQL cambiando la URL.
5. **Alembic migrations**: versionan el schema, reproducibles en cualquier motor.

## Consecuencias
- Las escrituras concurrentes están limitadas (un escritor a la vez con WAL).
- No hay soporte nativo para JSONB (usamos JSON con validación en aplicación).
- Las migraciones deben probarse en ambos motores antes de la Fase 5.
- El scheduler debe usar locks a nivel de aplicación para evitar race conditions.
