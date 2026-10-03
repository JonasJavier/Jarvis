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
**Guía recibida (2026-10-01):** adaptada en `docs/railway.md`. Fija acceso (token de proyecto),
servicio web con pre-deploy de migraciones y prohibición de operar OMSTA/OMSTA-Demo desde este
repositorio. Cola, aislamiento de workers, cron y observabilidad siguen pendientes.

## ADR-002 — Django + DRF + PostgreSQL como control plane
**Estado:** Aceptada

**Decisión:** el owner domina Django/DRF; PostgreSQL es la base operativa de clientes, tickets, jobs,
presupuestos, uso y auditoría (las políticas críticas se materializan desde manifests, ADR-017).
**Consecuencias:** los trabajos largos nunca viven en un request web.

## ADR-003 — Cola de tareas como abstracción
**Estado:** Aceptada (abstracción) · implementación de producción decidida en ADR-034 (`PostgresQueue`, 2026-10-03)

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
**Estado:** Aceptada (2026-10-02): opción A, con Claude Code dentro del sandbox apuntando al proxy

**Contexto:** la suscripción Max incluye Claude Code; las condiciones de uso de Agent SDK / `claude -p`
con suscripción han cambiado durante 2026. La API usa créditos prepagados con hard stop.
**Opciones a evaluar:** suscripción Max vs. API key; y si el worker accede al modelo directamente o a
través de un proxy del `LLMGateway` (el worker no tendría credencial propia y cada llamada pasaría
por `BudgetGuard` antes de ejecutarse).

**Alternativas presentadas al owner (2026-10-02, tras la Fase 3 con `MockCodingAgent`):**

| Opción | Cómo | A favor | En contra |
|---|---|---|---|
| A. Proxy del `LLMGateway` + API key (recomendada) | El sandbox sigue sin red ni credenciales; el agente dentro del contenedor habla con un endpoint local del control plane (socket/host gateway) que aplica `BudgetGuard`, `UsageLedger` y el filtro de secretos antes de llamar a la API de Anthropic con la clave del control plane. | Cumple las reglas 2, 6 y 10 tal cual; coste real por `JobRun`; hard stop por presupuesto en cada llamada; el worker no puede exfiltrar la clave. | Hay que implementar el proxy (Fase 3b) y el agente se construye con el Agent SDK/Messages API contra ese endpoint, no con `claude -p`. |
| B. API key inyectada en el sandbox | `ANTHROPIC_API_KEY` en el entorno del contenedor con egress permitido solo al endpoint del modelo. | Más simple; permite usar Claude Code/Agent SDK tal cual. | Una credencial de pago dentro del entorno que ejecuta código no confiable (T2); el presupuesto se controlaría a posteriori; exige allowlist de egress real. |
| C. Suscripción Max (Claude Code en el host) | Ejecutar `claude -p` en el host del control plane con la sesión del owner. | Sin coste por token adicional. | Las condiciones de uso de la suscripción para automatización han cambiado en 2026 y no está claro que permitan uso desatendido; sin medición de coste real en `UsageLedger`; rompe el aislamiento (el agente correría fuera del sandbox). |

**Decisión del owner (2026-10-02): opción A.** El agente será Claude Code ejecutándose dentro del
sandbox con `ANTHROPIC_BASE_URL` apuntando al proxy del `LLMGateway` y una clave ficticia; la API key
real vive solo en el control plane. Claude Code corre con herramientas restringidas (lectura y
edición del worktree; sin Bash libre; `run_tests`/`run_linter` como herramientas acotadas) y sin
internet. Se implementa como Fase 3b.

## ADR-006 — PostgreSQL de producción
**Estado:** Aceptada (2026-10-03): Railway PostgreSQL, ver ADR-034

**Opciones:** Railway PostgreSQL u otra alternativa si hay una razón técnica clara (backups,
point-in-time recovery, red privada, latencia).
**Propuesta (2026-10-01):** Railway PostgreSQL, conectado por referencia `${{Postgres.DATABASE_URL}}`
según la guía del owner. Se confirma en Fase 4 tras revisar backups y recuperación.

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
   debe depender de un túnel hacia el PC. Sigue la guía del owner (`docs/railway.md`).
