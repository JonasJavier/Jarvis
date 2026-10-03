# Arquitectura

## 1. Visión general

Arquitectura **cloud-first**: el plano de control funciona 24/7 independientemente del PC del owner.
**Producción: Railway. Desarrollo local: Docker / docker compose.** Los detalles de producción
(servicios, networking, workers, cron, volúmenes, Postgres, secretos, dominios, escalado,
observabilidad) se deciden en la fase de deployment siguiendo la guía del owner (ver ADR-001).

```
                    ┌──────────────── Fuentes (NO confiables) ────────────────┐
                    │ WhatsApp Cloud API · Gmail API · GitHub App · Scheduler │
                    └──────────────────────────────┬──────────────────────────┘
                                                   ▼
┌──────────────────────────── Control Plane (Django/DRF) ────────────────────────────────┐
│  Webhook Intake ──► InboundEvent (dedupe) ──► Normalizer ──► ProjectResolver            │
│   firma validada,                                    (normalización canónica + exacto)  │
│   respuesta 2xx rápida                                           ▼                       │
│                                                     PolicyEngine (determinista,         │
│                                                     fuente: manifests versionados)      │
│                                                                  ▼                       │
│                                    Ticket ──► Job ──► TaskQueue (abstracción)            │
│  AuditEvent ◄── todo          BudgetGuard + UsageLedger ◄── LLMGateway ◄── AI Triage     │
│  IdempotencyRecord ◄── todo efecto secundario externo                                    │
│  RepoBroker (token de escritura GitHub; push de branch, Draft PR)                        │
│  ApprovalService (action_digest, single-use) ──► Deployer (identidad separada)           │
│  Outbound Gateway ──► WhatsApp / Gmail (OutboundPolicy)                                  │
└───────────────────────────────┬──────────────────────────────────────────────────────────┘
                                ▼  JobSpec
              ┌──────── Coding Worker efímero (WorkerExecutor) ─────────┐
              │ 1 proyecto · token GitHub de solo lectura · sin secretos │
              │ de deploy/prod · límites de CPU/RAM/PIDs/tiempo/disco/red │
              │ clone ► branch ► cambios ► tests ► commit ► Draft PR      │
              │ (push y PR a través del RepoBroker)   ► workspace destruido│
              └─────────────────────────────┬──────────────────────────────┘
                                            ▼
              GitHub (Rulesets: PR obligatorio, required checks, sin bypass)
                                            ▼
              GitHub Actions (árbitro determinista) ► StagingTrigger ► Staging
                                            ▼
     PolicyEngine (nivel de autonomía o Approval) ► Deployer/ProductionTools ► Producción
```

## 2. Actores

Cada acción se evalúa como `(actor, action)`. Las capacidades de cada actor tienen un techo fijado
en código que ningún manifest puede ampliar.

| Actor | Qué puede hacer | Nunca |
|---|---|---|
| `owner` | Aprobar/rechazar, operar el panel | — |
| `control_plane` | Intake, política, presupuesto, colas, RepoBroker, outbound | Desplegar a producción sin `Approval` |
| `coding_worker` | Leer repo, editar worktree, ejecutar tests/lint, commit, solicitar branch/Draft PR al broker | Desplegar (staging o prod), leer secretos, merge, tocar `.github/`/manifests |
| `ci` | Tests, lint, scans, build sobre el PR | Usar credenciales de deploy en jobs que ejecutan código del PR |
| `staging_trigger` | Desplegar a staging un commit con CI en verde | Producción |
| `ops_agent` (LLM) | Diagnosticar producción con lecturas sanitizadas; **solicitar** herramientas de producción | Poseer credenciales; ejecutar nada directamente |
| `client_agent` (LLM) | Conversar con clientes en lenguaje natural; redactar mensajes | Enviar sin pasar por `OutboundPolicy`; comprometer precios/plazos/contratos |
| `deployer` | Ejecutar `ProductionTools` (deploy, rollback, restart, migración con backup) | Actuar sin decisión del `PolicyEngine` (nivel de autonomía o `Approval` consumido) |

