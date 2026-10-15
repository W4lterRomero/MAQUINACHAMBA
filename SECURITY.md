# Máquina de Chamba — Modelo de seguridad

## Principios

1. **Defensa en profundidad**: múltiples capas, no un solo punto de control.
2. **Mínimo privilegio**: cada componente accede solo a lo necesario.
3. **Simulación por defecto**: modo seguro hasta autorización explícita.
4. **Datos locales**: todo reside en la máquina del usuario.
5. **Sin secretos en código**: claves en `.env`, nunca en Git.

## Modelo de amenazas

### Activos a proteger

| Activo | Sensibilidad | Ubicación |
|--------|:---:|---|
| API keys (AI, email) | 🔴 Crítica | `.env`, keyring del OS |
| Credenciales de portales | 🔴 Crítica | Secrets store encriptado |
| CV y datos personales | 🟠 Alta | SQLite + filesystem |
| Historial de candidaturas | 🟠 Alta | SQLite |
| Perfil de comunicación | 🟡 Media | SQLite |
| Configuración de la app | 🟢 Baja | SQLite + `.env` |

### Vectores de ataque y mitigaciones

| Vector | Riesgo | Mitigación |
|--------|:---:|---|
| Vacante maliciosa con prompt injection | 🔴 | Sanitización de entrada, política central, contenido importado no puede modificar reglas del agente |
| Repositorio/CV importado con payloads | 🔴 | Validación de archivos, sandboxing de importación, revisión humana obligatoria |
| Acceso no autorizado a la API local | 🟠 | Autenticación por sesión, bind a localhost, CORS restrictivo |
| Filtración de secretos en logs | 🟠 | Redacción automática de patterns sensibles en logs |
| Ejecución de candidatura no aprobada | 🔴 | Política central de autorización independiente del modelo, registros inmutables |
| SSRF via URLs de vacantes | 🟠 | Validación de URLs, bloqueo de rangos internos (RFC 1918, localhost, metadata) |
| Duplicación de envíos | 🟠 | Idempotency keys, reconciliación, locks en cola |
| Pérdida de datos | 🟡 | Backups automáticos, migraciones versionadas |
| Escalación de privilegios entre usuarios | 🟠 | Aislamiento por tenant_id en todas las queries |

### Controles de seguridad

#### Autenticación y sesiones
- Autenticación local con contraseña hasheada (bcrypt/argon2).
- Sesiones con timeout configurable.
- Tokens JWT con expiración corta para API.
- No se almacenan contraseñas en texto plano.

#### Autorización
- Política central de autorización para acciones externas.
- Independiente del modelo de IA: el modelo no puede autorizar acciones.
- Todo contenido importado pasa por sanitización antes de procesarse.
- Registros inmutables de aprobaciones vinculados a versiones de documentos.

#### Gestión de secretos
- API keys en `.env` o keyring del sistema operativo.
- Nunca en Git, logs, índices de Graphify ni CI.
- Rotación y revocación documentadas.
- Encriptación at-rest para credenciales de portales.

#### Validación de entrada
- Pydantic para validación estricta de todas las entradas.
- Sanitización de HTML/scripts en contenido importado.
- Límites de tamaño para archivos importados.
- Validación de URLs contra rangos internos (anti-SSRF).

#### Aislamiento
- `tenant_id` en todas las tablas con datos de usuario.
- Queries siempre filtradas por tenant.
- Filesystem organizado por tenant.
- Caché aislada por usuario.

#### Auditoría
- Log de todas las acciones de aprobación y ejecución.
- Sin secretos ni datos personales innecesarios en logs.
- Exportación y eliminación de datos del usuario.
- Trazabilidad completa: quién aprobó qué, cuándo, con qué contenido.

#### Protección contra prompt injection
- Contenido de vacantes, CV importados y correos tratado como datos, no instrucciones.
- Separación estricta entre prompt del sistema y contenido del usuario.
- El contenido importado no puede: conceder permisos, modificar reglas del agente, habilitar herramientas.
- Validación estructurada de respuestas de IA.

## Datos que salen del equipo

| Operación | Destino | Datos enviados | Control |
|-----------|---------|---------------|--------|
| Evaluación con IA | Proveedor AI (BYOK) | Perfil anonimizado + vacante | Usuario configura proveedor |
| Búsqueda de vacantes | Portales de empleo | Queries de búsqueda | Usuario inicia |
| Postulación | Portal destino | CV, carta, datos formulario | Aprobación obligatoria |
| Email | Servidor SMTP | Contenido del correo | Aprobación obligatoria |
| GitHub import | GitHub API | Token de acceso | Permisos mínimos |

## Procedimiento de respuesta

1. Identificar y contener.
2. Evaluar impacto.
3. Notificar al usuario.
4. Corregir y documentar.
5. Actualizar controles.