3. Staging/producción de proyectos de clientes en la Fase 6, junto con aprobaciones y panel.

## ADR-009 — Canales oficiales
**Estado:** Aceptada

**Decisión:** GitHub App privada (no PAT global); Meta WhatsApp Cloud API directa (no Baileys ni
automatización de navegador); Gmail API con OAuth (no SMTP con contraseña); LinkedIn solo publicación
con `w_member_social` (Fase 12). Gmail requiere un cliente OAuth en un proyecto de Google
(y Pub/Sub si se usa push) por exigencia de la propia API, independientemente del hosting.

**Número de WhatsApp (ampliado 2026-10-01):** Jarvis usa un **número dedicado**, no el número personal
del owner. Automatizar un número personal requeriría librerías no oficiales (contrarias a los términos
de WhatsApp, con riesgo de bloqueo del número) y mezclaría vida personal y negocio. El rol Comercial
usará además un número separado del de soporte (ADR-024).

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
**Estado:** Aceptada (requisitos y abstracción) · backend de producción decidido en ADR-034 (runner con Docker fuera de Railway, 2026-10-03)

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
solo de staging, y producción solo `Deployer` mediante `ProductionTools` (ADR-023). Los jobs de CI que
ejecutan código del PR no tienen secretos de deploy.

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
**Consecuencias:** un fallo en el código de Jarvis no basta para integrar código sin pasar por PR y
CI. Si un proyecto exige además revisión humana obligatoria en el Ruleset, Jarvis no podrá hacer
merge autónomo aunque su nivel lo permita; es una decisión por proyecto.

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

## ADR-021 — Jarvis como empleado: tres roles
**Estado:** Aceptada (2026-10-01)

**Contexto:** el objetivo es depender cada vez menos del owner: no solo mantener proyectos, sino
construirlos y conseguir clientes.
**Decisión:** tres roles construidos en orden: **Soporte** (Fases 1–10), **Constructor** (Fase 11:
de idea a MVP con QA y conversación con el cliente) y **Comercial** (Fase 12: prospectos, propuestas,
demos y contacto). Cada rol reutiliza las piezas del anterior.
**Consecuencias:** el núcleo de seguridad (Fases 1A–1B) es aún más importante: más autonomía exige
más barreras deterministas, no menos.

## ADR-022 — Niveles de autonomía por proyecto
**Estado:** Aceptada (2026-10-01)

**Decisión:** cada proyecto declara `autonomy_level` (0–4) en su manifest. Cada acción tiene una clase
de riesgo (`read`, `internal`, `low`, `medium`, `high`, `critical`) fijada en código. El
`PolicyEngine` decide nivel × clase; las acciones `critical` requieren siempre aprobación; las
prohibiciones globales y el techo por actor prevalecen. Nivel 4 solo para `ownership: internal`
salvo ADR explícita. Nivel inicial recomendado para clientes: 2.
**Consecuencias:** reemplaza la matriz fija "MVP / futuro". Subir de nivel es un cambio revisado del
manifest, respaldado por el informe de confianza (Fase 10).

## ADR-023 — Acceso de Jarvis a producción mediante herramientas controladas
**Estado:** Aceptada (2026-10-01)

**Contexto:** el owner quiere que Jarvis pueda ver y modificar producción de los proyectos.
**Decisión:** Jarvis lee producción (logs, métricas, errores sanitizados) desde el inicio y opera
producción mediante un catálogo cerrado de `ProductionTools` (deploy, rollback, restart, migración
con backup, configuración no sensible), ejecutado por el `Deployer` determinista. Los agentes LLM
(`ops_agent`) solo solicitan herramientas; nunca poseen credenciales ni shell libre en producción.
Salvaguardas: backup previo, rollback automático, límite de operaciones por hora, alerta al owner.
**Consecuencias:** producción puede operarse de forma autónoma según el nivel del proyecto sin que
un error del modelo o una inyección tengan acceso directo a credenciales. Mantiene ADR-016: el coding
worker sigue sin desplegar.

## ADR-024 — Reglas de contacto comercial
**Estado:** Aceptada (2026-10-01)

