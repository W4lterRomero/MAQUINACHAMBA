# Máquina de Chamba — Matriz de requisitos

## Convenciones

- **ID**: REQ-NNN
- **Prioridad**: P0 (blocker), P1 (must), P2 (should), P3 (nice to have)
- **Fase**: 0-5
- **Estado**: ⬜ Pendiente | 🔨 En progreso | ✅ Completo | ❌ Bloqueado

## Requisitos funcionales

| ID | Requisito | Decisión | Fase | Prioridad | Estado | Prueba |
|----|-----------|----------|:----:|:---------:|:------:|--------|
| REQ-001 | Perfil profesional configurable sin editar código | D7, D12 | 1 | P1 | ✅ | `api/profile.py`, `test_api_v1_endpoints.py::test_profile_and_cv_api_flow` |
| REQ-002 | Importar CV en PDF/DOCX con revisión humana | D12 | 2 | P1 | ✅ | `test_cv_import_and_verification.py`, `test_cv_import_and_review_ui.py` |
| REQ-003 | Importar GitHub con autorización y permisos mínimos | D12 | 2 | P1 | ⬜ | T-003 |
| REQ-004 | Varios CV maestros especializados | D9 | 2 | P1 | ✅ | `api/profile.py`, `cv/models.py`, `test_api_v1_endpoints.py` |
| REQ-005 | Consistencia entre CV maestros y perfil | D9 | 2 | P1 | ✅ | `test_cv_import_and_review_ui.py`, `api/cv.py` |
| REQ-006 | Perfil de comunicación con cuestionario | D10 | 2 | P1 | ✅ | `test_communication_profile.py`, `api/communication.py` |
| REQ-007 | Descubrir e importar vacantes | D5 | 1 | P1 | ✅ | `api/vacancies.py`, `test_api_v1_endpoints.py` |
| REQ-008 | Deduplicación de vacantes | D5 | 1 | P1 | ✅ | `api/vacancies.py`, `test_api_v1_endpoints.py` |
| REQ-009 | Detección de vacantes cerradas y fraude | D5 | 1 | P2 | ✅ | `test_vacancy_fraud_and_closure.py`, `vacancies/service.py` |
| REQ-010 | Evaluar compatibilidad con justificación | D5 | 1 | P1 | ✅ | `api/evaluations.py`, `test_api_v1_endpoints.py` |
| REQ-011 | Reglas deterministas antes de IA | D14 | 1 | P1 | ✅ | `evaluation/service.py`, `api/evaluations.py` |
| REQ-012 | Adaptar CV sin inventar hechos | D9,D12 | 2 | P1 | ✅ | `test_communication_profile.py`, `test_cv_export_and_documents.py` |
| REQ-013 | Generar documentos PDF/DOCX | D9 | 2 | P1 | ✅ | `cv/generator.py`, `test_cv_export_and_documents.py` |
| REQ-014 | Revisar vacante, CV y mensaje antes de aprobar | D11 | 1 | P0 | ✅ | `api/approvals.py::get_candidacy_for_review`, `test_api_v1_endpoints.py` |
| REQ-015 | Aprobar hoy, ejecutar mañana | D3 | 1 | P0 | ✅ | `api/approvals.py::approve_candidacy`, `scheduling/service.py` |
| REQ-016 | Sin envíos durante preparación | D3 | 1 | P0 | ✅ | `api/approvals.py::prepare_candidacy`, `test_api_v1_endpoints.py` |
| REQ-017 | Edición invalida aprobación | D11 | 1 | P0 | ✅ | `api/approvals.py::modify_candidacy`, `test_api_v1_endpoints.py` |
| REQ-018 | Rechazo/cancelación impide envío | D3 | 1 | P0 | ✅ | `api/approvals.py::reject_candidacy`, `test_api_v1_endpoints.py` |
| REQ-019 | Reinicio sin pérdida de tareas | D3 | 1 | P0 | ✅ | Cola durable en base de datos (`ScheduledTask`, Alembic migrations) |
| REQ-020 | Workers no duplican ejecución | D3 | 1 | P0 | ✅ | `test_phase1_worker_regressions.py::TestAtomicPredispatchAndFencingTransitions` |
| REQ-021 | Recuperación tras caída | D3 | 1 | P0 | ✅ | `worker.py::recover_stale_tasks`, `test_phase1_worker_regressions.py` |
| REQ-022 | Resultado incierto distinguido | D3 | 1 | P0 | ✅ | `result_uncertain` + `requires_intervention` en worker, CRM y tareas |
| REQ-023 | Ventana perdida sin envío inesperado | D3 | 1 | P0 | ✅ | `test_phase1_mvp_vertical.py::test_missed_window_does_not_send_unexpectedly` |
| REQ-024 | LinkedIn modo asistido | D7 | 3 | P1 | ⬜ | T-024 |
| REQ-025 | Autorización específica para crear cuentas | D15 | 3 | P0 | ✅ | `test_security.py::test_account_creation_requires_authorization` |
| REQ-026 | CRM con tabla, Kanban y detalle | D8 | 1 | P1 | ✅ | `api/crm.py`, `CRMEntry`, `CRMEvent`, `test_api_v1_endpoints.py` |
| REQ-027 | Seguimientos con aprobación | D4 | 4 | P1 | ✅ | `test_security.py::test_follow_up_requires_approval` |
| REQ-028 | Reporte por correo y dashboard | D8 | 4 | P1 | ⬜ | T-028 |
| REQ-029 | BYOK con presupuesto | D14 | 2 | P0 | ✅ | `test_ai_byok_and_budget.py`, `secrets/ai_service.py` |
| REQ-030 | Horarios y zona horaria configurables | D13 | 1 | P1 | ✅ | `core/clock.py`, `scheduling/service.py` |
| REQ-031 | Operación con IDE cerrado | D8 | 1 | P0 | ✅ | `api/execution.py`, `worker.py` ejecutable como proceso background |
| REQ-032 | Instalación reproducible | D1 | 5 | P1 | ⬜ | T-032 |

