# Plan de implementación por fases

## Protocolo de trabajo

Cada fase sigue el mismo ciclo:

1. **Inicio:** leer `CLAUDE.md` y la sección de la fase. Confirmar requisitos previos del owner.
2. **Implementación:** solo el alcance de la fase. Lo que aparezca fuera de alcance se anota en
   "Pendientes detectados" de la fase, no se implementa.
3. **Verificación:** tests + lint + typecheck en verde; criterios de salida cumplidos.
4. **Cierre:** actualizar la tabla de estado y el registro de decisiones si hubo decisiones nuevas.
5. **⛔ STOP:** revisión del owner. La siguiente fase no empieza sin su autorización explícita.

Las fases son pequeñas a propósito: cada una produce algo verificable y se puede revisar en una
sesión. Los modelos y migraciones son incrementales: cada fase crea solo lo que sus flujos necesitan.

## Estado

| Fase | Nombre | Estado | Estimación |
|---|---|---|---|
| 0 | Documentación y fundamentos | ✅ Completada | — |
| 1A | Scaffold del control plane | ✅ Completada — pendiente revisión del owner | 5–8 h |
| 1B | Núcleo de seguridad: política, aprobaciones, presupuesto, idempotencia | ⏳ Pendiente | 10–14 h |
| 2 | Integración GitHub App | ⏳ Pendiente | 15–25 h |
| 3 | Coding worker local (mock → Claude) | ⏳ Pendiente | 20–35 h |
| 4 | Despliegue de Jarvis en producción (Railway) | ⏳ Pendiente — guía recibida ([railway.md](railway.md)) | 8–14 h |
| 5 | WhatsApp Cloud API | ⏳ Pendiente | 12–20 h |
| 6 | Aprobaciones, despliegues de clientes y panel | ⏳ Pendiente | 15–25 h |
| 7 | Gmail | ⏳ Pendiente | 10–18 h |
| 8 | Mantenimiento mensual | ⏳ Pendiente | 15–25 h |
| 9 | Hardening y observabilidad | ⏳ Pendiente | 20–40 h |
| 10 | Autonomía gradual (subir niveles con evidencia) | 🔁 Continuo | — |
| 11 | Rol Constructor: de idea a MVP | ⏳ Pendiente | 40–70 h |
| 12 | Rol Comercial: prospectos, propuestas y contacto | ⏳ Pendiente | 30–50 h |

Roles: **Soporte** (Fases 1–10) → **Constructor** (11) → **Comercial** (12). Cada rol reutiliza lo
construido en el anterior; el orden importa: primero arreglar bien, luego construir bien, luego vender.

Hitos: **prototipo local** al terminar Fase 3 · **MVP de soporte usable con clientes** al terminar
Fase 6 · **operación confiable** tras Fase 9 · **Jarvis constructor** tras Fase 11 · **Jarvis
comercial** tras Fase 12. Las estimaciones no incluyen esperas de aprobación de Meta/Google.

---

## Fase 0 — Documentación y fundamentos

**Entregables:** `README.md`, `CLAUDE.md`, `docs/*.md`, `project_manifests/` (global, cliente y
proyecto de ejemplo), `.env.example`, `.gitignore`.

**Criterio de salida:** arquitectura aprobada por el owner. ✅

---

## Fase 1A — Scaffold del control plane

**Objetivo:** proyecto Django ejecutable localmente, con el dominio mínimo y los manifests como fuente de verdad.

**Alcance**
- `pyproject.toml` con `uv`; dependencias: Django, DRF, psycopg, pydantic, PyYAML, phonenumbers;
  dev: pytest, pytest-django, ruff, mypy, django-stubs, factory-boy.
- `apps/api/` con settings separados (`base`, `dev`, `test`, `prod`) configurados por variables de
  entorno; `prod` sin supuestos de proveedor.
- `compose.yaml`: Postgres + API. `Dockerfile` de la API.
- Modelos y migraciones **solo**: `Client`, `Contact`, `Project`, `ContractPolicy` (materializada,
  con `manifest_hash` y commit de origen), `AuditEvent` (append-only a nivel de aplicación).
- Normalización canónica de contactos (E.164, email conservador) con unicidad por valor normalizado.
- Esquemas pydantic para `global.yaml`, `clients/*.yaml`, `projects/*.yaml` (incluye `ownership` y
  `autonomy_level`; nivel 4 solo con `ownership: internal`); validación de techos globales.
- `manage.py load_manifests` (materializa y audita) y `manage.py check_manifests` (detecta drift).
- Django admin: modelos materializados en **solo lectura**; `AuditEvent` sin edición ni borrado.
- GitHub Actions `ci.yml`: ruff, mypy, pytest con Postgres de servicio.

