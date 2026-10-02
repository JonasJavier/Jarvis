# Registro de decisiones (ADR)

Formato: contexto breve → decisión → consecuencias. Estados: `Aceptada`, `Pendiente` (se decide en
la fase indicada; no se toma por iniciativa propia) o `Reemplazada`.

---

## ADR-001 — Cloud-first con Railway como producción
**Estado:** Aceptada (revisada 2026-10-01: Railway sustituye a Google Cloud)

**Contexto:** Jarvis debe atender WhatsApp, email, webhooks y cron 24/7 aunque el PC esté apagado.
El owner dispone de un plan de Railway; optimizar recursos de cómputo no es prioridad.
**Decisión:** desarrollo local con Docker / docker compose; producción en **Railway**. Firebase puede
usarse para Auth/Hosting del panel, no para el backend.
**Pendiente para la fase de deployment (Fase 4), tras la guía del owner:** servicios, networking,
workers, cron, volúmenes, PostgreSQL, secretos, dominios, deployment, escalado y observabilidad.
**Consecuencias:** la lógica de dominio no depende de Railway; todo lo específico de infraestructura
queda detrás de interfaces (`TaskQueue`, `WorkerExecutor`, `DeployTarget`, configuración por entorno).

## ADR-002 — Django + DRF + PostgreSQL como control plane
**Estado:** Aceptada

**Decisión:** el owner domina Django/DRF; PostgreSQL es la base operativa de clientes, tickets, jobs,
presupuestos, uso y auditoría (las políticas críticas se materializan desde manifests, ADR-017).
**Consecuencias:** los trabajos largos nunca viven en un request web.

## ADR-003 — Cola de tareas como abstracción
**Estado:** Aceptada (abstracción) · **Pendiente** (implementación de producción, Fase 4)

**Decisión:** interfaz `TaskQueue`; `InProcessQueue` para desarrollo y tests. **No** se elige
todavía Redis, Celery, cola en PostgreSQL ni otra tecnología de producción. La anterior elección de
Cloud Tasks queda descartada al cambiar el destino a Railway.
**Consecuencias:** en la Fase 4 se presentarán alternativas y consecuencias antes de elegir, salvo que
una fase anterior lo requiera necesariamente (en ese caso, se planteará primero al owner).

## ADR-004 — Integraciones detrás de interfaces con fakes
**Estado:** Aceptada

**Decisión:** GitHub, WhatsApp, Gmail, Firebase, Claude, cola, ejecución de workers y deploy se
implementan detrás de `Protocol`s con implementaciones fake/locales. Las Fases 1A–1B no usan ninguna
credencial real.

## ADR-005 — Credencial de Claude para workers
**Estado:** Pendiente (Fase 3)

**Contexto:** la suscripción Max incluye Claude Code; las condiciones de uso de Agent SDK / `claude -p`
con suscripción han cambiado durante 2026. La API usa créditos prepagados con hard stop.
**Opciones a evaluar:** suscripción Max vs. API key; y si el worker accede al modelo directamente o a
través de un proxy del `LLMGateway` (el worker no tendría credencial propia y cada llamada pasaría
por `BudgetGuard` antes de ejecutarse).

## ADR-006 — PostgreSQL de producción
**Estado:** Pendiente (Fase 4)

**Opciones:** Railway PostgreSQL u otra alternativa si hay una razón técnica clara (backups,
point-in-time recovery, red privada, latencia).

## ADR-007 — Django admin como panel provisional
**Estado:** Aceptada

**Decisión:** hasta la Fase 6, el owner opera desde Django admin (acceso restringido). Los modelos
materializados desde manifests son de solo lectura en el admin (ADR-017). El panel definitivo llega en
la Fase 6 (Firebase Auth/Hosting o Django templates, a decidir al iniciar esa fase).

## ADR-008 — Orden de fases
**Estado:** Aceptada (revisada 2026-10-01)

**Decisión:**
1. Fundamentos divididos en **1A (scaffold y dominio mínimo)** y **1B (núcleo de seguridad)**.
2. **Despliegue de Jarvis en Railway como fase propia (4)**, antes de WhatsApp: el número comercial no
   debe depender de un túnel hacia el PC. Bloqueada hasta recibir la guía del owner.
3. Staging/producción de proyectos de clientes en la Fase 6, junto con aprobaciones y panel.

## ADR-009 — Canales oficiales
**Estado:** Aceptada

**Decisión:** GitHub App privada (no PAT global); Meta WhatsApp Cloud API directa (no Baileys ni
automatización de navegador); Gmail API con OAuth (no SMTP con contraseña); LinkedIn solo publicación
con `w_member_social` y fuera del MVP. Gmail requiere un cliente OAuth en un proyecto de Google
(y Pub/Sub si se usa push) por exigencia de la propia API, independientemente del hosting.

## ADR-010 — Identificación: normalización canónica + coincidencia exacta
**Estado:** Aceptada (ampliada 2026-10-01)

**Decisión:** el remitente se normaliza de forma canónica y segura (teléfonos a E.164 con librería
de parsing; emails con trim, NFC y minúsculas, sin quitar puntos ni `+tag` ni resolver alias) y se
compara **exactamente** contra `Contact`. Sin fuzzy matching ni LLM para autorizar acciones.
Sin identificación inequívoca de cliente **y** proyecto ⇒ `NeedsIdentification` y ninguna acción
sobre repositorios o infraestructura.

