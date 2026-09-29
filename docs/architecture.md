# Arquitectura

## 1. Visión general

Arquitectura **híbrida cloud-first**: el plano de control vive en la nube (disponible 24/7 aunque
el PC esté apagado), el trabajo pesado corre en workers efímeros, y opcionalmente un worker local
procesa proyectos que no deban salir de tu equipo.

```
                    ┌───────────────── Fuentes (NO confiables) ─────────────────┐
                    │ WhatsApp Cloud API · Gmail (Pub/Sub) · GitHub App · Cron │
                    └──────────────────────────────┬────────────────────────────┘
                                                   ▼
┌──────────────────────── Control Plane (Django/DRF · Cloud Run) ────────────────────────┐
│  Webhook Intake ──► InboundEvent (dedupe) ──► Normalizer ──► ProjectResolver            │
│        │                                                         │                       │
│        ▼                                                         ▼                       │
│  firma validada                                          PolicyEngine (determinista)     │
│  respuesta 2xx rápida                                            │                       │
│                                                                  ▼                       │
│                                              Ticket ──► Job ──► TaskQueue                │
│  AuditEvent ◄── todo                                             │                       │
│  BudgetGuard ◄── LLMGateway ◄── AI Triage (modelo económico, opcional)                   │
│  Approval Gate ──► Deployer (identidad separada)                                         │
│  Outbound Gateway ──► WhatsApp / Gmail (con OutboundPolicy)                              │
└──────────────────────────────────────────────────┬──────────────────────────────────────┘
                                                   ▼
                          ┌──────── Worker efímero (Cloud Run Job / Docker local) ────────┐
                          │ JobSpec · 1 repo · token GitHub temporal · sin secretos prod  │
                          │ CodingAgent (Claude) · herramientas acotadas · límites duros  │
                          │ ► branch ► cambios ► tests ► Draft PR ► contenedor destruido  │
                          └──────────────────────────────┬────────────────────────────────┘
                                                         ▼
                                    GitHub Actions (árbitro determinista) ► Staging
                                                         ▼
                                           Approval del owner ► Producción
```

## 2. Componentes

| Componente | Responsabilidad | Usa LLM |
|---|---|---|
| Webhook Intake | Validar firma, persistir `InboundEvent`, deduplicar, responder rápido, encolar | No |
| Normalizer | Convertir payloads de cada canal a un `Message` común | No |
| ProjectResolver | `teléfono/email/repo -> Client/Project` por coincidencia **exacta** | No |
| PolicyEngine | Decidir si una acción es `autonomous`, `requires_approval` o `forbidden` | No |
| BudgetGuard | Reservar/conciliar gasto; circuit breaker global/cliente/job | No |
| LLMGateway | Único punto de acceso a modelos; aplica BudgetGuard y routing de modelo | — |
| AI Triage | Clasificar peticiones ambiguas (bug/feature/soporte), resumir | Sí (económico) |
| Agent Orchestrator | Construir `JobSpec`, lanzar worker, recoger resultados | No |
| Coding Worker | Diagnosticar y modificar código en sandbox de un único proyecto | Sí |
| Approval Gate | Registrar y exigir aprobaciones del owner | No |
| Outbound Gateway | Enviar mensajes a clientes aplicando `OutboundPolicy` | No (redacción sí) |
| Audit | Registro inmutable de acciones, decisiones y evidencias | No |

## 3. Interfaces (adapters)

Todo lo externo se implementa detrás de un `Protocol` con una implementación **fake** para tests.
Esto permite desarrollar Fases 1–3 sin credenciales reales.

```python
class TaskQueue(Protocol):          # InProcessQueue (dev/tests) · CloudTasksQueue (prod)
    def enqueue(self, job_id: str, *, delay_s: int = 0) -> None: ...

class LLMProvider(Protocol):        # FakeLLM · AnthropicProvider
    def estimate_cost(self, request: LLMRequest) -> Decimal: ...
    def invoke(self, request: LLMRequest) -> LLMResult: ...

class CodingAgent(Protocol):        # MockCodingAgent · ClaudeCodeAgent · ClaudePlatformAgent
    def execute(self, spec: JobSpec) -> AgentResult: ...

class RepoHost(Protocol):           # FakeRepoHost · GitHubAppHost
    def create_branch(...) -> str: ...
    def open_draft_pr(...) -> PullRequestRef: ...

class MessagingProvider(Protocol):  # FakeMessaging · WhatsAppCloudProvider
    def send(self, msg: OutboundMessage) -> str: ...

class MailProvider(Protocol):       # FakeMail · GmailProvider · (futuro) MicrosoftGraphProvider
    def fetch_message(self, external_id: str) -> Message: ...
    def send(self, msg: OutboundMessage) -> str: ...
    def create_draft(self, msg: OutboundMessage) -> str: ...

class IdentityVerifier(Protocol):   # FakeVerifier · FirebaseVerifier
    def verify(self, id_token: str) -> VerifiedIdentity: ...
```

## 4. Modelo de datos

