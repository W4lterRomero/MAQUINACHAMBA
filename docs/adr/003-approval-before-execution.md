# ADR-003: Aprobación obligatoria antes de ejecución

## Estado
Aceptada

## Contexto
La decisión #3 del propietario establece que tras aprobar una candidatura, se ejecuta al día siguiente sin reconfirmaciones. Pero la decisión #11 requiere revisar vacante, CV y mensaje en cada aprobación.

## Decisión
Implementar un ciclo de tres fases con registros inmutables:

1. **Preparación**: el sistema descubre, clasifica y prepara. Ningún envío.
2. **Aprobación**: el usuario revisa y decide. Se genera un registro inmutable con hash del contenido.
3. **Ejecución**: al día siguiente, se ejecuta SOLO si la aprobación sigue válida.

## Reglas
- Aprobar hoy programa para mañana, nunca envío inmediato.
- Cualquier cambio en contenido invalida la aprobación.
- Los rechazos y cancelaciones son terminales (no se envían).
- Las ventanas perdidas conservan la tarea sin enviar.
- Los resultados inciertos se registran y verifican antes de reintentar.
- El paquete aprobado incluye: CV, carta, formulario, destino, acciones.

## Consecuencias
- Cada aprobación genera un content_hash que se verifica antes de ejecutar.
- Se necesita versionado de documentos para soportar auditoría.
- El scheduler debe respetar zonas horarias y horario de verano.
- Los workers deben usar locks para evitar ejecución duplicada.
