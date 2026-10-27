# Máquina de Chamba — Matriz de requisitos

### Convenciones de Estado

- **✅ Implementado y Verificado**: Lógica completamente desarrollada y verificada mediante suites de pruebas automatizadas locales.
- **🟡 Simulado (Verificado)**: Flujo completamente implementado y verificado en modo local seguro (`SIMULATION_MODE=true` o modo asistido) sin conexiones directas externas no autorizadas.
- **⏳ Pendiente**: Funcionalidad programada para fases posteriores o dependiente de autorización explícita para efectos en vivo externos.

> [!NOTE]
> Conforme a las reglas de desarrollo y seguridad, el sistema opera con `SIMULATION_MODE=true` por defecto. No se declara "100%" ni "blindado" mientras existan adaptadores con capacidades externas no autorizadas o pendientes de despliegue en producción.

## Requisitos funcionales

| ID | Requisito | Decisión | Fase | Prioridad | Estado | Implementación y Evidencia |
|----|-----------|----------|:----:|:---------:|:------:|----------------------------|
| REQ-001 | Perfil profesional configurable sin editar código | D7, D12 | 1 | P1 | ✅ Implementado y Verificado | `api/profile.py`, `test_api_v1_endpoints.py::test_profile_and_cv_api_flow` |
| REQ-002 | Importar CV en PDF/DOCX con revisión humana | D12 | 2 | P1 | ✅ Implementado y Verificado | `test_cv_import_and_verification.py`, `test_cv_import_and_review_ui.py` |
| REQ-003 | Importar GitHub con autorización y permisos mínimos | D12 | 2 | P1 | ✅ Implementado y Verificado | Sin niveles/autoría inventados, hash de contenido, separación sintético/real. `test_github_import.py` |
| REQ-004 | Varios CV maestros especializados | D9 | 2 | P1 | ✅ Implementado y Verificado | `api/profile.py`, `cv/models.py`, `test_api_v1_endpoints.py` |
| REQ-005 | Consistencia entre CV maestros y perfil | D9 | 2 | P1 | ✅ Implementado y Verificado | `test_cv_import_and_review_ui.py`, `api/cv.py` |
| REQ-006 | Perfil de comunicación con cuestionario | D10 | 2 | P1 | ✅ Implementado y Verificado | `test_communication_profile.py`, `api/communication.py` |
| REQ-007 | Descubrir e importar vacantes | D5 | 1 | P1 | ✅ Implementado y Verificado (Búsqueda Real + Protocolo de Consentimiento) | Preferencias persistentes por tenant (puestos, habilidades, ubicación, modalidad, experiencia, salarios, exclusiones). Conector de lectura Remotive público (`remotive.py`). Protocolo de consentimiento previo (modo estructurado local por defecto, live autorizado). `api/vacancies.py`, `search_service.py`, `test_job_search_real.py`. |
| REQ-008 | Deduplicación de vacantes | D5 | 1 | P1 | ✅ Implementado y Verificado | Deduplicación determinista (`dedup_hash` SHA-256 de URL, empresa y título). Normalización, enlace original, fechas (`posted_date`, `fetched_at`) y campos desconocidos explícitos. `test_job_search_real.py::test_search_execution_exclusions_and_deduplication`. |
| REQ-009 | Detección de vacantes cerradas y fraude | D5 | 1 | P2 | ✅ Implementado y Verificado | `test_vacancy_fraud_and_closure.py`, `vacancies/service.py` |
| REQ-010 | Evaluar compatibilidad con justificación | D5 | 1 | P1 | ✅ Implementado y Verificado | `api/evaluations.py`, `test_api_v1_endpoints.py` |
| REQ-011 | Reglas deterministas antes de IA | D14 | 1 | P1 | ✅ Implementado y Verificado | `evaluation/service.py`, `api/evaluations.py` |
| REQ-012 | Adaptar CV sin inventar hechos | D9,D12 | 2 | P1 | ✅ Implementado y Verificado | `test_communication_profile.py`, `test_cv_export_and_documents.py` |
| REQ-013 | Generar documentos PDF/DOCX | D9 | 2 | P1 | ✅ Implementado y Verificado | `cv/generator.py`, `test_cv_export_and_documents.py` |
| REQ-014 | Revisar vacante, CV y mensaje antes de aprobar | D11 | 1 | P0 | ✅ Implementado y Verificado | `api/approvals.py::get_candidacy_for_review`, `test_api_v1_endpoints.py` |
| REQ-015 | Aprobar hoy, ejecutar mañana | D3 | 1 | P0 | ✅ Implementado y Verificado | `api/approvals.py::approve_candidacy`, `scheduling/service.py` |
| REQ-016 | Sin envíos durante preparación | D3 | 1 | P0 | ✅ Implementado y Verificado | `api/approvals.py::prepare_candidacy`, `test_api_v1_endpoints.py` |
| REQ-017 | Edición invalida aprobación | D11 | 1 | P0 | ✅ Implementado y Verificado | `api/approvals.py::modify_candidacy`, `test_api_v1_endpoints.py` |
| REQ-018 | Rechazo/cancelación impide envío | D3 | 1 | P0 | ✅ Implementado y Verificado | `api/approvals.py::reject_candidacy`, `test_api_v1_endpoints.py` |
| REQ-019 | Reinicio sin pérdida de tareas | D3 | 1 | P0 | ✅ Implementado y Verificado | Cola durable en base de datos (`ScheduledTask`, SQLite WAL) |
| REQ-020 | Workers no duplican ejecución | D3 | 1 | P0 | ✅ Implementado y Verificado | `test_phase1_worker_regressions.py::TestAtomicPredispatchAndFencingTransitions` |
| REQ-021 | Recuperación tras caída | D3 | 1 | P0 | ✅ Implementado y Verificado | `worker.py::recover_stale_tasks`, `test_phase1_worker_regressions.py` |
| REQ-022 | Resultado incierto distinguido | D3 | 1 | P0 | ✅ Implementado y Verificado | `result_uncertain` + `requires_intervention` en worker, CRM y tareas |
| REQ-023 | Ventana perdida sin envío inesperado | D3 | 1 | P0 | ✅ Implementado y Verificado | `test_phase1_mvp_vertical.py::test_missed_window_does_not_send_unexpectedly` |
| REQ-024 | LinkedIn modo asistido | D7 | 3 | P1 | 🟡 Asistido / Declaración Manual (Verificado) | Requiere aprobación previa obligatoria; semántica honesta de declaración (`manual_external_submission_declared`, `has_automated_portal_evidence=False`, `manual_attestation`). `test_linkedin_assisted.py` |
| REQ-025 | Autorización específica para crear cuentas | D15 | 3 | P0 | ✅ Implementado y Verificado | `test_security.py::test_account_creation_requires_authorization` |
| REQ-026 | CRM con tabla, Kanban y detalle | D8 | 1 | P1 | ✅ Implementado y Verificado | `api/crm.py`, `CRMEntry`, `CRMEvent`, `test_api_v1_endpoints.py` |
| REQ-027 | Seguimientos con aprobación | D4 | 4 | P1 | ✅ Implementado y Verificado | `test_security.py::test_follow_up_requires_approval` |
| REQ-028 | Reporte por correo y dashboard | D8 | 4 | P1 | 🟡 Simulado (Verificado) | Despacho SMTP seguro simulado, HTML escapado, logs sin PII, deducción de reservas. `test_dashboard_and_email_report.py` |
| REQ-029 | BYOK con presupuesto | D14 | 2 | P0 | ✅ Implementado y Verificado | `test_ai_byok_and_budget.py`, `secrets/ai_service.py` |
| REQ-030 | Horarios y zona horaria configurables | D13 | 1 | P1 | ✅ Implementado y Verificado | Ventanas de ejecución, zona horaria por tenant, intervalos de búsqueda periódica (6h, 12h, 24h, 48h). `core/clock.py`, `scheduling/service.py`, `test_job_search_real.py`. |
| REQ-031 | Operación con IDE cerrado | D8 | 1 | P0 | ✅ Implementado y Verificado | Demonio de fondo autónomo (`DurableExecutionDaemon` en `execution/daemon.py`). Escanea y despacha tareas durables y ejecuta búsquedas periódicas autorizadas sin requerir el IDE ni peticiones HTTP manuales. `test_job_search_real.py::test_periodic_search_and_daemon_autonomous_execution`. |
| REQ-032 | Instalación reproducible | D1 | 5 | P1 | 🔨 Implementado y Verificado localmente | `backend/requirements.lock` fijado, migraciones `alembic upgrade head`, script `install_reproducible.sh` y `test_installation_reproducible.py` |
| REQ-033 | Backups y restauración | D1 | 5 | P1 | 🔨 Implementado y Verificado (Aislamiento completo de archivos/BD, reversión demostrada, política de tareas; Restauración en API/UI ordinaria deshabilitada temporalmente) | Respaldos estrictamente aislados por tenant (`tenant_data.json` y `data/documents/{tenant_id}/`), sin fixtures hack. Validación estricta anti-symlinks y rutas ajenas. Reversión atómica probada ante fallo real tras mutar BD y copiar archivos (exactitud de bytes y cero archivos residuales). Coordinación con workers (bloqueo ante tareas en ejecución) y política de tareas históricas a `cancelled`. Preservación de credenciales de usuario y pista de auditoría append-only. Endpoint ordinario `/restore` retorna HTTP 503 y botón en UI deshabilitado. `tests/integration/test_backup_restore.py` (14/14 pasadas). |

