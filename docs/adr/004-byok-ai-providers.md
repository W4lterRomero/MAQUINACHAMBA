# ADR-004: BYOK — Bring Your Own Key

## Estado
Aceptada

## Contexto
Decisión #14 del propietario: cada usuario aporta su propia API key.

## Decisión
Implementar un sistema de proveedores de IA intercambiables con presupuesto por usuario.

## Diseño
1. **Adaptador por proveedor**: interfaz común para OpenAI, Anthropic, Google.
2. **Presupuesto**: límite mensual en USD por tenant.
3. **Control de concurrencia**: máximo de peticiones simultáneas.
4. **Registro de consumo**: tokens de entrada/salida y estimación de costo.
5. **Caché aislada**: por tenant para evitar repetir consultas idénticas.
6. **Determinismo primero**: reglas antes de IA.
7. **Validación estructurada**: las respuestas de IA se validan con Pydantic.

## Consecuencias
- Las estimaciones de costo son locales y pueden diferir de la facturación real del proveedor.
- El sistema debe funcionar sin IA configurada (modo degradado con solo reglas).
- Las API keys se almacenan en .env o keyring, nunca en la BD.
