# ADR-001: Monolito modular

## Estado
Aceptada

## Contexto
Necesitamos definir la estrategia arquitectónica para Máquina de Chamba. Las opciones son:
- Microservicios
- Monolito modular
- Serverless

## Decisión
Monolito modular con separación clara de módulos.

## Justificación
1. **Simplicidad de despliegue**: un solo proceso, ideal para local-first.
2. **Bajo overhead operativo**: sin necesidad de orquestación, service mesh ni message brokers.
3. **Refactoring fácil**: los módulos comparten proceso pero tienen fronteras claras.
4. **Evolución natural**: si un módulo necesita escalar independientemente, se puede extraer.
5. **Coherente con decisión #1 del propietario**: aplicación instalable local.

## Consecuencias
- Los módulos deben comunicarse mediante interfaces explícitas, no acceso directo a modelos de otros módulos.
- La base de datos es compartida pero cada módulo tiene sus tablas.
- El scheduler y worker corren en el mismo proceso (configurables como procesos separados).
- La migración a servidor (decisión #2) no requiere cambiar la arquitectura, solo la configuración de despliegue.