| REQ-033 | Backups y restauración | D1 | 5 | P1 | ⬜ | T-033 |

## Requisitos de seguridad

| ID | Requisito | Fase | Prioridad | Estado | Prueba |
|----|-----------|:----:|:---------:|:------:|--------|
| SEC-001 | Autenticación y autorización | 0 | P0 | ✅ | T-S01 (`test_security.py`) |
| SEC-002 | Aislamiento por tenant | 0 | P0 | ✅ | T-S02 (Base models, queries filtradas) |
| SEC-003 | Protección de claves y secretos en código | 0 | P0 | ✅ | T-S03 (`scripts/check_secrets.py`, `test_secret_check_regression.py`) |
| SEC-004 | Validación de URLs (anti-SSRF formato) | 0 | P0 | ✅ | T-S04 (`test_url_regression.py`) |
| SEC-005 | Protección contra prompt injection | 0 | P0 | ✅ | T-S05 (`test_security.py::TestInjectionDetection`) |
| SEC-006 | Logs sin secretos | 0 | P0 | ✅ | T-S06 (`test_security.py::TestSecretRedaction`) |
| SEC-007 | Simulación por defecto | 0 | P0 | ✅ | T-S07 (`test_config.py::test_simulation_mode_default_true`) |
| SEC-008 | Política central de autorización | 0 | P0 | ✅ | T-S08 (`test_security.py::TestAuthorizationPolicy`) |
| SEC-009 | Hash inmutable de aprobación versionado | 0 | P0 | ✅ | T-S09 (`test_approval_hash_regression.py`) |
| SEC-010 | Exportación y eliminación de datos | 5 | P1 | ⬜ | T-S10 |

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

