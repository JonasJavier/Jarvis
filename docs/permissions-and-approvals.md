# Permisos, autonomía y aprobaciones

## Niveles de decisión

`PolicyEngine.evaluate(actor, action, project)` devuelve uno de tres resultados.
**Deny by default:** una acción no declarada en el catálogo es `forbidden`.

| Resultado | Significado |
|---|---|
| `autonomous` | Jarvis la ejecuta y la audita |
| `requires_approval` | Se crea un `Approval`; no se ejecuta hasta que el owner aprueba |
| `forbidden` | Nunca se ejecuta, ni con aprobación desde el flujo automático |

Entradas de la decisión, en orden de prioridad:

1. **Prohibiciones globales** (`global.yaml`) → `forbidden`.
2. **Techo de capacidades del actor** (código): un actor nunca ejecuta acciones fuera de su techo
   (p. ej. el `coding_worker` jamás despliega).
3. **Acciones críticas** → siempre `requires_approval`, en cualquier nivel.
4. **Nivel de autonomía del proyecto × clase de riesgo de la acción** → `autonomous` si el nivel la
   cubre; si no, `requires_approval`.
5. **Restricciones del manifest del proyecto** (`restrict`): pueden volver más restrictiva una acción, nunca menos.

## Niveles de autonomía

Cada proyecto declara `autonomy_level` en su manifest (fuente de verdad versionada). Subir de nivel es
un commit revisado por el owner, respaldado por evidencia (Fase 10).

| Nivel | Nombre | Autónomo hasta la clase |
|---|---|---|
| 0 | Observa | `read` |
| 1 | Propone | `internal` |
| 2 | Actúa en lo seguro | `low` |
| 3 | Actúa e informa | `medium` |
| 4 | Autónomo | `high` (solo proyectos `ownership: internal`, salvo ADR explícita) |

**Nivel inicial recomendado:** proyectos de clientes en nivel 2; proyectos internos según confianza.

## Clases de riesgo (catálogo en código)

| Clase | Acciones |
|---|---|
| `read` | Leer repositorio; leer logs, métricas y errores de producción **sanitizados**; consultar CI |
| `internal` | Crear tickets; modificar worktree; commit; crear branch y Draft PR; ejecutar tests/lint/QA |
| `low` | Deploy a staging; mensajes de acuse, solicitud de información y estado |
| `medium` | **Aviso de resolución al cliente**; merge a rama principal con CI en verde; deploy a producción con CI en verde y rollback automático; rollback; restart de servicios; mensajes dentro de una campaña comercial aprobada |
| `high` | Migraciones **no destructivas** con backup previo; cambios de configuración de producción no sensibles; conversaciones técnicas de alcance con el cliente; crear repositorio desde plantilla |
| `critical` | **Siempre requiere aprobación:** operaciones destructivas sobre datos; migraciones destructivas; dinero y operaciones financieras; precios, cotizaciones, plazos y contratos; IAM, credenciales y secretos; cambios de manifests, políticas o nivel de autonomía; lanzar una campaña comercial; borrar repositorios o servicios |
| `forbidden` | Exponer secretos; desactivar auditoría; acceder a otro cliente; que una tarea modifique CI o políticas; cualquier deploy por el coding worker |

## Ejemplo: flujo de un error en un proyecto de cliente (nivel 2)

| Paso | Decisión |
|---|---|
| Responder "lo estamos revisando" | autónomo (`low`) |
| Crear ticket, arreglar en branch, Draft PR, tests | autónomo (`internal`) |
| Deploy a staging | autónomo (`low`) |
| Deploy a producción | aprobación (`medium` > nivel 2) |
| Aviso de resolución en lenguaje natural | aprobación (`medium` > nivel 2) |

En nivel 3, el mismo flujo es completamente autónomo: Jarvis publica, verifica, avisa al cliente e
informa al owner. Borrar datos o hablar de precios seguiría requiriendo aprobación.

## Reglas de `Approval`

**Ligadura.** Cada aprobación está ligada a un `action_digest` inmutable:

