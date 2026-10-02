# Modelo de amenazas

## Activos a proteger

| Activo | Impacto si se compromete |
|---|---|
| Código fuente de clientes | Filtración, pérdida de confianza, incumplimiento contractual |
| Secretos (GitHub App key, tokens Meta/Gmail, credencial IA, credenciales de deploy) | Toma de control de integraciones o despliegues |
| Infraestructura y datos de producción de clientes | Caída de servicio, pérdida de datos |
| Canal de comunicación con clientes (número WhatsApp, email) | Daño reputacional, promesas no autorizadas, bloqueo del número |
| Datos de prospectos (rol Comercial) | Incumplimiento de normas de privacidad y anti-spam |
| Presupuesto de servicios de pago por uso (IA, WhatsApp, APIs) | Facturas inesperadas |
| Políticas (manifests) y registro de auditoría | Autonomía no autorizada, pérdida de trazabilidad |

## Límites de confianza

```
NO CONFIABLE                          CONFIABLE (código determinista)            AISLADO
──────────────                        ───────────────────────────────            ───────
WhatsApp, emails, issues,     ──►     Intake · PolicyEngine · BudgetGuard  ──►   Coding worker
comentarios, archivos del repo,       ApprovalService · RepoBroker · Audit       (1 proyecto, token
logs, salida del LLM,                 (decide permisos y límites;                de solo lectura,
código del repo al ejecutarse         fuente: manifests versionados)             sin deploy, límites)
```

La salida del LLM también es no confiable: se valida con esquemas antes de actuar sobre ella y nunca
amplía permisos. Ejecutar install/test/build de un repo es ejecución de código no confiable.

## Amenazas y mitigaciones