### Niveles de autonomía

Cada proyecto declara en su manifest un `autonomy_level` (0–4). Cada acción tiene una **clase de
riesgo** fijada en código. El `PolicyEngine` decide: nivel del proyecto × clase de riesgo, con
prohibiciones y acciones críticas globales que prevalecen. Detalle en
[permissions-and-approvals.md](permissions-and-approvals.md#niveles-de-autonomía).

| Nivel | Jarvis… |
|---|---|
| 0 — Observa | Lee, diagnostica e informa al owner |
| 1 — Propone | Prepara PRs y borradores; el owner aprueba cada acción externa |
| 2 — Actúa en lo seguro | Autónomo en staging y mensajes de bajo riesgo; pide aprobación para producción |
| 3 — Actúa e informa | Autónomo en producción de bajo/medio riesgo con rollback; informa después |
| 4 — Autónomo | Todo salvo acciones críticas; inicialmente solo proyectos internos |

## 3. Componentes

| Componente | Responsabilidad | Usa LLM |
|---|---|---|
| Webhook Intake | Validar firma, persistir `InboundEvent`, deduplicar, responder rápido, encolar | No |
| Normalizer | Convertir payloads de cada canal a un mensaje común | No |
| ProjectResolver | Normalización canónica + coincidencia exacta → `Client`/`Project` (§7) | No |
| PolicyEngine | `(actor, action, project)` → `autonomous` / `requires_approval` / `forbidden` | No |
| BudgetGuard | Reservas multi-scope con locking ordenado; circuit breaker (§8) | No |
| UsageLedger | Registro append-only de consumo real y coste por servicio de pago por uso | No |
| LLMGateway | Único punto de acceso a modelos; aplica BudgetGuard, registra UsageLedger, routing de modelo. Expone el **proxy** `/llm/v1/messages` para el agente del sandbox (ADR-033) | — |
| AI Triage | Clasificar peticiones ambiguas, resumir | Sí (económico) |
| Agent Orchestrator | Construir `JobSpec`, lanzar worker vía `WorkerExecutor`, recoger resultados | No |
| Coding Worker | Diagnosticar y modificar código de un único proyecto | Sí |
| RepoBroker | Emitir tokens de solo lectura por repo; push de branch y Draft PR con token propio | No |
| StagingTrigger | Desplegar a staging tras CI en verde (determinista o vía CI) | No |
| ApprovalService | Crear, aprobar, invalidar y consumir aprobaciones (§9) | No |
| ProductionTools | Catálogo cerrado de operaciones de producción (leer logs/métricas/errores sanitizados, deploy, rollback, restart, migración con backup previo) | No |
| Deployer | Ejecutar `ProductionTools` con identidad separada, solo con decisión favorable del `PolicyEngine` | No |
| Ops Agent | Diagnosticar incidencias de producción y solicitar herramientas | Sí |
| Client Agent | Conversación con clientes: acuses, preguntas, avisos de resolución en lenguaje natural | Sí |
| Outbound Gateway | Enviar mensajes aplicando `OutboundPolicy` + nivel de autonomía + idempotencia | No |
| Audit | `AuditEvent` append-only a nivel de aplicación | No |

## 4. Interfaces (adapters)

Todo lo externo —incluido el proveedor de infraestructura— está detrás de un `Protocol` con una
implementación fake/local. La lógica de dominio no conoce Railway.

```python
class TaskQueue(Protocol):          # InProcessQueue (dev/tests) · producción: PENDIENTE (ADR-003)
    def enqueue(self, job_id: str, *, idempotency_key: str, delay_s: int = 0) -> None: ...

class WorkerExecutor(Protocol):     # LocalDockerExecutor (dev) · producción: PENDIENTE (ADR-015)
    def launch(self, spec: JobSpec, *, limits: WorkerLimits, idempotency_key: str) -> RunHandle: ...
    def cancel(self, handle: RunHandle) -> None: ...

class LLMProvider(Protocol):        # FakeLLMProvider · AnthropicProvider (Fase 3)
    def estimate_usage(self, request: LLMRequest, model: str) -> LLMUsage: ...  # el gateway lo tarifica
    def invoke(self, request: LLMRequest) -> LLMResult: ...   # LLMResult incluye usage detallado

class CodingAgent(Protocol):        # MockCodingAgent · ClaudeCodeAgent · ClaudePlatformAgent
    def execute(self, spec: JobSpec) -> AgentResult: ...

class RepoHost(Protocol):           # FakeRepoHost · GitHubAppHost (usado solo por RepoBroker)
    def read_only_token(self, repo: str) -> ScopedToken: ...
    def push_branch(self, repo: str, branch: str, changes: ChangeSet) -> str: ...   # ADR-031
    def find_open_pull_request(self, repo: str, branch: str) -> PullRequestRef | None: ...
    def open_draft_pull_request(self, repo, branch, base, title, body) -> PullRequestRef: ...
    def protection(self, repo: str, branch: str) -> ProtectionStatus: ...         # preflight

class MessagingProvider(Protocol):  # FakeMessaging · WhatsAppCloudProvider
    def send(self, msg: OutboundMessage, *, idempotency_key: str) -> str: ...

class MailProvider(Protocol):       # FakeMail · GmailProvider · (futuro) MicrosoftGraphProvider
    def fetch_message(self, external_id: str) -> InboundMail: ...
    def send(self, msg: OutboundMessage, *, idempotency_key: str) -> str: ...

class DeployTarget(Protocol):       # FakeDeployTarget · implementación según cada proyecto (Fase 6)
    def deploy(self, project: str, env: str, ref: str, *, idempotency_key: str) -> DeploymentRef: ...

class IdentityVerifier(Protocol):   # FakeVerifier · FirebaseVerifier (Fase 6, opcional)
    def verify(self, id_token: str) -> VerifiedIdentity: ...
```

Secretos: la aplicación los lee de la configuración del entorno. En desarrollo, `.env` local no
versionado. El mecanismo de producción se define en la fase de deployment.

## 5. Fuente de verdad de la política

Para v1, **los manifests versionados en `project_manifests/` son la fuente de verdad** de todo lo
relacionado con seguridad, autonomía, límites, contrato y acciones permitidas/aprobables/prohibidas.

```
project_manifests/
  global.yaml               techos globales: budgets, límites del worker, acciones prohibidas, TTL de aprobaciones
  clients/<client_id>.yaml  cliente, contactos, budget del cliente
  projects/<project_id>.yaml proyecto, contrato, política, budget, agente, comunicaciones
```

- `load_manifests` valida con esquema estricto y **materializa** en PostgreSQL (`Client`, `Contact`,
  `Project`, `ContractPolicy`), guardando hashes y commit de origen. La validación es todo-o-nada:
  un error en cualquier archivo rechaza el conjunto. Las claves YAML duplicadas se rechazan.
- `global.yaml` no tiene tabla propia: se valida en cada carga y su hash entra en el
  `manifest_hash` efectivo de cada `ContractPolicy` (global + cliente + proyecto), que es el usado en
  los `action_digest`. Los hashes ignoran formato y comentarios.
- Un cliente o proyecto que desaparece de los manifests se **desactiva** (no se borra); los contactos
  de un cliente desactivado se eliminan para que no pueda ser identificado. La auditoría registra
  conteos de contactos, nunca sus valores.
- Un proyecto nunca puede superar los techos de `global.yaml`; un valor fuera de rango invalida el manifest.
- Los modelos materializados son **solo lectura en Django Admin**. `check_manifests` detecta drift
  entre archivos y base de datos; con drift, el proyecto queda bloqueado para acciones no triviales
  hasta recargar.
- Editar políticas desde el panel requerirá una ADR explícita sobre sincronización y versionado.
- Una tarea de código nunca modifica `project_manifests/` (ruta protegida).

## 6. Modelo de datos (incremental por fase)

Cada fase crea solo los modelos que sus flujos necesitan. Las relaciones de fases futuras son
orientativas y se revisan al llegar a esa fase.

| Fase | Modelos |
|---|---|
| 1A | `Client`, `Contact`, `Project`, `ContractPolicy` (materializada), `AuditEvent` |
| 1B | `GlobalPolicy` (materializada), `InboundEvent`, `Ticket`, `Job`, `JobRun`, `Approval`, `Budget`, `BudgetReservation`, `UsageLedger`, `IdempotencyRecord` |
| 2 | `RepositoryConnection` (instalación GitHub ↔ repo del proyecto) |
| 3 | `Artifact` (diff, logs, reporte) |
| 5 | `Conversation`, `Message` |
| 6 | `Deployment` |
| 8 | `MaintenanceRun` |
| 11 | `Initiative` (idea → MVP), `Milestone` |
| 12 | `Prospect`, `Campaign`, `OutreachMessage`, `ConsentRecord` |

```
Client 1──* Contact (valor normalizado, único por tipo)
Client 1──* Project 1──1 ContractPolicy (manifest_hash)
Project 1──* Ticket 1──* Job 1──* JobRun unique(job, attempt)
InboundEvent unique(source, external_id) ──► Ticket
Approval (project, action, target, subject_ref, params, action_digest, expires_at, used_at)
Budget (scope, scope_ref, period, limit_usd, spent_usd, reserved_usd)
BudgetReservation (job_run, budget, amount, status)
UsageLedger (provider, service, model, tokens…, cost_usd, pricing_version, client, project, job, created_at)
IdempotencyRecord unique(key) (operation, request_hash, status, result_ref)
AuditEvent (actor, action, target, project, correlation_id, payload, created_at)
```

**AuditEvent** es append-only **a nivel de aplicación** (sin update/delete desde el código ni el
admin). No es criptográficamente inmutable. Quedan como opciones futuras: almacenamiento externo,
logging centralizado, export, tamper-evident logging y hash chaining (Fase 9 / deployment).

## 7. Identificación de cliente y proyecto

1. **Normalización canónica segura** del remitente:
   - Teléfonos → **E.164** con una librería de parsing (sin heurísticas propias).
   - Emails → normalización conservadora: trim, Unicode NFC, minúsculas. **No** se eliminan puntos ni
     sufijos `+tag`, ni se resuelven alias.
   - Valores no normalizables ⇒ no identificados.
2. **Coincidencia exacta** del valor normalizado contra `Contact` (único por tipo).
3. Proyecto: si el cliente tiene un único proyecto activo, se asigna; si tiene varios, el proyecto debe
   determinarse de forma inequívoca (repo del evento GitHub, referencia explícita) — si no, `NeedsIdentification`.
4. Sin identificación inequívoca ⇒ `NeedsIdentification`, alerta al owner y **ninguna acción sobre
   repositorios o infraestructura del cliente**. Se permite como máximo un acuse genérico sin datos del proyecto.

Nunca se usa fuzzy matching ni un LLM para autorizar acciones sobre un proyecto.

## 8. Presupuesto y uso

- Una reserva afecta simultáneamente a todos los scopes aplicables: **global → client → project → job**.
- **Orden fijo de locking:** `global → client → project → job` (`select_for_update` en ese orden,
  dentro de una transacción). Si cualquier scope no tiene saldo, la reserva completa falla.
- `reconcile()` registra el coste real en `UsageLedger`, ajusta `spent_usd` y libera la diferencia.
  Reservas sin conciliar expiran y se liberan.
- Aplica a servicios de **pago por uso**: IA, WhatsApp y APIs externas con coste. Los recursos de
  cómputo de Railway no se presupuestan aquí; se limitan por seguridad y estabilidad (§11).
- Detalle en [cost-controls.md](cost-controls.md).

## 9. Aprobaciones

- `action_digest = sha256(JSON canónico de {project_id, action, target/environment, subject_ref
  (commit SHA, artifact digest o hash del texto del mensaje), params relevantes, manifest_hash})`.
- Solo se crea una aprobación cuando el `PolicyEngine` devuelve `requires_approval` (por nivel de
  autonomía insuficiente o por acción crítica).
- **Single-use:** se consume atómicamente (`used_at`) al iniciar la ejecución y queda ligada a la
  idempotency key de esa ejecución. Una redelivery de la misma ejecución continúa; una ejecución
  nueva requiere aprobación nueva.
- **`expires_at`** obligatorio (TTL en `global.yaml`).
- Antes de ejecutar, el código recalcula el digest de la operación real; si no coincide (cambió
  target, commit, artifact, parámetros o política) la aprobación es **inválida**.
- Reglas completas en [permissions-and-approvals.md](permissions-and-approvals.md).

## 10. Idempotencia

Todos los handlers asumen entrega **at-least-once**: un evento o job puede llegar o ejecutarse más de
una vez. Cada efecto secundario externo usa un `IdempotencyRecord` (`started` → `succeeded`/`failed`).

| Efecto | Idempotency key |
|---|---|
| Evento entrante | `(source, external_id)` |
| Creación de ticket | `ticket:{source}:{external_id}` |
| Creación de job | `job:{ticket_id}:{purpose}` |
| Lanzamiento de worker | `launch:{job_id}:{attempt}` (+ `unique(job, attempt)`) |
| Branch | nombre determinista `jarvis/{ticket_id}-{job_id}-{correlation_id[:8]}` (único entre bases de datos); si existe, se reutiliza |
| Draft PR | `pr:{repo}:{branch}`; se consulta PR abierto antes de crear |
| Mensaje outbound | `out:{conversation}:{ticket}:{purpose}:{seq}` |
| Deploy a staging | `deploy:staging:{project}:{commit_sha}` |
| Deploy a producción | `deploy:prod:{project}:{action_digest}` |
| Mantenimiento | `maint:{project}:{period}:{task}` |

Si un efecto quedó en `started` sin resultado (caída a mitad), se **reconcilia** con el proveedor
(¿existe la branch/PR/mensaje?) antes de reintentar. Para mensajes a clientes se prefiere no
reenviar ante la duda: se reconcilia o se escala al owner.

## 11. Coding worker: aislamiento y límites

Ejecutar instalaciones, tests, builds o scripts de un repositorio es **ejecución de código no
confiable**. Los límites existen por seguridad y estabilidad, no para ahorrar recursos.

| Límite | Valor inicial | Techo (`global.yaml`) |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GiB (OOM kill) | 8 GiB |
| PIDs / procesos | 512 | 1024 |
| Tiempo de ejecución | 40 min | 90 min |
| Workspace (disco efímero) | 10 GiB, destruido al terminar | 20 GiB |
| Tamaño de archivo escrito | 50 MiB | 100 MiB |
| Salida capturada (logs) | 10 MiB, truncado | — |
| Intentos (`max_retries`) | 1 | 2 |
| Turnos del agente (`max_turns`) | 12 | 30 |
| Coste IA por run | según manifest | `global.yaml` |

**Red (egress):** denegado por defecto. Allowlist: GitHub (clone con token de solo lectura), endpoint
del modelo, registries de paquetes solo durante la instalación, API del RepoBroker. Bloqueados: rangos
privados, endpoints de metadata, base de datos y servicios internos de Jarvis.

**Credenciales en el worker:** solo un token GitHub de **solo lectura**, de un repositorio y de corta
duración, más el acceso al modelo. Push de branch y Draft PR se hacen a través del RepoBroker. Los
comandos del repositorio (install/test/build) se ejecutan con un entorno saneado, sin ninguna credencial.
Sin credenciales de deploy, producción, base de datos, WhatsApp ni Gmail.

**Ejecución:** `WorkerExecutor` es una abstracción. Desarrollo: `LocalDockerExecutor` aplicando estos
límites en dos fases (`prepare` con red para instalar dependencias, `work` sin red para el agente y
los tests), con el clon hecho por el control plane y **ninguna credencial dentro del contenedor**
(ADR-032). Producción: **pendiente** hasta la guía de deployment (ADR-015). No se depende de
características específicas de ningún proveedor de nube.

## 12. JobSpec (contrato worker ↔ control plane)

```json
{
  "job_id": "job_01HXYZ",
  "attempt": 1,
  "correlation_id": "corr_...",
  "client_id": "acme",
  "project_id": "reservas",
  "repository": "my-org/reservas",
  "base_ref": "main@<sha>",
  "task": "Investigate payment registration error",
  "risk": "medium",
  "allowed_actions": ["read_repository", "read_sanitized_logs", "run_tests", "run_linter", "modify_worktree", "commit", "request_draft_pr"],
  "limits": { "max_turns": 12, "timeout_seconds": 2400, "max_ai_cost_usd": 3.00, "max_retries": 1,
              "cpu": 2, "memory_mib": 4096, "pids": 512, "workspace_gib": 10 }
}
```

`allowed_actions` es la intersección entre la política del proyecto y el techo de capacidades del
actor `coding_worker`. El contexto para el modelo se arma con política global + del cliente + del
proyecto + ticket + archivos relevantes; nunca con datos de otros clientes.

## 13. Staging y producción

- **Coding worker:** termina en el Draft PR. Nunca despliega.
- **CI:** los jobs que ejecutan código del PR no tienen credenciales de deploy. El deploy vive en
  jobs/entornos separados y protegidos (p. ej. GitHub Environments con reglas de protección).
- **Staging:** lo inicia `StagingTrigger` (determinista) o CI tras checks en verde, con credenciales
  solo de staging. Staging no contiene datos ni credenciales reales de producción.
- **Producción:** solo `Deployer`, ejecutando `ProductionTools`, cuando el `PolicyEngine` lo permite:
  autónomo si el nivel del proyecto cubre la clase de riesgo de la acción, o con `Approval` consumido
  para el `action_digest` exacto. Acciones críticas: siempre con `Approval`.
- **Lectura de producción:** logs, métricas y errores accesibles a Jarvis desde el inicio, siempre
  sanitizados (sin secretos, datos personales minimizados). Lectura de datos de la base productiva:
  solo vía herramientas acotadas y de solo lectura (diseño en Fase 6).
- **Salvaguardas:** backup antes de operaciones de riesgo, rollback automático si fallan los health
  checks tras un deploy, ventana de verificación post-deploy.

## 14. Máquinas de estado

**Ticket**

```
Received → Identified → Classified ─┬→ SupportOnly → ReplyDraft → Sent → Closed
   │                                ├→ CodeTask → ContractCheck ─┬→ Investigating → (Job) → PullRequest
   └→ NeedsIdentification           │                            └→ NeedsQuote → WaitingOwner
                                    ├→ FeatureRequest → NeedsQuote → WaitingOwner
                                    └→ NeedsClarification → WaitingClient
PullRequest → CI → Staging → [WaitingApproval] → Production → ResolutionNotice → ClientNotified → Closed
Cualquier estado → Escalated (humano) · Failed
```

`WaitingApproval` solo aparece si el nivel de autonomía no cubre el deploy. `ResolutionNotice`: el
`client_agent` redacta un aviso breve, humano y sencillo ("Listo, ya corregimos el error al
registrar pagos; puedes intentarlo de nuevo"); se envía directamente o tras aprobación según el nivel.

**Job**

```
Pending → BudgetCheck ─┬→ Queued → Running ─┬→ Succeeded
                       └→ BlockedBudget      ├→ Failed → (retry ≤ max_retries) → Queued
                                             ├→ TimedOut
                                             └→ Cancelled
```

Las transiciones se validan en código; una transición inválida lanza excepción y se audita.

## 15. Trazabilidad

Cada ejecución lleva un `correlation_id` que encadena:

```
external_message_id → inbound_event → ticket → job → job_run → branch → commit → PR → deployment
```

Desde un ticket debe poder reconstruirse: quién pidió, qué mensaje, qué proyecto, qué política
(`manifest_hash`) autorizó, qué modelo y cuánto costó (`UsageLedger`), qué herramientas y archivos,
qué tests, qué PR, quién aprobó (`action_digest`) y qué se respondió al cliente. **No** se guarda el
razonamiento interno del modelo. Logs estructurados con `correlation_id`; su destino en producción
se decide en la fase de deployment.

## 16. Entornos

| Entorno | Estado |
|---|---|
| Local | `docker compose` (API + Postgres en el puerto 55432 del host), `InProcessQueue`, `LocalDockerExecutor`, `.env` local no versionado |
| Producción | **Railway.** Servicios, networking, workers, cron, volúmenes, Postgres, secretos, dominios, escalado y observabilidad: **pendientes** de la guía de deployment del owner |

Identidades lógicas separadas en cualquier entorno: `control_plane`, `coding_worker`, `deployer`,
cada una con su propio conjunto mínimo de credenciales.

## 17. Estructura del repositorio (objetivo)

Se crea **incrementalmente** según la fase; no se generan carpetas vacías por adelantado.

```
apps/api/            Django: config, clients, projects, policies, audit (1A); tickets, jobs,
                     budgets, approvals, idempotency (1B); integrations (2+)
workers/coder/       runner, job_spec, sandbox, prompts (3)
integrations/        adapters: anthropic, github, whatsapp, gmail, firebase
project_manifests/   global.yaml · clients/ · projects/
infra/               (fase de deployment, según guía del owner)
frontend/dashboard/  (Fase 6) panel del owner
docs/                documentación
tests/               unit · integration · security
```

## 18. Rol Constructor (Fase 11)

Convierte una idea en un MVP reutilizando las piezas del rol Soporte (jobs, coding worker, CI,
staging, `ProductionTools`, `client_agent`).

```
Idea (owner o cliente) → Initiative → especificación (PRD) ──[Approval del owner]──►
plan de milestones → por cada milestone: jobs del coding worker → Draft PR → CI → staging
→ QA (tests e2e + revisión en navegador sobre staging) → demo al cliente/owner
→ feedback por conversación → siguiente milestone → producción (según nivel)
```

- Creación del repositorio: desde una plantilla, como acción que requiere aprobación (la GitHub App
  no recibe permisos de administración de la organización).
- Cada milestone tiene presupuesto propio dentro del presupuesto de la `Initiative`.
- La conversación con el cliente (requisitos, dudas, demos) la lleva el `client_agent`; cambios de
  alcance, precios y plazos requieren aprobación.
- Proyectos internos del owner (`ownership: internal`) pueden operar con nivel de autonomía alto
  antes que los de clientes.

## 19. Rol Comercial (Fase 12)

Busca y contacta clientes potenciales para un producto o servicio.

```
Producto/MVP → definición del cliente ideal → investigación de prospectos (información pública de
empresas) → propuesta + demo personalizada → contacto por canal permitido → seguimiento →
conversación con interesados → entrega al owner o al rol Constructor
```

Reglas de canal (detalle en [permissions-and-approvals.md](permissions-and-approvals.md#contacto-comercial)):

- **WhatsApp:** solo con personas que dieron su consentimiento (opt-in), desde un número comercial
  dedicado, nunca el número de soporte de clientes ni un número personal.
- **Email:** primer contacto permitido cumpliendo normas anti-spam (identificación del remitente,
  opción de baja, volumen limitado).
- **LinkedIn / redes:** Jarvis redacta mensajes y publicaciones; el owner envía los mensajes
  privados. Solo se automatiza lo que permitan las APIs oficiales (p. ej. publicaciones).
- Datos de prospectos: mínimos, de fuentes públicas, con baja respetada y retención limitada.
