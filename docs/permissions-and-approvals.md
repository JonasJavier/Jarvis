# Permisos y aprobaciones

## Niveles de decisión

`PolicyEngine.evaluate(actor, action, project)` devuelve uno de tres resultados.
**Deny by default:** una acción no declarada es `forbidden`.

| Nivel | Significado |
|---|---|
| `autonomous` | Jarvis la ejecuta y la audita |
| `requires_approval` | Se crea un `Approval`; no se ejecuta hasta que el owner aprueba |
| `forbidden` | Nunca se ejecuta, ni con aprobación desde el flujo automático |

La política sale de los manifests versionados (`project_manifests/`), que son la fuente de verdad.
Además, cada actor tiene un **techo de capacidades fijado en código** que ningún manifest puede
ampliar (ver [architecture.md §2](architecture.md#2-actores)).

## Matriz de autonomía

| Acción | Actor | MVP | Objetivo a futuro |
|---|---|---|---|
| Leer mensajes y crear tickets | control_plane | autonomous | autonomous |
| Identificar cliente/proyecto (normalizado + exacto) | control_plane | autonomous | autonomous |
| Consultar logs sanitizados | coding_worker | autonomous | autonomous |
| Ejecutar tests / lint / scanners | coding_worker, ci | autonomous | autonomous |
| Modificar worktree, commit | coding_worker | autonomous | autonomous |
| Crear branch y Draft PR | control_plane (RepoBroker, a petición del worker) | autonomous | autonomous |
| Responder acuse / pedir info / estado | control_plane | autonomous (con `OutboundPolicy`) | autonomous |
| Clasificar si está cubierto por mantenimiento | control_plane | regla contractual (+ IA auxiliar) | igual |
| Deploy a staging | staging_trigger / ci | autonomous | autonomous |
| Merge a rama principal | owner | requires_approval | autonomous solo para riesgo muy bajo |
| Deploy a producción | deployer | requires_approval | según manifest |
| Migraciones de base de datos | deployer | requires_approval | requires_approval |
| IAM / rotación de credenciales | owner | requires_approval | requires_approval |
| Operaciones financieras | owner | requires_approval | requires_approval |
| Cotizaciones, plazos, cambios de contrato | owner | requires_approval | requires_approval |
| Cualquier deploy por el coding worker | coding_worker | forbidden | forbidden |
| Exponer secretos, desactivar auditoría, acceder a otro cliente | todos | forbidden | forbidden |
| Modificar CI, manifests o políticas desde una tarea | coding_worker | forbidden | forbidden |

## Reglas de `Approval`

**Ligadura.** Cada aprobación está ligada a un `action_digest` inmutable:

```
action_digest = sha256(JSON canónico de {
  project_id, action, target/environment,
  subject_ref (commit SHA o artifact digest),
  params relevantes de la acción,
  manifest_hash
})
```

**Semántica.**
- **Single-use:** se consume atómicamente al iniciar la ejecución (`used_at`), ligada a la idempotency
  key de esa ejecución. Una redelivery de la misma ejecución continúa; cualquier ejecución nueva
  (incluido un reintento manual tras fallo) necesita aprobación nueva.
- **`expires_at`** obligatorio (TTL definido en `global.yaml`).
- **Invalidación automática:** antes de ejecutar se recalcula el digest de la operación real. Si cambió
  el target, el commit, el artifact, los parámetros relevantes o la política, no coincide y la
  aprobación queda inválida.
- Estados: `pending → approved → used`, o `rejected` / `expired` / `invalidated`.
- Registra quién aprobó, cuándo y desde dónde; cada transición genera `AuditEvent`.
- Un `Approval` no se puede crear, aprobar ni consumir desde un coding worker ni desde un mensaje de cliente.

## Separación del coding worker

- Responsabilidad: `clone → branch → cambios → tests → commit → Draft PR`. Nada más.
- No posee credenciales de deployment, producción, base de datos, WhatsApp ni Gmail.
- No puede obtenerlas indirectamente: los jobs de CI que ejecutan código del PR no tienen secretos de
  deploy; el deploy vive en jobs/entornos separados y protegidos.
- Staging lo inicia `StagingTrigger` o CI tras checks en verde. Producción solo `Deployer` con `Approval`.

## Permisos por integración

### GitHub App

Privada, instalada solo en los repositorios gestionados.

| Permiso | Nivel | Motivo |
|---|---|---|
| Metadata | Read | Identificar repositorio |
| Contents | Read/Write | El broker sube ramas; el worker recibe tokens **reducidos a Read** |
| Pull requests | Read/Write | Crear y actualizar Draft PRs (broker) |
| Checks | Read | Ver resultado de CI |
| Actions | Read | Consultar ejecuciones |
| Issues | Read/Write (opcional) | Tickets vía Issues |
| **Workflows** | **Sin permiso** | El agente no puede modificar su propia red de seguridad |

**Defensa en profundidad en GitHub (Rulesets / branch protection)** — la seguridad no depende solo
de que el código de Jarvis se comporte bien:

- Rama por defecto sin pushes directos (incluida la GitHub App de Jarvis).
- PR obligatorio para integrar cambios.
- Required status checks; CI obligatorio donde corresponda.
- La GitHub App de Jarvis **no figura en la lista de bypass**.
- Sin permiso `workflows` y rutas protegidas: el worker no puede modificar sus barreras.
- Jarvis verifica estas reglas (preflight) y se niega a operar en repos que no las tengan.

Rutas protegidas (rechazadas por el guard): `.github/`, `project_manifests/`, `tests/security/`,
configuración de infraestructura.

### WhatsApp (Meta Cloud API)

- `whatsapp_business_messaging` para enviar/recibir; `whatsapp_business_management` solo para gestionar suscripciones.
- Auto-respuesta limitada a: `acknowledgement`, `request_for_debug_information`, `status_update`.
- Requiere aprobación: precios, plazos, cambios de contrato, compensaciones.
- Palabras de escalamiento (definidas en el manifest) desactivan la auto-respuesta de esa
  conversación y alertan al owner (exigido por la política de Meta).

### Gmail

- Inicial: `gmail.readonly` + `gmail.send`. `gmail.modify` solo si se justifica (etiquetado).
- Nunca contraseña SMTP; solo OAuth 2.0.

### Identidades de servicio

Independientes del proveedor; el mecanismo concreto en producción se define en la fase de deployment.

| Identidad | Credenciales | No tiene |
|---|---|---|
| `control_plane` | Canales (WhatsApp/Gmail), clave de la GitHub App (RepoBroker), base de datos de Jarvis | Credenciales de deploy de clientes |
| `coding_worker` | Token GitHub de solo lectura (1 repo, corta duración), acceso al modelo | Deploy, producción, DB, canales, otros secretos |
| `deployer` | Credenciales de deploy de proyectos; se usa solo tras `Approval` | Todo lo demás |

### Claude (coding worker)

- Herramientas explícitas según `allowed_actions` del `JobSpec`; sin Bash irrestricto.
- Límites: `max_turns`, `timeout_seconds`, `max_ai_cost_usd` y límites de recursos (architecture.md §11).