**Fuera de alcance:** tickets, jobs, política, presupuesto, aprobaciones, cualquier integración externa.

**Criterios de salida**
- `docker compose up` levanta API + DB; `manage.py migrate` limpio.
- `load_manifests` carga los ejemplos; rechaza manifests inválidos o que superen techos globales (tests).
- `check_manifests` detecta una modificación manual en base de datos (test).
- Teléfonos y emails equivalentes se normalizan al mismo valor; valores ambiguos se rechazan (tests).
- CI en verde.

**Resultado (2026-10-01):** criterios cumplidos localmente: 74 tests (unit, integración, seguridad),
cobertura 94 %, ruff + mypy estricto en verde, `docker compose up` sirve `/healthz`. El CI se
ejecutará en GitHub con el primer push.

**Pendientes detectados (para fases posteriores)**
- 1B: validar `restrict` contra el catálogo de acciones; aplicar en runtime los techos de
  `global.yaml`; bloquear acciones no triviales en proyectos con drift (`detect_drift` ya existe).
- 5: los `wa_id` de WhatsApp llegan sin `+`; anteponerlo antes de normalizar.
- 4: servidor WSGI/ASGI de producción, ficheros estáticos del admin y `SECURE_HSTS_PRELOAD`
  (el `Dockerfile` actual usa `runserver`, solo para desarrollo).
- `factory-boy` no se añadió: aún no hace falta.

---

## Fase 1B — Núcleo de seguridad

**Objetivo:** las piezas deterministas que hacen segura la autonomía.

**Alcance**
- Modelos: `InboundEvent`, `Ticket`, `Job`, `JobRun`, `Approval`, `Budget`, `BudgetReservation`,
  `UsageLedger`, `IdempotencyRecord`.
- `PolicyEngine`: `evaluate(actor, action, project) -> Decision`. Deny by default; catálogo de
  acciones con clase de riesgo; nivel de autonomía × clase; acciones críticas siempre con aprobación;
  techo de capacidades por actor en código (el `coding_worker` nunca puede recibir acciones de deploy).
- `ProjectResolver`: contacto normalizado → cliente/proyecto; ambigüedad ⇒ `NeedsIdentification`.
- `ApprovalService`: `action_digest`, single-use (`used_at`), `expires_at`, invalidación por cambio
  de target/commit/artifact/parámetros/política, consumo atómico.
- `BudgetGuard`: reserva simultánea `global → client → project → job` con locking en ese orden fijo,
  `reconcile()`/`release()`, expiración de reservas, umbrales y circuit breaker.
- `UsageLedger` + catálogo de precios versionado (`pricing_version`).
- `LLMGateway` con `FakeLLMProvider` como único punto de acceso a modelos.
- `IdempotencyService` genérico para efectos secundarios (ver architecture.md §10).
- Máquinas de estado de `Ticket` y `Job`; `AuditService.record()`.
- `TaskQueue` + `InProcessQueue`. `IdentityVerifier` + `FakeVerifier` para la API.

**Tests obligatorios (`tests/security/`)**
- Acción `forbidden` nunca se ejecuta; acción desconocida ⇒ `forbidden`; el actor `coding_worker`
  no obtiene acciones de deploy aunque un manifest las liste.
- Cada nivel de autonomía permite exactamente sus clases de riesgo; una acción `critical` requiere
  aprobación incluso en nivel 4; `restrict` del manifest solo endurece; nivel 4 en proyecto de cliente
  es rechazado.
- `requires_approval` falla sin `Approval`; funciona con uno válido; falla si cambia commit, target,
  parámetros o `manifest_hash`; falla si expiró; falla en el segundo uso; un `Approval` de otro
  proyecto no sirve.
- Deploy a producción sin aprobación es imposible cuando el nivel del proyecto no lo cubre (nivel ≤ 2).
- Presupuesto agotado en cualquier scope bloquea el job; el circuit breaker global detiene todo;
  reservas concurrentes no sobrepasan el límite.
- Evento duplicado no crea ticket ni job duplicado; un efecto con la misma idempotency key no se repite.
- Remitente no identificado o ambiguo ⇒ `NeedsIdentification` y ninguna acción sobre el proyecto.
- Un job del proyecto A no puede referenciar recursos del proyecto B.
- Transiciones de estado inválidas se rechazan.

**Fuera de alcance:** proveedores reales (Anthropic, GitHub, Firebase).

