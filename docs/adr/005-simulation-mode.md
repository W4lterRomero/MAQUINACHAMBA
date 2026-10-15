# ADR-005: Modo simulación por defecto

## Estado
Aceptada

## Contexto
Decisión de seguridad: la aplicación debe ser segura desde el primer arranque.

## Decisión
`SIMULATION_MODE=true` por defecto. En modo simulación:

1. Todas las acciones externas se preparan pero NO se ejecutan.
2. Los adaptadores de portales registran qué harían sin hacer peticiones reales.
3. Los emails se generan pero no se envían.
4. Los logs muestran `[SIMULACIÓN]` en cada acción que sería externa.
5. El dashboard muestra claramente que el modo simulación está activo.

## Consecuencias
- El usuario debe cambiar explícitamente a `SIMULATION_MODE=false` para operación real.
- Esto permite probar todo el flujo sin riesgo.
- Las pruebas automatizadas siempre corren en modo simulación.
- El adaptador de LinkedIn siempre opera en modo asistido, independiente de este flag.