```
Client 1──* Project 1──1 ContractPolicy
  │           │ 1──* MaintenancePlan
  │           │ 1──* IntegrationConnection (repo, canal, etc.)
  │           │
  │ 1──* Contact (teléfono/email verificados -> identificación exacta)
  │
  └ 1──* Conversation 1──* Message ──(origen)── InboundEvent
                              │
Project 1──* Ticket *──1 Conversation (opcional)
               │ 1──* Job 1──* JobRun 1──* Artifact (diff, logs, reporte)
               │                  │
               │                  └── BudgetReservation (*──1 Budget)
               │ 1──* Approval
               └ 1──* Deployment

AuditEvent: (actor, action, target, project, correlation_id, payload, created_at) — append-only
Budget: scope ∈ {global, client, project, job} · period ∈ {day, month, run} · limit_usd · spent_usd
```

Notas de diseño:

- `InboundEvent` tiene `unique(source, external_id)` → idempotencia a nivel de base de datos.
- `ContractPolicy` es **datos**, no texto libre: qué está incluido/excluido en mantenimiento, SLA,
  nivel de autonomía por acción. Se carga desde el manifest del proyecto (`project_manifests/`).
- `Contact` evita la identificación difusa: si el remitente no coincide exactamente, el ticket
  queda en `NeedsIdentification` y **no se ejecuta nada** sobre ningún repositorio.
- `AuditEvent` es append-only (sin update/delete desde la aplicación).
- `MaintenancePlan` tiene clave única `(project, period, task)` para evitar ejecuciones dobles.

## 5. Máquinas de estado

**Ticket**

```
Received → Identified → Classified ─┬→ SupportOnly → ReplyDraft → Sent → Closed
   │                                ├→ CodeTask → ContractCheck ─┬→ Investigating → (Job) → PullRequest
   └→ NeedsIdentification           │                            └→ NeedsQuote → WaitingOwner
                                    ├→ FeatureRequest → NeedsQuote → WaitingOwner
                                    └→ NeedsClarification → WaitingClient
PullRequest → CI → Staging → WaitingApproval → Production → ClientNotified → Closed
Cualquier estado → Escalated (humano) · Failed
```

**Job**

```
Pending → BudgetCheck ─┬→ Queued → Running ─┬→ Succeeded
                       └→ BlockedBudget      ├→ Failed → (retry ≤ max_retries) → Queued
                                             ├→ TimedOut
                                             └→ Cancelled
```

Las transiciones se validan en código; una transición inválida lanza excepción y se audita.

## 6. JobSpec (contrato worker ↔ control plane)

```json
{
  "job_id": "job_01HXYZ",
  "correlation_id": "corr_...",
  "client_id": "acme",
  "project_id": "reservas",
  "repository": "my-org/reservas",
  "task": "Investigate payment registration error",
  "risk": "medium",
  "allowed_actions": ["read_repository", "read_sanitized_logs", "run_tests", "edit_worktree", "create_draft_pr"],
  "denied_actions": ["deploy_production", "read_production_secrets", "run_database_writes", "modify_iam", "modify_ci"],
  "limits": { "max_turns": 12, "timeout_seconds": 2400, "max_ai_cost_usd": 3.00, "max_retries": 1 }
}
```

El worker recibe **solo** el JobSpec y un token GitHub temporal limitado al repositorio del job.
El contexto para el modelo se arma con: política global + política del cliente + contexto del
proyecto + ticket + archivos relevantes. Nunca el contexto de otros clientes.

## 7. Trazabilidad

Cada ejecución lleva un `correlation_id` que encadena:

```
external_message_id → inbound_event → ticket → job → job_run → branch → commit → PR → deployment
```

Desde un ticket debe poder reconstruirse: quién pidió, qué mensaje, qué proyecto, qué política
autorizó, qué modelo y cuánto costó, qué herramientas y archivos, qué tests, qué PR, quién aprobó
y qué se respondió al cliente. **No** se guarda el razonamiento interno del modelo.

## 8. Despliegue

| Entorno | API | Cola | Workers | DB | Secretos |
|---|---|---|---|---|---|
| Local (Fases 1–3) | `docker compose` | `InProcessQueue` | Docker local | Postgres en compose | `.env` local (no versionado) |
| Cloud (Fase 4+) | Cloud Run (min 0) | Cloud Tasks | Cloud Run Jobs | Postgres gestionado (ver ADR-006) | Secret Manager |

Identidades de servicio separadas (Fase 4+):

- `jarvis-api`: canales (WhatsApp/Gmail), orquestación. Sin acceso a deploy.
- `jarvis-coder`: solo token GitHub vía broker + credencial de IA. Sin WhatsApp/Gmail/DB prod.
- `jarvis-deployer`: solo permisos de deploy; se usa únicamente tras un `Approval`.

## 9. Estructura del repositorio (objetivo)

Se crea **incrementalmente** según la fase; no se generan carpetas vacías por adelantado.

```
apps/api/            Django: config, clients, projects, contracts, conversations, tickets,
                     jobs, policies, budgets, approvals, audit, integrations
workers/coder/       runner, job_spec, sandbox, prompts
integrations/        adapters: anthropic, github, whatsapp, gmail, firebase
project_manifests/   un YAML por proyecto de cliente
infra/               terraform / configuración Cloud Run, Tasks, Scheduler
frontend/dashboard/  (Fase 6) panel del owner
docs/                documentación
tests/               unit · integration · security
```