```
action_digest = sha256(JSON canónico de {
  project_id, action, target/environment,
  subject_ref (commit SHA, artifact digest o hash del contenido del mensaje),
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
  el target, el commit, el artifact, el texto del mensaje, los parámetros o la política, no coincide y
  la aprobación queda inválida.
- Estados: `pending → approved → used`, o `rejected` / `expired` / `invalidated`.
- Registra quién aprobó, cuándo y desde dónde; cada transición genera `AuditEvent`.
- Un `Approval` no se puede crear, aprobar ni consumir desde un agente LLM ni desde un mensaje de cliente.

## Acceso a producción

Jarvis sí opera producción, igual que lo haría un empleado: con acceso progresivo y herramientas
concretas, no con las llaves maestras.

- **Lectura (desde el inicio):** logs, métricas y errores sanitizados. Datos de la base productiva
  solo con herramientas acotadas de solo lectura y minimización de datos personales.
- **Escritura:** únicamente mediante el catálogo cerrado de `ProductionTools` (deploy, rollback,
  restart, migración con backup, cambios de configuración no sensibles). Las ejecuta el `Deployer`,
  código determinista; el `ops_agent` (LLM) solo las solicita.
- **Nunca:** shell libre en producción, credenciales en el contexto o entorno de un LLM.
- **Salvaguardas:** backup previo a operaciones de riesgo, health checks y rollback automático,
  límite de operaciones de producción por hora, alerta al owner de cada acción autónoma en producción.

## Separación del coding worker

- Responsabilidad: `clone → branch → cambios → tests → commit → Draft PR`. Nada más.
- No posee credenciales de deployment, producción, base de datos, WhatsApp ni Gmail.
- No puede obtenerlas indirectamente: los jobs de CI que ejecutan código del PR no tienen secretos de
  deploy; el deploy vive en jobs/entornos separados y protegidos.
- Staging lo inicia `StagingTrigger` o CI tras checks en verde. Producción solo el `Deployer`.

## Conversación con clientes

- El `client_agent` responde en lenguaje natural, breve, humano y sencillo, en el idioma del cliente.
- Cada mensaje se clasifica (acuse, info, estado, resolución, alcance técnico, comercial) y su clase
  de riesgo decide si se envía directo o como borrador para aprobación.
- Compromisos de precio, plazo, alcance contractual o compensación: siempre `critical`.
- Palabras de escalamiento (definidas en el manifest) desactivan la auto-respuesta de esa
  conversación y alertan al owner (exigido por la política de Meta).

## Contacto comercial

- **Campañas:** el owner aprueba cada campaña (público objetivo, plantilla de mensaje, canal, volumen
  máximo). Los mensajes dentro de una campaña aprobada son `medium`.
- **WhatsApp:** solo con contactos que dieron consentimiento (registrado en `ConsentRecord`), desde un
  **número comercial dedicado**, nunca el de soporte ni un número personal.
- **Email:** primer contacto permitido cumpliendo normas anti-spam: remitente identificado, opción de
  baja respetada, volumen limitado.
- **LinkedIn / redes:** Jarvis redacta; el owner envía los mensajes privados. Se automatiza solo lo
  que permiten las APIs oficiales (p. ej. publicar con `w_member_social`).
- **Datos de prospectos:** mínimos, de fuentes públicas de empresas, con retención limitada.

## Permisos por integración

### GitHub App

Privada, instalada solo en los repositorios gestionados.

| Permiso | Nivel | Motivo |
|---|---|---|
| Metadata | Read | Identificar repositorio |
| Contents | Read/Write | El broker sube ramas; el worker recibe tokens **reducidos a Read** |
| Pull requests | Read/Write | Crear y actualizar PRs (broker) |
| Checks | Read | Ver resultado de CI |
| Actions | Read | Consultar ejecuciones |
| Issues | Read/Write (opcional) | Tickets vía Issues |
| **Workflows** | **Sin permiso** | El agente no puede modificar su propia red de seguridad |
| **Administration** | **Sin permiso** | Crear/borrar repos no es automático (ver Fase 11) |

**Defensa en profundidad en GitHub (Rulesets / branch protection)** — la seguridad no depende solo
de que el código de Jarvis se comporte bien:

- Rama por defecto sin pushes directos (incluida la GitHub App de Jarvis).
- PR obligatorio para integrar cambios.
- Required status checks; CI obligatorio donde corresponda.
- La GitHub App de Jarvis **no figura en la lista de bypass**.
- Sin permiso `workflows` y rutas protegidas: el worker no puede modificar sus barreras.
- Jarvis verifica estas reglas (preflight) y se niega a operar en repos que no las tengan.

Cuando el nivel de autonomía permite el merge, lo ejecuta el broker vía PR una vez cumplidos los
required checks, sin saltarse el Ruleset.

Rutas protegidas (rechazadas por el guard): `.github/`, `project_manifests/`, `tests/security/`,
configuración de infraestructura.

### WhatsApp (Meta Cloud API)

- `whatsapp_business_messaging` para enviar/recibir; `whatsapp_business_management` solo para gestionar suscripciones.
- Número dedicado para soporte de clientes. Para el rol Comercial, número separado (Fase 12).

### Gmail

- Inicial: `gmail.readonly` + `gmail.send`. `gmail.modify` solo si se justifica (etiquetado).
- Nunca contraseña SMTP; solo OAuth 2.0.

### Identidades de servicio

Independientes del proveedor; el mecanismo concreto en producción se define en la fase de deployment.

| Identidad | Credenciales | No tiene |
|---|---|---|
| `control_plane` | Canales (WhatsApp/Gmail), clave de la GitHub App (RepoBroker), base de datos de Jarvis | Credenciales de producción de clientes |
| `coding_worker` | Token GitHub de solo lectura (1 repo, corta duración), acceso al modelo | Deploy, producción, DB, canales, otros secretos |
| `deployer` | Credenciales de producción de proyectos, usadas solo por `ProductionTools` | Todo lo demás |

### Claude (agentes)

- Herramientas explícitas según `allowed_actions`; sin Bash irrestricto; sin credenciales.
- Límites: `max_turns`, `timeout_seconds`, `max_ai_cost_usd` y límites de recursos (architecture.md §11).