**Decisión:** el owner aprueba cada campaña (público, plantilla, canal, volumen). WhatsApp solo con
contactos con consentimiento y desde un número comercial separado; email cumpliendo normas anti-spam;
mensajes privados en LinkedIn/redes redactados por Jarvis y enviados por el owner; solo se automatiza
lo permitido por APIs oficiales. Datos de prospectos mínimos, públicos y con baja respetada.
**Consecuencias:** el alcance comercial es menor que un "envío masivo", pero protege el número de
soporte, las cuentas del owner y el cumplimiento legal.

## ADR-025 — Base técnica de la Fase 1A
**Estado:** Aceptada (2026-10-01)

**Decisión:** Python 3.13 (fijado en `.python-version`), Django 6.1, DRF 3.18, Pydantic 2 para los
esquemas de manifests, `phonenumbers` para E.164, `dj-database-url` para leer `DATABASE_URL` sin
asumir proveedor. DRF sin clases de autenticación: ningún endpoint de negocio es accesible hasta la
Fase 1B. `global.yaml` no se materializa en tabla propia; entra en el hash efectivo de cada política.
**Consecuencias:** el mismo `DATABASE_URL` sirve en local, CI y producción. Los tests usan PostgreSQL
real (no SQLite) para que el comportamiento coincida con producción.

## ADR-026 — `global.yaml` materializado como `GlobalPolicy`
**Estado:** Aceptada (2026-10-02) · matiza ADR-025

**Contexto:** el `PolicyEngine`, el `BudgetGuard` y el `ApprovalService` necesitan en tiempo de
ejecución los techos y prohibiciones globales (forbidden, presupuestos, TTL de aprobaciones, umbrales,
niveles máximos). ADR-025 decía que `global.yaml` no tenía tabla propia.
**Decisión:** `GlobalPolicy` es una fila única (pk=1) escrita solo por `load_manifests`, de solo
lectura en el admin y vigilada por `check_manifests`. `global.yaml` sigue entrando en el hash
efectivo de cada `ContractPolicy`. Si la fila no existe, el motor de política deniega todo.
**Consecuencias:** una sola fuente en tiempo de ejecución (la base de datos materializada) y
fail-closed si los manifests nunca se cargaron.

## ADR-027 — Catálogo de acciones y techos de actor en código
**Estado:** Aceptada (2026-10-02)

**Decisión:** `policies/actions.py` define el catálogo cerrado de acciones (`Action`), la clase de
riesgo de cada una (`RISK_OF`), la clase máxima autónoma por nivel (`AUTONOMOUS_CEILING`) y el techo
de capacidades de cada actor (`ACTOR_CEILING`). Los manifests solo pueden nombrar acciones del
catálogo (`restrict`, `forbidden`); un nombre desconocido invalida el bundle. Los agentes LLM
(`client_agent`, `ops_agent`, `coding_worker`) no ejecutan acciones con efectos externos: el
`coding_worker` termina en el Draft PR, `ops_agent` solo lee, `client_agent` solo redacta. El `owner`
no ejecuta acciones: aprueba o rechaza. El `PolicyEngine` también bloquea acciones no `read` cuando la
base de datos ha derivado de los manifests (`project_drift`).
**Consecuencias:** cambiar la clase de riesgo de una acción o el techo de un actor es un cambio de
código revisado y cubierto por `tests/security/`. Añadir una acción exige asignarle clase y al menos
un actor ejecutor (comprobado al importar el módulo).

## ADR-028 — Catálogo de precios versionado en `pricing/pricing.yaml`
**Estado:** Aceptada (2026-10-02)

**Decisión:** tarifas en `pricing/pricing.yaml` (ruta configurable `JARVIS_PRICING_FILE`) con una
`version` global que cada `UsageLedger` guarda como `pricing_version`. Precios por millón de tokens
(entrada, salida, lectura y escritura de caché) y por unidad (mensajes). Un proveedor/modelo sin
tarifa es `PricingError`, nunca coste cero. Los modelos se eligen por **rol** (`cheap`, `coding`,
`reasoning`) desde variables de entorno `JARVIS_LLM_MODEL_*`; el proveedor desde `JARVIS_LLM_PROVIDER`
(solo `fake` hasta la Fase 3). El proveedor estima **uso** (tokens) y el `LLMGateway` lo tarifica,
en lugar de que el proveedor estime coste.
**Consecuencias:** las tarifas reales de Anthropic y Meta se añaden al activar las Fases 3 y 5,
verificadas contra la documentación oficial; los costes históricos siguen siendo reproducibles.