## ADR-011 — Monorepo de control
**Estado:** Aceptada

**Decisión:** este repositorio contiene solo Jarvis. El código de los clientes vive en sus propios
repositorios y se clona por job.

## ADR-012 — Aprobaciones ligadas a `action_digest`, de un solo uso
**Estado:** Aceptada

**Decisión:** cada `Approval` se liga a `sha256` del JSON canónico de `{project, action,
target/environment, commit SHA o artifact digest, parámetros relevantes, manifest_hash}`. Single-use
(`used_at`, consumo atómico ligado a la idempotency key de la ejecución), `expires_at` obligatorio,
recálculo del digest antes de ejecutar.
**Consecuencias:** cualquier cambio del target, commit, artifact, parámetros o política invalida la
aprobación. Un reintento manual tras fallo requiere aprobación nueva (más fricción, más seguridad).

## ADR-013 — UsageLedger y presupuestos jerárquicos con locking ordenado
**Estado:** Aceptada

**Decisión:** cada consumo de pago por uso se registra en un `UsageLedger` append-only (provider,
model, tokens incluidos cacheados, units, coste real, `pricing_version`, client, project, job,
timestamp). Una reserva afecta simultáneamente a `global → client → project → job`, bloqueando en ese
orden fijo.
**Consecuencias:** coste atribuible por cliente/proyecto/ticket; sin deadlocks por orden de locking;
los recursos de cómputo de Railway quedan fuera de este mecanismo.

## ADR-014 — Idempotencia de todos los efectos secundarios
**Estado:** Aceptada

**Decisión:** entrega at-least-once asumida en todos los handlers. `IdempotencyRecord` con clave única
para ticket, job, lanzamiento de worker, branch, PR, mensajes outbound, staging, deployments,
mantenimiento y cualquier operación externa repetible (claves en architecture.md §10). Ante un efecto
en estado incierto se reconcilia con el proveedor antes de reintentar.

## ADR-015 — Aislamiento y límites del coding worker
**Estado:** Aceptada (requisitos y abstracción) · **Pendiente** (backend de producción, Fase 4)

**Decisión:** ejecutar código de un repo es ejecución no confiable. Límites obligatorios de CPU, RAM,
PIDs, tiempo, workspace, tamaño de archivo, egress, intentos y turnos (valores en architecture.md §11).
Interfaz `WorkerExecutor`; `LocalDockerExecutor` en desarrollo. No se depende de características
específicas de ningún proveedor de nube.
**Consecuencias:** la estrategia de aislamiento en producción se documenta e implementa en la Fase 4,
tras la guía del owner, verificando que la plataforma permite cumplir estos límites (en especial el
control de egress).

## ADR-016 — Separación entre coding worker y despliegue
**Estado:** Aceptada

**Decisión:** el coding worker termina en `clone → branch → cambios → tests → commit → Draft PR`.
Recibe solo un token GitHub de solo lectura de un repo; push y PR los hace el RepoBroker. Nunca tiene
ni puede obtener credenciales de deployment: staging lo inicia `StagingTrigger`/CI con credenciales
solo de staging, y producción solo `Deployer` con `Approval`. Los jobs de CI que ejecutan código del
PR no tienen secretos de deploy.

## ADR-017 — Manifests versionados como fuente de verdad
**Estado:** Aceptada

**Decisión:** para v1, `project_manifests/` (`global.yaml`, `clients/`, `projects/`) es la fuente de
verdad de seguridad, autonomía, límites, contrato y acciones permitidas/aprobables/prohibidas.
PostgreSQL mantiene una representación materializada con `manifest_hash`; solo lectura en el admin;
`check_manifests` detecta drift y bloquea.
**Consecuencias:** cambiar una política es un commit revisable. Permitir edición desde el panel
requerirá una ADR nueva sobre sincronización y versionado.

## ADR-018 — Defensa en profundidad en GitHub
**Estado:** Aceptada

**Decisión:** además de los controles de Jarvis, cada repo gestionado debe tener Rulesets/branch
protection: sin pushes directos a la rama por defecto, PR obligatorio, required checks, la GitHub App
de Jarvis sin bypass, sin permiso `workflows`. Jarvis verifica estas reglas antes de operar.
**Consecuencias:** un fallo en el código de Jarvis no basta para integrar código no revisado.

## ADR-019 — Modelos y migraciones incrementales
**Estado:** Aceptada

**Decisión:** cada fase crea solo los modelos que sus flujos necesitan (tabla en architecture.md §6).
La Fase 1A se limita a `Client`, `Contact`, `Project`, `ContractPolicy` y `AuditEvent`.

## ADR-020 — Auditoría append-only a nivel de aplicación
**Estado:** Aceptada

**Decisión:** `AuditEvent` es append-only a nivel de aplicación; no se presenta como criptográficamente
inmutable. Almacenamiento externo, logging centralizado, export, tamper-evident logging y hash
chaining quedan como opciones para la Fase 9 / deployment. Sin dependencia de ningún servicio de
logging de un proveedor concreto.
