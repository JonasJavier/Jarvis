# Plan de implementación por fases

## Protocolo de trabajo

Cada fase sigue el mismo ciclo:

1. **Inicio:** leer `CLAUDE.md` y la sección de la fase. Confirmar requisitos previos del owner.
2. **Implementación:** solo el alcance de la fase. Lo que aparezca fuera de alcance se anota en
   "Pendientes detectados" de la fase, no se implementa.
3. **Verificación:** tests + lint + typecheck en verde; criterios de salida cumplidos.
4. **Cierre:** actualizar la tabla de estado y el registro de decisiones si hubo decisiones nuevas.
5. **⛔ STOP:** revisión del owner. La siguiente fase no empieza sin su visto bueno.

Las fases son pequeñas a propósito: cada una produce algo verificable y se puede revisar en una
sesión, lo que mantiene bajo el consumo de tokens y evita generar código antes de validar decisiones.

## Estado

| Fase | Nombre | Estado | Estimación |
|---|---|---|---|
| 0 | Documentación y fundamentos | ✅ Completada — pendiente revisión | — |
| 1A | Scaffold del control plane | ⏳ Pendiente | 5–8 h |
| 1B | Núcleo de seguridad: política, presupuesto, idempotencia | ⏳ Pendiente | 8–12 h |
| 2 | Integración GitHub App | ⏳ Pendiente | 15–25 h |
| 3 | Worker de código (mock → Claude) | ⏳ Pendiente | 20–35 h |
| 4 | Despliegue en Google Cloud | ⏳ Pendiente | 10–18 h |
| 5 | WhatsApp Cloud API | ⏳ Pendiente | 12–20 h |
| 6 | Panel del owner y aprobaciones | ⏳ Pendiente | 12–20 h |
| 7 | Gmail | ⏳ Pendiente | 10–18 h |
| 8 | Mantenimiento mensual | ⏳ Pendiente | 15–25 h |
| 9 | Hardening y observabilidad | ⏳ Pendiente | 20–40 h |
| 10 | Autonomía gradual | 🔁 Continuo | — |

Hitos: **prototipo** al terminar Fase 3 (~50–80 h) · **MVP usable con clientes** al terminar
Fase 6 (~100–140 h) · **operación confiable** tras Fase 9 (~160–250 h).
Las estimaciones no incluyen esperas de aprobación de Meta/Google.

---

## Fase 0 — Documentación y fundamentos

**Objetivo:** fijar arquitectura, reglas de seguridad y plan antes de escribir código.

**Entregables:** `README.md`, `CLAUDE.md`, `docs/architecture.md`, `docs/implementation-plan.md`,
`docs/threat-model.md`, `docs/permissions-and-approvals.md`, `docs/cost-controls.md`,
`docs/decisions.md`, `project_manifests/example.yaml`, `.env.example`, `.gitignore`.

**Criterio de salida:** el owner revisa y aprueba (o ajusta) las decisiones de `docs/decisions.md`.

---

## Fase 1A — Scaffold del control plane

**Objetivo:** proyecto Django ejecutable localmente con base de datos, modelos y CI.

**Alcance**
- `pyproject.toml` con `uv`; dependencias: Django, DRF, psycopg, pydantic, PyYAML; dev: pytest,
  pytest-django, ruff, mypy, django-stubs, factory-boy.
- `apps/api/` con settings separados (`base`, `dev`, `test`, `prod`), configuración por variables de entorno.
- `compose.yaml`: Postgres + API. `Dockerfile` de la API.
- Modelos y migraciones: `Client`, `Contact`, `Project`, `ContractPolicy`, `IntegrationConnection`,
  `Conversation`, `Message`, `InboundEvent`, `Ticket`, `MaintenancePlan`, `Job`, `JobRun`,
  `Artifact`, `Approval`, `Budget`, `BudgetReservation`, `AuditEvent`, `Deployment`.
- Django admin registrado para todos los modelos (será el panel provisional hasta la Fase 6).
- Cargador de `project_manifests/*.yaml` → `Project` + `ContractPolicy` (comando `manage.py load_manifests`), validado con pydantic.
- GitHub Actions `ci.yml`: ruff, mypy, pytest con Postgres de servicio.

**Fuera de alcance:** lógica de política/presupuesto, endpoints de negocio, cualquier integración externa.

**Criterios de salida**
- `docker compose up` levanta API + DB; `manage.py migrate` limpio.
- `load_manifests` carga `example.yaml` y rechaza manifests inválidos (con test).
- CI en verde.

---

## Fase 1B — Núcleo de seguridad

**Objetivo:** las piezas deterministas que hacen segura la autonomía.

**Alcance**
- `PolicyEngine`: `evaluate(project, action, context) -> Decision{autonomous|requires_approval|forbidden, reason}`.
  Fuente: `ContractPolicy` + reglas globales. Acción desconocida ⇒ `forbidden` (deny by default).