## ADR-029 — Semántica de presupuestos, periodos y circuit breaker
**Estado:** Aceptada (2026-10-02)

**Decisión:** una fila `Budget` por `(scope, scope_ref, period, period_key)`; periodos `day` y
`month` según la zona horaria de `global.yaml`, y `run` para el tope por ejecución
(`max_ai_usd_per_run`, scope `job`, ref `run:{job_run_id}`). Las filas se crean bajo demanda con el
límite vigente del manifest (que se reescribe si el manifest cambió) y se bloquean con
`select_for_update` en el orden fijo global → client → project → job. El circuit breaker vive en la
propia fila (`tripped_at`): se dispara al llegar al 100 % del periodo, o manualmente para el scope
global (`trip_global`), y bloquea cualquier reserva que toque esa fila. A partir de
`low_priority_block_pct` (nuevo campo de `global.yaml`, 90 %) se rechaza el trabajo de prioridad
baja. Las denegaciones y los cruces de umbral se auditan fuera de la transacción revertida.
**Consecuencias:** sin deadlocks entre reservas concurrentes (test con hilos reales); un coste real
superior al estimado se cobra igual y puede disparar el breaker; los recursos de cómputo siguen fuera
de este mecanismo (ADR-013).

## ADR-030 — Identidad de la API: `IdentityVerifier` y allowlist del owner
**Estado:** Aceptada (2026-10-02)

**Decisión:** la API autentica con `Authorization: Bearer <token>` contra la implementación de
`IdentityVerifier` configurada en `JARVIS_IDENTITY_VERIFIER` (`FakeVerifier` en desarrollo y tests;
la real se decide en la Fase 6). El owner se reconoce por `JARVIS_OWNER_EMAILS` (emails normalizados
con las mismas reglas que los contactos), nunca por un claim del token. Solo una identidad owner puede
aprobar o rechazar un `Approval`; solo `control_plane`, `deployer` y `staging_trigger` pueden
solicitar o consumir uno.
**Consecuencias:** ningún endpoint acepta anónimos; el panel de la Fase 6 solo tiene que aportar un
verificador real y las vistas.

## ADR-031 — Cambios como `ChangeSet` vía Git Data API; webhooks con resumen tipado
**Estado:** Aceptada (2026-10-02)

**Contexto:** architecture.md hablaba de un `GitBundle` para que el worker entregue sus cambios al
RepoBroker. Eso exigiría `git` y un clon en el control plane.
**Decisión:** el broker recibe un `ChangeSet` (commit base + lista de archivos a escribir o borrar +
mensaje) y `GitHubAppHost` lo convierte en commit con la Git Data API (blobs → tree → commit → ref)
usando su propio token de instalación. El guard de rutas protegidas se aplica a las rutas del
`ChangeSet` antes de tocar el host. El nombre de rama es determinista (`jarvis/{ticket}-{job}`); si
existe se reutiliza; antes de crear un PR se consulta si ya hay uno abierto para esa rama.
Los tokens de instalación se piden con `repositories` y `permissions` explícitos y la respuesta se
verifica: si GitHub devuelve más permisos o más repos de los pedidos, el token se descarta.
Del payload de cada webhook solo se persiste un resumen tipado (`summarize`), nunca el cuerpo
completo; la firma HMAC es la única autenticación y sin secreto configurado se rechaza todo.
Hasta la Fase 4, `InProcessQueue` puede ejecutar el handler en el mismo proceso al encolar; los
fallos del handler se auditan (`task.failed`) y el evento ya persistido puede reprocesarse.
**Consecuencias:** el control plane no necesita `git` ni espacio de clonado para publicar; el worker
(Fase 3) exporta su diff como `ChangeSet`. Cambios binarios grandes pasan por blobs base64 (límite
de GitHub ~100 MiB por archivo, muy por encima del límite del worker).

## ADR-032 — Sandbox del coding worker: clon en el host, dos fases y exportación como `ChangeSet`
**Estado:** Aceptada (2026-10-02)