| # | Amenaza | Vector | Mitigación | Fase |
|---|---|---|---|---|
| T1 | Prompt injection | Mensaje de cliente, email, README, comentario, log | Permisos decididos por `PolicyEngine`, no por el LLM; techo de capacidades por actor; herramientas acotadas; sin secretos en el entorno del agente; salida validada | 1B, 3 |
| T2 | Exfiltración de secretos | Agente o script del repo lee el entorno y lo envía fuera | Worker sin secretos de deploy/prod; token GitHub de solo lectura, 1 repo, corta duración; comandos del repo con entorno saneado; egress denegado por defecto con allowlist | 3, 4 |
| T3 | Contaminación entre clientes | Job del cliente A accede al repo/contexto de B | Un workspace por job; clon de un solo repo; contexto solo del proyecto; test cross-tenant | 1B, 3 |
| T4 | Webhook falsificado | POST directo al endpoint | Validación HMAC obligatoria; rechazo + auditoría + alerta ante repetidos | 2, 5, 7 |
| T5 | Duplicados / replay | Reentrega de webhook, job reejecutado, caída a mitad | Entrega at-least-once asumida; `IdempotencyRecord` en todo efecto externo; reconciliación antes de reintentar | 1B |
| T6 | Suplantación o error de identificación | Número/email desconocido, formato distinto, contacto en dos clientes | Normalización canónica (E.164, email conservador) + coincidencia exacta; ambigüedad ⇒ `NeedsIdentification` sin acciones | 1A, 1B, 5, 7 |
| T7 | Agente desactiva su red de seguridad | Modificar `.github/`, manifests, tests de seguridad | GitHub App sin permiso `workflows`; guard de rutas protegidas; Rulesets con PR obligatorio, required checks y sin bypass; preflight que exige la protección | 2 |
| T8 | Gasto descontrolado | Loop del agente, CI flaky, avalancha de mensajes | `max_turns`, timeout, `max_retries`, BudgetGuard multi-scope con hard stop, `UsageLedger`, créditos prepagados sin auto-reload, rate limits | 1B, 3, 5 |
| T9 | Mensaje incorrecto a cliente | LLM promete plazos, precios o cambios de contrato, o anuncia un arreglo que no funciona | Cada mensaje clasificado por clase de riesgo; precios/plazos/contratos siempre `critical`; aviso de resolución solo tras deploy verificado; en nivel 2, borrador aprobado | 5 |
| T10 | Cambio de código dañino | Fix incorrecto o regresión | Branch + Draft PR + CI + staging; producción según nivel de autonomía (o `Approval`) con health checks y rollback automático | 2, 6 |
| T11 | Operación destructiva en DB | Migración o script del agente | Worker sin credenciales de DB; migraciones siempre `requires_approval` | 1B, 3 |
| T12 | Robo de sesión del owner | Token del panel | Verificación del token en backend; allowlist de la identidad del owner; sesiones cortas | 6 |
| T13 | Código no confiable agota o compromete el worker | Dependencia maliciosa, fork bomb, script de install | Workspace efímero; límites de CPU/RAM/PIDs/disco/tamaño de archivo/tiempo; egress restringido; sin acceso a servicios internos | 3, 4 |
| T14 | Token OAuth revocado/expirado | Cambio en la cuenta Google/Meta | Health checks + alerta inmediata; degradación controlada | 7, 9 |
| T15 | Alteración de auditoría | Modificación o borrado de `AuditEvent` | Append-only a nivel de aplicación (sin update/delete en código ni admin). **No** es inmutable criptográficamente; almacenamiento externo / hash chaining evaluados en Fase 9 | 1A, 9 |
| T16 | Obtención indirecta de credenciales de deploy | Código del PR ejecutado en CI con secretos de deploy disponibles | Jobs de CI que ejecutan código del PR sin secretos; deploy en entornos protegidos separados; staging con credenciales solo de staging; worker nunca despliega | 2, 6 |
| T17 | Reutilización o desvío de aprobaciones | Aprobar commit X y desplegar commit Y; reutilizar una aprobación | `action_digest` recalculado antes de ejecutar; single-use con `used_at`; `expires_at`; invalidación por cambio de política | 1B, 6 |
| T18 | Drift de política | Edición directa en Django Admin o base de datos | Manifests versionados como fuente de verdad; modelos materializados de solo lectura; `check_manifests` bloquea ante drift | 1A |
| T19 | Error de Jarvis operando producción | Deploy defectuoso, restart en mal momento, migración errónea | Catálogo cerrado de `ProductionTools` ejecutado por código determinista; niveles de autonomía; acciones críticas siempre aprobadas; backup previo; health checks + rollback automático; límite de operaciones por hora; alerta por cada acción autónoma | 6 |
| T20 | Inyección que escala a producción | Mensaje malicioso induce al `ops_agent` a pedir una operación dañina | El LLM solo solicita; `PolicyEngine` decide por nivel y clase de riesgo; operaciones destructivas `critical`; sin credenciales en el agente | 6 |
| T21 | Bloqueo de número o cuenta por contacto comercial | Mensajes no solicitados por WhatsApp, automatización de DMs en redes | WhatsApp solo con consentimiento y número comercial separado del de soporte; DMs de redes enviados por el owner; rate limits; campañas aprobadas | 12 |
| T22 | Uso indebido de datos de prospectos | Recolección excesiva, sin base legal, sin baja | Datos mínimos de fuentes públicas de empresas; `ConsentRecord`; baja inmediata; retención limitada | 12 |
| T23 | MVP generado con vulnerabilidades | Código del rol Constructor sin revisión de seguridad | Scans de seguridad en CI, QA por milestone, especificación aprobada; proyectos con dinero/datos personales requieren revisión del owner antes de producción | 11 |

## Fallos operativos previsibles

| Fallo | Mitigación |
|---|---|
| Eventos fuera de orden | Timestamps + máquina de estados que rechaza transiciones inválidas |
| Proveedor externo caído (GitHub/Meta/Gmail/Anthropic) | Cola con reintentos + backoff exponencial + dead-letter + alerta |
| Rate limit de Claude | Concurrencia limitada de jobs IA + cola |
| Deadlock en presupuestos | Orden fijo de locking `global → client → project → job` |
| Deploy parcial | Releases reproducibles + rollback documentado |
| Cron ejecutado dos veces | Idempotency key `maint:{project}:{period}:{task}` |
| PC apagado | Control plane en producción (Railway, Fase 4) |

## Invariantes verificadas por tests

Viven en `tests/security/` y no pueden romperse. Lista inicial en las Fases 1A, 1B y 3 del
[plan de implementación](implementation-plan.md).