## Requisitos de seguridad

| ID | Requisito | Fase | Prioridad | Estado | Implementación y Evidencia |
|----|-----------|:----:|:---------:|:------:|----------------------------|
| SEC-001 | Autenticación y autorización | 0 | P0 | ✅ Implementado y Verificado | T-S01 (`test_security.py`, JWT, Argon2/Bcrypt) |
| SEC-002 | Aislamiento por tenant | 0 | P0 | ✅ Implementado y Verificado | T-S02 (Todas las tablas con `tenant_id`, queries filtradas, test de spoofing) |
| SEC-003 | Protección de claves y secretos en código | 0 | P0 | ✅ Implementado y Verificado | T-S03 (`scripts/check_secrets.py`, `test_secret_check_regression.py`) |
| SEC-004 | Validación de URLs (anti-SSRF formato) | 0 | P0 | ✅ Implementado y Verificado | T-S04 (`test_url_regression.py`, bloqueo de metadata e IPs privadas) |
| SEC-005 | Protección contra prompt injection | 0 | P0 | ✅ Implementado y Verificado | T-S05 (`test_security.py::TestInjectionDetection`, sanitización activa) |
| SEC-006 | Logs sin secretos ni datos personales | 0 | P0 | ✅ Implementado y Verificado | T-S06 (`test_security.py::TestSecretRedaction`, logs limpios sin emails/PII) |
| SEC-007 | Simulación por defecto | 0 | P0 | ✅ Implementado y Verificado | T-S07 (`test_config.py::test_simulation_mode_default_true`) |
| SEC-008 | Política central de autorización | 0 | P0 | ✅ Implementado y Verificado | T-S08 (`test_security.py::TestAuthorizationPolicy`, desacoplada de IA) |
| SEC-009 | Hash inmutable de aprobación versionado | 0 | P0 | ✅ Implementado y Verificado | T-S09 (`test_approval_hash_regression.py`, verificación pre-ejecución) |
| SEC-010 | Exportación, respaldo y eliminación de datos | 5 | P1 | ✅ Implementado y Verificado | Respaldo atómico con manifest SHA-256 (`test_backup_restore.py`) |