**Criterios de salida:** todos los tests de seguridad en verde; cobertura ≥ 90 % en `policies`,
`budgets`, `approvals`, `idempotency`.

---

## Fase 2 — Integración GitHub App

**Requisitos previos del owner**
- Crear GitHub App privada con los permisos de [permissions-and-approvals.md](permissions-and-approvals.md#github-app);
  instalarla **solo** en un repositorio de prueba.
- Configurar en ese repo un Ruleset/branch protection: PR obligatorio, required checks, sin push
  directo a la rama por defecto, **sin bypass para la GitHub App de Jarvis**.
- Túnel de desarrollo (smee.io o cloudflared) para webhooks.

**Alcance**
- Webhook: validación `X-Hub-Signature-256`, dedupe por `X-GitHub-Delivery`, respuesta 202 inmediata,
  procesamiento vía `TaskQueue`.
- Modelo `RepositoryConnection`.
- `RepoBroker` + `GitHubAppHost`: tokens de instalación de **solo lectura** limitados a un repo para
  el worker; push de branch y Draft PR con token propio del broker; idempotencia de branch y PR.
- Guard de rutas protegidas: rechazar cambios en `.github/`, `project_manifests/`, `tests/security/`
  e infraestructura.
- **Preflight de protección:** Jarvis verifica que el repo tiene las reglas exigidas y se niega a
  operar si faltan.
- Eventos: `pull_request`, `check_suite`/`check_run`, `issues` (opcional).

**Criterios de salida:** un job manual crea branch + commit trivial + Draft PR en el repo de prueba;
repetir el job no duplica branch ni PR; el resultado de CI se refleja en el `Ticket`; webhook con
firma inválida ⇒ 401 auditado; repo sin protección ⇒ Jarvis se niega a operar.

---

## Fase 3 — Coding worker local

**Requisitos previos del owner:** decidir credencial de Claude para esta fase (ADR-005).

**Alcance**
- `workers/coder/`: runner que recibe `JobSpec`, clona **solo** el repo del job, ejecuta el agente,
  corre tests del manifest, genera commit y solicita Draft PR al RepoBroker.
- `WorkerExecutor` + `LocalDockerExecutor` aplicando los límites de architecture.md §11
  (CPU, RAM, PIDs, tiempo, workspace, tamaño de archivo, egress, intentos, turnos).
- Comandos del repo (install/test/build) en entorno saneado sin credenciales.
- `CodingAgent` con `MockCodingAgent` (primero, determinista) y luego `ClaudeCodeAgent`.
- Herramientas acotadas (`run_tests`, `run_linter`, `git_diff`, `read_logs`, lectura/edición del
  worktree). Sin Bash irrestricto.
- `JobRun` con coste (`UsageLedger`), turnos, herramientas, archivos cambiados y tests.

**Criterios de salida:** un ticket de bug de prueba produce un Draft PR con fix y tests en verde;
tests de seguridad: un prompt de inyección no logra leer el entorno ni salir del repo; un script del
repo no alcanza la red bloqueada ni supera límites de memoria/PIDs; agotar presupuesto detiene el job;
el worker no posee ninguna credencial de deploy.

**Hito: prototipo local.**

---

## Fase 4 — Despliegue de Jarvis en producción (Railway)

**Guía del owner recibida (2026-10-01) y adaptada en [railway.md](railway.md).** Se sigue en esta
fase, en su orden; no se despliega nada antes.

**Alcance**
- Cambios de código de [railway.md §3](railway.md#3-cambios-de-código-necesarios-antes-del-primer-deploy-fase-4):
  gunicorn, whitenoise, `CSRF_TRUSTED_ORIGINS`, `Dockerfile` de producción, `railway.toml`.
- Proyecto `jarvis-ops` (con permiso del owner), Railway PostgreSQL, servicio `api` desde GitHub,
  pre-deploy con migraciones y `load_manifests`, healthcheck `/healthz`.
- Token de proyecto para toda operación posterior a la creación.

**Decisiones a tomar en esta fase** (se presentan alternativas y consecuencias antes de cambiar la arquitectura):
- Implementación de producción de `TaskQueue` (ADR-003).
- PostgreSQL de producción (ADR-006).
- Ejecución aislada de workers en producción y cómo se cumplen los límites de §11 (ADR-015).
- Servicios, networking, cron/scheduler, volúmenes, secretos, dominios, escalado.
- Observabilidad y destino de logs/auditoría.
- CI/CD de Jarvis hacia Railway (deploy en cada push a `main` y relación con el CI de GitHub).

**Criterio de salida:** flujo de la Fase 3 funcionando en producción con el PC apagado; credenciales
separadas por identidad (`control_plane`, `coding_worker`, `deployer`).

---

## Fase 5 — WhatsApp Cloud API

**Requisitos previos del owner:** cuenta Meta Business, WABA, **número dedicado** a Jarvis (no el
personal; ver ADR-009), app de Meta con `whatsapp_business_messaging`; revisar tarifas vigentes de
Meta (ver [cost-controls.md](cost-controls.md#whatsapp)).

**Alcance**
- Modelos `Conversation`, `Message`.
- `client_agent`: respuestas en lenguaje natural, breves, humanas y sencillas; clasificación de cada
  mensaje por clase de riesgo (enviar directo o borrador para aprobación según nivel).
- **Aviso de resolución:** cuando un ticket llega a producción, Jarvis redacta el aviso al cliente
  ("Listo, ya corregimos…"); en nivel 2 queda para aprobación del owner, en nivel 3+ se envía solo.
- Webhook: verificación de suscripción, validación `X-Hub-Signature-256`, dedupe por message id.
- `WhatsAppCloudProvider` (implementa `MessagingProvider`); estados de entrega vía webhook.
- Identificación por teléfono E.164 exacto; no identificado ⇒ `NeedsIdentification` + alerta.
- `OutboundPolicy` + idempotencia de mensajes salientes; coste por mensaje en `UsageLedger`.
- Escalamiento humano por palabras clave del manifest ⇒ desactiva auto-respuesta y alerta.
- Notificaciones al owner (PR listo, aprobaciones pendientes).
- Rate limit de mensajes salientes con corte automático ante anomalías.

**Criterios de salida:** mensaje de prueba → ticket → acuse → job → Draft PR → aviso al owner →
aviso de resolución al cliente (aprobado o autónomo según nivel); mensaje con intento de inyección no
altera permisos; reentrega del webhook no duplica respuestas; ningún mensaje con precio o plazo sale
sin aprobación.

---

## Fase 6 — Aprobaciones, producción de clientes y panel

**Alcance**
- Modelo `Deployment`; `DeployTarget` por proyecto (mecanismo según hosting de cada cliente).
- `StagingTrigger`: deploy a staging tras CI en verde, con credenciales solo de staging.
- `ProductionTools` + `Deployer`: deploy, rollback, restart, migración con backup; ejecutados solo con
  decisión favorable del `PolicyEngine` (nivel de autonomía o `Approval` consumido).
- Lectura de producción para Jarvis: logs, métricas y errores sanitizados; diseño de lectura acotada
  de datos productivos con minimización de datos personales.
- `ops_agent`: diagnóstico de incidencias de producción que solicita herramientas, sin credenciales.
- Salvaguardas: backup previo, health checks + rollback automático, límite de operaciones por hora,
  alerta al owner por cada acción autónoma en producción.
- Verificar que los workflows de CI de cada proyecto separan tests (sin secretos) de deploy (entorno protegido).
- Panel del owner (Firebase Auth + Hosting, o Django templates — decidir al iniciar): integraciones,
  cola, tickets, presupuesto/uso, proyectos, "Needs You" con aprobar/rechazar, trazabilidad por ticket.

**Criterios de salida:** el owner aprueba un deploy desde el panel; cualquier cambio posterior del
commit invalida la aprobación; en un proyecto de prueba en nivel 3, un deploy fallido se revierte solo;
toda acción queda auditada con su identidad.

**Hito: MVP usable con clientes.**

---

## Fase 7 — Gmail

**Requisitos previos del owner:** cliente OAuth en un proyecto de Google (requerido por Gmail API,
independiente del hosting); aceptar que los scopes restringidos pueden requerir verificación.

**Alcance:** OAuth con `gmail.readonly` + `gmail.send` (`gmail.modify` solo si se justifica);
mecanismo de recepción a decidir al iniciar la fase (push vía Pub/Sub de Google, que exige Gmail, o
sincronización por `history`); `GmailProvider`; misma ruta de intake/política que WhatsApp.

**Criterios de salida:** email de cliente crea ticket y recibe acuse; token revocado genera alerta crítica.

---

## Fase 8 — Mantenimiento mensual

**Alcance:** ejecución programada por proyecto según manifest (mecanismo de cron decidido en Fase 4);
tareas deterministas primero (tests, dependencias, scans, health, backups, errores); findings
normalizados; IA solo donde haga falta interpretación o fix (Batch API cuando aplique); informe
mensual; idempotencia `maint:{project}:{period}:{task}`; modelo `MaintenanceRun`.

**Criterios de salida:** un ciclo completo sobre el repo de prueba genera informe y, si hay finding
reparable, un Draft PR; re-ejecutar el cron no duplica trabajo.

---

## Fase 9 — Hardening y observabilidad

**Alcance:** métricas y alertas (ver [cost-controls.md](cost-controls.md) y
[threat-model.md](threat-model.md)); backoff exponencial y dead-letter; runbooks en `docs/runbooks/`;
tests ampliados (inyección, cross-tenant, replay, TOCTOU de aprobaciones); rollback de deploys;
revisión de credenciales; evaluar almacenamiento externo de auditoría, export, tamper-evident
logging / hash chaining.

**Criterios de salida:** cada alerta crítica tiene runbook; ejercicio de fallo (worker caído, API de
IA caída, presupuesto agotado) documentado y superado.

---

## Fase 10 — Autonomía gradual (continuo)

Medir por proyecto y por clase de riesgo: propuestas aprobadas sin cambios vs. corregidas vs.
rechazadas, incidencias y rollbacks. El panel muestra un **informe de confianza** que sugiere subir
(o bajar) el `autonomy_level`. Subir de nivel es un cambio en el manifest versionado que el owner
revisa, y solo procede con: tasa de éxito demostrada en un periodo definido, tests sólidos, rollback
probado y blast radius aceptable. Las acciones `critical` nunca pasan a autónomas por esta vía.

---

## Fase 11 — Rol Constructor: de idea a MVP

**Objetivo:** "Jarvis, encárgate de este proyecto": Jarvis convierte una idea en un MVP funcionando,
probado y publicado, conversando con el cliente (o con el owner) durante el proceso.

**Requisitos previos:** Fases 1–9 operativas; al menos un proyecto de soporte estable en nivel ≥ 2.
Primer piloto con un **proyecto interno** del owner (p. ej. un sistema de préstamos).

**Alcance**
- Modelos `Initiative`, `Milestone`.
- Intake de idea → especificación (PRD) con preguntas de clarificación → **aprobación del owner**.
- Plantillas de proyecto (stack preferido del owner); creación de repo desde plantilla como acción con
  aprobación; manifest del nuevo proyecto generado y revisado por el owner.
- Plan por milestones; cada milestone: jobs del coding worker → Draft PR → CI → staging.
- **QA:** tests e2e y revisión en navegador sobre staging; informe de QA por milestone.
- Demos: enlace de staging + resumen en lenguaje natural para el cliente/owner.
- Ciclo de feedback por conversación (`client_agent`); cambios de alcance, precio o plazo: aprobación.
- Presupuesto por `Initiative` y por milestone.

**Criterios de salida:** a partir de una idea escrita, Jarvis entrega un MVP interno publicado, con
tests en verde, informe de QA y demo; el owner solo intervino en las aprobaciones definidas.

**Nota:** proyectos con dinero o datos personales (p. ej. préstamos) tienen requisitos legales y de
cumplimiento que el owner debe validar; Jarvis los señala en la especificación, no los resuelve.

---

## Fase 12 — Rol Comercial: prospectos, propuestas y contacto

**Objetivo:** a partir de un producto o MVP, Jarvis encuentra clientes potenciales, prepara
propuestas y demos, y los contacta respetando las reglas de cada canal.

**Requisitos previos del owner:** número de WhatsApp comercial separado del de soporte; dominio/email
de envío configurado para outreach; revisión de normas anti-spam y de protección de datos aplicables.

**Alcance**
- Modelos `Prospect`, `Campaign`, `OutreachMessage`, `ConsentRecord`.
- Definición del cliente ideal → investigación de prospectos con información pública de empresas.
- Propuesta y demo personalizadas (reutiliza staging/demos de la Fase 11).
- **Campañas aprobadas por el owner** (público, plantilla, canal, volumen máximo); mensajes dentro de
  la campaña según nivel de autonomía.
- Canales: email con remitente identificado y baja; WhatsApp solo con consentimiento y número
  comercial; LinkedIn/redes: borradores que envía el owner, publicaciones vía API oficial.
- Seguimiento y conversación con interesados (`client_agent`); traspaso al owner o a la Fase 11.
- Rate limits por canal, presupuesto por campaña, respeto inmediato de bajas.

**Criterios de salida:** una campaña piloto aprobada genera propuestas y contactos sin incumplir las
reglas de canal; las bajas se respetan; cada contacto queda auditado.

---

## Backlog (sin fase asignada)

- Worker local privado para proyectos que no deben ir a la nube.
- `MicrosoftGraphProvider` para clientes con Outlook.
- Edición de políticas desde el panel (requiere ADR de sincronización/versionado).