- `ApprovalService`: crear/aprobar/rechazar `Approval`; ejecución de acciones `requires_approval`
  imposible sin `Approval` aprobado y vigente para ese target.
- `BudgetGuard`: `reserve()` / `reconcile()` / `release()` atómicos (transacción + `select_for_update`),
  jerarquía global → cliente → proyecto → job, umbrales 50/80/90/100 %, circuit breaker.
- `LLMGateway` con `FakeLLMProvider` (sin llamadas reales) como único punto de acceso a modelos.
- Idempotencia: `InboundEvent unique(source, external_id)` + servicio `ingest_event()`.
- Máquinas de estado de `Ticket` y `Job` con transiciones validadas.
- `AuditService.record()`; `AuditEvent` append-only.
- `TaskQueue` + `InProcessQueue`.
- Autenticación de API: interfaz `IdentityVerifier` + `FakeVerifier`; DRF authentication class.

**Tests obligatorios (`tests/security/`)**
- Acción `forbidden` nunca se ejecuta; acción desconocida ⇒ `forbidden`.
- Acción `requires_approval` falla sin `Approval`; funciona con `Approval` válido; un `Approval`
  de otro proyecto/target no sirve.
- Presupuesto agotado bloquea el job; el circuit breaker global detiene todo.
- Evento duplicado no crea ticket ni job duplicado.
- Un job del proyecto A no puede referenciar recursos del proyecto B.
- Deploy a producción sin aprobación es imposible.
- Transiciones de estado inválidas se rechazan.

**Fuera de alcance:** proveedores reales (Anthropic, GitHub, Firebase).

**Criterios de salida:** todos los tests de seguridad en verde; cobertura ≥ 90 % en `policies`, `budgets`, `approvals`.

---

## Fase 2 — Integración GitHub App