**Contexto:** architecture.md §11 pedía egress denegado por defecto con allowlist y un worker con
solo un token de lectura. Con Docker local no hay allowlist de hosts sin infraestructura extra, y un
token en el entorno del contenedor es legible por cualquier script del repo.
**Decisión:**
1. El **control plane clona** el repositorio en el workspace con el token de instalación de solo
   lectura enviado como cabecera `Authorization: basic` (nunca en la URL ni en disco). El contenedor
   **no recibe ninguna credencial**: ni GitHub, ni modelo, ni base de datos.
2. El worker corre en **dos fases**, un contenedor por fase sobre el mismo workspace montado:
   `prepare` (comandos `install` del manifest, con red para registries) y `work` (agente + tests,
   `--network none`). Ambas con `--cpus`, `--memory`/`--memory-swap`, `--pids-limit`, `--cap-drop
   ALL`, `no-new-privileges`, rootfs de solo lectura, usuario 1000 y kill al agotar el tiempo. El
   tamaño del workspace se mide al terminar y el workspace se destruye desde el propio sandbox.
3. El worker **no hace commit ni push**: exporta los archivos cambiados (`git status` contra el
   commit base) como `AgentReport.files`; el orquestador los convierte en el `ChangeSet` de la
   ADR-031 y el `RepoBroker` crea el commit y el Draft PR. El veredicto de los tests lo toma el
   runner, nunca el agente.
4. Los clones y el `git status` fuerzan `core.autocrlf=false`, `core.eol=lf`, `core.filemode=false`
   y `core.symlinks=false` para que un control plane en Windows y un worker en Linux vean el mismo
   árbol.
5. `InProcessExecutor` ejecuta el mismo runner en el host sin aislamiento: solo para tests y
   desarrollo de confianza, nunca para repos de terceros.
**Consecuencias:** T2 queda mitigada de forma más fuerte que lo documentado (cero credenciales en el
sandbox). El egress permitido durante `prepare` es amplio: la allowlist real y el aislamiento de
producción siguen pendientes (ADR-015, Fase 4). El agente con modelo (ADR-005) necesitará un canal
hacia el `LLMGateway` que no sea red abierta.

## ADR-033 — La ventanilla: proxy del `LLMGateway` y red por ejecución para el agente
**Estado:** Aceptada (2026-10-02) · implementa la opción A de la ADR-005

**Contexto:** el sandbox no tiene red ni credenciales (ADR-032), pero Claude Code necesita hablar
con el modelo. En Docker, una red `--internal` no alcanza al host, así que hace falta un camino
explícito y exclusivo.
**Decisión:**
1. **Proxy** `POST /llm/v1/messages` (y `count_tokens`) en el control plane, transparente a la
   Messages API (streaming incluido). Por petición: resuelve un **token por ejecución** a un
   `JobRun` activo (solo se guarda el hash; expira con el timeout del run y deja de valer al
   terminar), aplica un tope de peticiones derivado de `max_turns`, rechaza prompts con secretos
   configurados, reserva presupuesto con `BudgetGuard` para una cota superior, reenvía a
   Anthropic con la clave del control plane, concilia el uso real (tokens de entrada, salida y
   caché) en `UsageLedger` y `JobRun.cost_usd`, y audita (`llm.proxy.*`). Un modelo sin tarifa en
   `pricing/pricing.yaml` se rechaza.
2. **Red por ejecución:** la fase `work` corre en una red Docker `--internal` cuyo único otro
   miembro es un contenedor **reenviador** (`workers/coder/forward.py`, stdlib, imagen del worker,
   sin capabilities, rootfs de solo lectura) conectado además a `bridge` y que reenvía su puerto a
   un único destino: el proxy (`JARVIS_LLM_PROXY_TARGET`). El worker recibe
   `ANTHROPIC_BASE_URL=http://<reenviador>:8080/llm` y `ANTHROPIC_API_KEY=<token del run>`; no puede
   alcanzar nada más. Sin `ANTHROPIC_API_KEY` en el control plane no hay red en absoluto.
3. **Claude Code dentro del sandbox** (`ClaudeCodeAgent`): modo `-p` con la tarea por stdin,
   `--output-format json`, `--max-turns` del manifest, herramientas de archivos permitidas, `Bash`
   denegado salvo los comandos exactos de test/lint del manifest, rutas protegidas denegadas en sus
   permisos y de nuevo en la exportación del runner y en el guard del broker; telemetría,
   autoactualización y tráfico no esencial desactivados. El veredicto de los tests sigue siendo del
   runner, no del agente.