### Criterios explícitos de seguridad para Fase 1 y Fase 3

1. **SSRF a nivel de transporte (Fase 1/3)**:
   - `validate_url` solo verifica formato y rangos obvios antes de la conexión.
   - El cliente de transporte HTTP (e.g. `httpx` custom transport) debe resolver DNS una sola vez, verificar que ninguna IP devuelta sea privada/loopback/link-local/multicast, y conectar fijando la IP del socket (`IP pinning`) conservando cabeceras `Host` y SNI TLS.
   - Toda redirección (códigos 3xx) debe abortar el flujo automático y volver a evaluar la URL destino contra la política completa antes de reanudar.
2. **Inmutabilidad y Prevención de Duplicados en Cola (Fase 1)**:
   - Todo envío requiere un `approval_id` con `content_hash` matching en tiempo de ejecución.
   - `idempotency_key` única por candidatura y fecha de ejecución en `scheduled_tasks`.
   - Bloqueo atómico (`locked_by`, `locked_at`) para evitar doble despacho por múltiples workers.
3. **Modo Simulación Forzado (Fase 1)**:
   - `SIMULATION_MODE=true` activo permanentemente en suites y arranque local sin configuración explícita.
   - Adaptadores de portal solo emiten respuestas sintéticas y registran `[SIMULACIÓN]` en `execution_logs`.

## Requisitos de Graphify

| ID | Requisito | Estado | Evidencia |
|----|-----------|:------:|-----------|
| GRF-001 | Graphify instalado y funcional | ✅ | Versión 0.9.2 sincronizada paquete y skill |
| GRF-002 | Exclusiones de datos sensibles configuradas | ✅ | `tests/unit/test_graphify_exclusions.py` (fixtures sintéticos .env, claves, bases de datos) |
| GRF-003 | Índice del proyecto construido | ✅ | `graphify-out/graph.json` (359 nodos, 375 enlaces, SHA256 `93904fd0...`) |
| GRF-004 | Instrucciones en AGENTS.md | ✅ | Reglas de navegación y comando verificado `graphify update .` |
| GRF-005 | Línea base de métricas registrada | ✅ | `docs/graphify-baseline.json` y `docs/graphify-usage.md` |
| GRF-006 | Comparación en tarea equivalente | 🔨 | En curso durante tareas de desarrollo de Fase 1/2 |