**Requisitos previos del owner:** crear GitHub App privada con permisos de
[permissions-and-approvals.md](permissions-and-approvals.md#github-app); instalarla **solo** en un
repositorio de prueba; configurar un túnel de desarrollo (smee.io o cloudflared) para webhooks.

**Alcance**
- Endpoint de webhook: validación `X-Hub-Signature-256`, dedupe por `X-GitHub-Delivery`,
  respuesta 202 inmediata, procesamiento vía `TaskQueue`.
- `GitHubAppHost` (implementa `RepoHost`): token de instalación temporal limitado a un repo,
  crear branch, push de cambios, abrir Draft PR, leer estado de checks.
- Broker de credenciales: el token lo emite el control plane, nunca se entrega la clave privada.
- Eventos soportados: `pull_request`, `check_suite`/`check_run`, `issues` (opcional).
- Guard: rechazar cualquier cambio que toque `.github/workflows/` u otras rutas protegidas.

**Criterios de salida:** un job manual (desde admin) crea branch + commit trivial + Draft PR en el
repo de prueba; el resultado de CI se refleja en el `Ticket`; webhook con firma inválida ⇒ 401 y auditado.

---

## Fase 3 — Worker de código

**Requisitos previos del owner:** decidir credencial de Claude para esta fase (suscripción Max vs.
API key con créditos prepagados y auto-reload desactivado — ver ADR-005).

**Alcance**
- `workers/coder/`: runner que recibe `JobSpec`, clona **solo** el repo del job en un directorio
  temporal, ejecuta el agente, corre tests del manifest, genera diff y abre Draft PR vía control plane.
- `CodingAgent` con `MockCodingAgent` (primero, determinista) y luego `ClaudeCodeAgent`.
- Herramientas acotadas para el agente (`run_tests`, `run_linter`, `git_diff`, `read_logs`,
  lectura/edición del worktree). Sin Bash irrestricto, sin red salvo lo necesario.
- Límites duros: `max_turns`, `timeout_seconds`, `max_ai_cost_usd`, `max_retries`.
- Contenedor Docker del worker, sin secretos más allá del token temporal y la credencial de IA.
- Registro de `JobRun` con coste, turnos, herramientas usadas, archivos cambiados, tests.

**Criterios de salida:** un ticket de bug de prueba produce un Draft PR con fix y tests en verde en
el repo de prueba; un prompt de inyección en el ticket no logra leer variables de entorno ni salir
del repo (test de seguridad); agotar presupuesto detiene el job.

**Hito: prototipo funcional.**

---

## Fase 4 — Despliegue en Google Cloud

**Requisitos previos del owner:** proyecto GCP con facturación y alertas de presupuesto de GCP;
decisión de proveedor de Postgres (ADR-006).

**Alcance**
- Terraform (o gcloud documentado) para: Cloud Run (API, min instances 0), Cloud Run Job (worker),
  Cloud Tasks, Cloud Scheduler, Secret Manager, Artifact Registry, service accounts separadas.
- `CloudTasksQueue` implementa `TaskQueue`.
- Secretos resueltos en runtime desde Secret Manager con IAM por secreto.
- `deploy-api.yml` y `deploy-worker.yml` en GitHub Actions (Workload Identity Federation, sin keys JSON).
- Webhook de GitHub apuntando a la URL de Cloud Run.

**Criterios de salida:** flujo de la Fase 3 funcionando en la nube con el PC apagado; ninguna
service account con permisos más amplios que los documentados.

---

## Fase 5 — WhatsApp Cloud API

**Requisitos previos del owner:** cuenta Meta Business, WABA, número verificado, app de Meta con
`whatsapp_business_messaging`; revisar tarifas vigentes de Meta (ver [cost-controls.md](cost-controls.md#whatsapp)).

**Alcance**
- Webhook: verificación de suscripción, validación `X-Hub-Signature-256`, dedupe por message id.
- `WhatsAppCloudProvider` (implementa `MessagingProvider`); estados de entrega vía webhook.
- `Contact` → `Client` por teléfono exacto; desconocido ⇒ `NeedsIdentification` + alerta al owner.
- `OutboundPolicy`: auto-respuesta solo `acknowledgement`, `request_for_debug_information`,
  `status_update`; todo lo demás queda como borrador para aprobación.
- Escalamiento humano: "hablar con Jonas", "necesito una persona", "urgente" ⇒ desactiva auto-respuesta
  en esa conversación y alerta.
- Notificaciones al owner por WhatsApp (resumen de PR listo, aprobaciones pendientes).
- Rate limit de mensajes salientes con corte automático ante anomalías.

**Criterios de salida:** mensaje de cliente real (de prueba) → ticket → acuse automático → job →
Draft PR → aviso al owner; un mensaje con intento de inyección no altera permisos (test).

---

## Fase 6 — Panel del owner y aprobaciones

**Alcance**
- Firebase Auth en frontend; `FirebaseVerifier` en backend (solo el UID del owner autorizado).
- Panel (Next.js en Firebase Hosting, o Django templates si se prefiere simplicidad — decidir al iniciar):
  estado de integraciones, cola, tickets, presupuesto, proyectos, "Needs You" con aprobar/rechazar.
- Vista de trazabilidad por ticket (ver architecture.md §7).
- Aprobación de deploy a producción → `jarvis-deployer`.

**Criterios de salida:** el owner puede aprobar un deploy desde el panel y la acción queda auditada
con su identidad.

**Hito: MVP usable con clientes.**

---

## Fase 7 — Gmail

**Requisitos previos del owner:** proyecto OAuth en Google Cloud; aceptar que los scopes restringidos
pueden requerir verificación si la app deja de ser de uso interno.

**Alcance:** OAuth con `gmail.readonly` + `gmail.send` (añadir `gmail.modify` solo si se justifica);
`users.watch` → Pub/Sub → webhook; renovación programada del watch; `GmailProvider`; misma ruta de
intake/política que WhatsApp.

**Criterios de salida:** email de cliente crea ticket y recibe acuse; token revocado genera alerta crítica.

---

## Fase 8 — Mantenimiento mensual

**Alcance:** Cloud Scheduler por proyecto según manifest; tareas deterministas primero (tests,
dependencias, scans de seguridad, health de endpoints, verificación de backups, tendencia de errores);
`Findings` normalizados; IA solo para findings que requieran interpretación o fix (Batch API cuando
aplique); informe mensual por cliente; clave única `(project, period, task)`.

**Criterios de salida:** un ciclo mensual completo sobre el repo de prueba genera informe y, si hay
finding reparable, un Draft PR; re-ejecutar el cron no duplica trabajo.

---

## Fase 9 — Hardening y observabilidad

**Alcance:** logs estructurados con `correlation_id`; métricas y alertas (ver tabla en
[cost-controls.md](cost-controls.md) y [threat-model.md](threat-model.md)); backoff exponencial y
dead-letter; runbooks en `docs/runbooks/`; tests de seguridad ampliados (inyección, cross-tenant,
replay de webhooks); rollback de deploys; revisión de IAM.

**Criterios de salida:** cada alerta crítica tiene runbook; ejercicio de fallo (worker caído, API de
IA caída, presupuesto agotado) documentado y superado.

---

## Fase 10 — Autonomía gradual (continuo)

Medir por tipo de acción: propuestas aprobadas sin cambios vs. corregidas vs. rechazadas.
Una acción pasa de `requires_approval` a `autonomous` solo si cumple **todo**:
tasa de éxito demostrada en un periodo definido, tests sólidos en el proyecto, rollback probado,
blast radius pequeño, y registro de la decisión en `docs/decisions.md`.

---

## Backlog (sin fase asignada)

- LinkedIn: login + publicación con `w_member_social`, calendario de posts. Sin DMs ni scraping.
- Worker local privado para proyectos que no deben ir a la nube.
- `MicrosoftGraphProvider` para clientes con Outlook.
- Auto-merge para categorías de riesgo muy bajo (depende de Fase 10).
