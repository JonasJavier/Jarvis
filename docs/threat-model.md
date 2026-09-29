# Modelo de amenazas

## Activos a proteger

| Activo | Impacto si se compromete |
|---|---|
| Código fuente de clientes | Filtración, pérdida de confianza, incumplimiento contractual |
| Secretos (GitHub App key, tokens Meta/Gmail, credencial IA) | Toma de control de integraciones |
| Infraestructura y datos de producción de clientes | Caída de servicio, pérdida de datos |
| Canal de comunicación con clientes (número WhatsApp, email) | Daño reputacional, promesas no autorizadas |
| Presupuesto (IA, cloud, WhatsApp) | Facturas inesperadas |
| Registro de auditoría | Pérdida de trazabilidad |

## Límites de confianza

```
NO CONFIABLE                          CONFIABLE (código determinista)            AISLADO
──────────────                        ───────────────────────────────            ───────
WhatsApp, emails, issues,     ──►     Intake · PolicyEngine · BudgetGuard  ──►   Worker (1 proyecto,
comentarios, archivos del repo,       ApprovalService · Audit                    sin secretos prod,
logs, salida del LLM                  (decide permisos y límites)               herramientas acotadas)
```

**La salida del LLM también es no confiable**: se valida con esquemas antes de actuar sobre ella
y nunca amplía permisos.

## Amenazas y mitigaciones

| # | Amenaza | Vector | Mitigación | Fase |
|---|---|---|---|---|
| T1 | Prompt injection | Mensaje de cliente, email, README, comentario de código, log | Permisos decididos por `PolicyEngine`, no por el LLM; herramientas acotadas; sin secretos en el entorno del agente; salida validada por esquema | 1B, 3 |
| T2 | Exfiltración de secretos | Agente lee env/archivos y los envía | Worker sin secretos prod; token GitHub temporal de 1 repo; red de salida restringida; Outbound Gateway no acepta contenido arbitrario del worker | 3, 4 |
| T3 | Contaminación entre clientes | Job del cliente A accede al repo/contexto de B | Un contenedor por job; clon de un solo repo; contexto construido solo con datos del proyecto; test cross-tenant | 1B, 3 |
| T4 | Webhook falsificado | POST directo al endpoint | Validación HMAC obligatoria; rechazo + auditoría + alerta ante repetidos | 2, 5, 7 |
| T5 | Replay / duplicados | Reenvío de un webhook válido | `unique(source, external_id)`; ventanas de tiempo donde el proveedor lo permita | 1B |
| T6 | Suplantación de cliente | Número/email desconocido o parecido | Identificación solo por coincidencia exacta con `Contact`; desconocido ⇒ sin ejecución | 5, 7 |
| T7 | Agente desactiva su red de seguridad | Modificar `.github/workflows`, políticas, tests de seguridad | GitHub App sin permiso `workflows`; guard de rutas protegidas; CI como árbitro | 2 |
| T8 | Gasto descontrolado | Loop del agente, CI flaky, avalancha de mensajes | `max_turns`, timeout, `max_retries`, BudgetGuard con hard stop, créditos prepagados sin auto-reload, rate limit de entrada/salida | 1B, 3, 5 |
| T9 | Mensaje incorrecto a cliente | LLM promete plazos, precios o cambios de contrato | `OutboundPolicy`: solo categorías seguras en automático; resto como borrador aprobado | 5 |
| T10 | Cambio de código dañino | Fix incorrecto o regresión | Branch + Draft PR + CI + staging; producción solo con `Approval` | 2, 6 |
| T11 | Operación destructiva en DB | Migración o script del agente | Worker sin credenciales de DB prod; migraciones siempre `requires_approval` + backup | 1B, 3 |
| T12 | Robo de credenciales del owner | Token de sesión del panel | Firebase Auth verificado en backend; allowlist del UID del owner; sesiones cortas | 6 |
| T13 | Compromiso del worker | Dependencia maliciosa en el repo del cliente | Contenedor efímero; identidad por job; sin acceso lateral a otras SAs | 3, 4 |
| T14 | Token OAuth revocado/expirado | Cambio en la cuenta Google/Meta | Health checks + alerta inmediata; degradación controlada | 7, 9 |
| T15 | Pérdida de auditoría | Borrado o modificación de `AuditEvent` | Append-only en la aplicación; export a Cloud Logging | 1B, 9 |

## Fallos operativos previsibles

| Fallo | Mitigación |
|---|---|
| Eventos fuera de orden | Timestamps + máquina de estados que rechaza transiciones inválidas |
| Proveedor externo caído (GitHub/Meta/Gmail/Anthropic) | Cola durable + backoff exponencial + dead-letter + alerta |
| Rate limit de Claude | Concurrencia limitada de jobs IA + cola |
| Deploy parcial | Releases reproducibles + rollback documentado |
| Cron ejecutado dos veces | Clave única `(project, period, task)` |
| PC apagado | Control plane en la nube (Fase 4) |

## Invariantes verificadas por tests

Viven en `tests/security/` y no pueden romperse. Lista inicial en la Fase 1B del
[plan de implementación](implementation-plan.md#fase-1b--núcleo-de-seguridad).