4. Modelos por rol en configuración (`JARVIS_LLM_MODEL_CODING` → `ANTHROPIC_MODEL`,
   `JARVIS_LLM_MODEL_CHEAP` → modelo pequeño de Claude Code). Por defecto `claude-opus-5-5` y
   `claude-haiku-4-5`, con tarifas en el catálogo.
**Consecuencias:** cumple las reglas 2, 6, 10 y 12 de CLAUDE.md con el mismo Claude Code que usa el
owner; el único valor que un script malicioso podría leer en el sandbox es un token que solo sirve
para gastar el presupuesto de ese run a través del proxy, y ese gasto está acotado por run, día y
mes. Coste: un contenedor y una red extra por ejecución, y el proxy añade una pasada de parseo de
SSE. La prueba real con Claude requiere `ANTHROPIC_API_KEY` en el `.env` del control plane.

## ADR-034 — Topología de producción en Railway (Fase 4)
**Estado:** Aceptada (2026-10-03) · cierra las partes pendientes de ADR-001, ADR-003, ADR-006 y ADR-015

**Contexto:** Railway ejecuta contenedores **no privilegiados**: no hay Docker dentro de Docker, así
que el sandbox de la Fase 3 no puede correr allí. Además `railway.toml` está obsoleto para servicios
nuevos (corte 2026-12-01); la configuración se declara con Infrastructure as Code.
**Decisión:**
1. **Proyecto** `jarvis-ops` (ID `4719fd5e-f83a-42ff-bf0e-08cbfff4dd17`), ambiente `production`,
   definido en `.railway/railway.ts` (IaC, SDK `railway` en `package.json`; se aplica con
   `railway config apply` desde este repo, nunca por dashboard). Tres recursos:
   - `postgres`: Railway PostgreSQL, referenciado como `${{Postgres.DATABASE_URL}}` (**ADR-006
     decidida**: Railway PostgreSQL; backups y recuperación se revisan en la Fase 9).
   - `api`: imagen del `Dockerfile` del repo (gunicorn + WhiteNoise), pre-deploy
     `migrate && load_manifests`, healthcheck `/healthz`, dominio generado por Railway.
   - `worker`: la misma imagen con `run_worker --kinds inbound_event`: consume la cola duradera para
     webhooks y tareas del control plane. No tiene secretos de integraciones.
2. **Cola de producción (ADR-003 decidida):** `PostgresQueue` sobre la propia base de datos
   (`SELECT … FOR UPDATE SKIP LOCKED`, reintentos con backoff, dead-letter tras `max_attempts`,
   recuperación de locks obsoletos). Sin Redis ni broker adicional: a esta escala, una pieza menos
   que operar y la misma transacción que el resto del dominio. Los ids de tarea llevan su clase
   (`inbound_event:…`, `job:…`) y cada worker consume solo las clases que puede ejecutar.
3. **Ejecución de workers (ADR-015, backend de producción):** los jobs de código (`job:*`) solo los
   consume un **runner con Docker** fuera de Railway, ejecutando `run_worker --kinds job` con el
   `LocalDockerExecutor` y el proxy del `LLMGateway` locales, conectado a la base de datos de
   producción. Hoy ese runner es el PC del owner; la opción de un VPS pequeño con Docker (misma
   imagen, mismo comando) se decide cuando el volumen lo justifique. Mientras el runner esté
   apagado, los jobs esperan en la cola; intake, webhooks, tickets y aprobaciones siguen 24/7.
4. **Secretos:** solo los define el owner con `railway variables --set` y el IaC los conserva con
   `preserve()`; `DJANGO_SECRET_KEY` se genera aleatoriamente por servicio. `HSTS` 30 días sin
   `preload` (irreversible para el dominio). `ALLOWED_HOSTS` incluye `healthcheck.railway.app`.
**Consecuencias:** el flujo completo de soporte funciona con el PC apagado salvo la ejecución del
agente de código, que depende del runner; es el compromiso explícito hasta decidir el VPS. La
configuración de Railway queda versionada y revisable como el resto de la política.
