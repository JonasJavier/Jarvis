# Permisos y aprobaciones

## Niveles de decisión

`PolicyEngine` devuelve uno de tres resultados. **Deny by default:** una acción no declarada es `forbidden`.

| Nivel | Significado |
|---|---|
| `autonomous` | Jarvis la ejecuta y la audita |
| `requires_approval` | Se crea un `Approval`; no se ejecuta hasta que el owner aprueba |
| `forbidden` | Nunca se ejecuta, ni con aprobación desde el flujo automático |

## Matriz de autonomía

| Acción | MVP | Objetivo a futuro |
|---|---|---|
| Leer mensajes y crear tickets | autonomous | autonomous |
| Identificar cliente/proyecto (coincidencia exacta) | autonomous | autonomous |
| Consultar logs sanitizados | autonomous | autonomous |
| Ejecutar tests / lint / scanners | autonomous | autonomous |
| Crear rama y modificar código en rama | autonomous | autonomous |
| Abrir Draft PR | autonomous | autonomous |
| Responder acuse / pedir info / estado | autonomous (con `OutboundPolicy`) | autonomous |
| Clasificar si está cubierto por mantenimiento | regla contractual (+ IA auxiliar) | igual |
| Deploy a staging | autonomous | autonomous |
| Merge a rama principal | requires_approval | autonomous solo para riesgo muy bajo |
| Deploy a producción | requires_approval | según política por proyecto |
| Migraciones de base de datos | requires_approval | requires_approval |
| IAM / rotación de credenciales | requires_approval | requires_approval |
| Operaciones financieras | requires_approval | requires_approval |
| Cotizaciones, plazos, cambios de contrato | requires_approval | requires_approval |
| Exponer secretos, desactivar auditoría, acceder a otro cliente | forbidden | forbidden |
| Modificar CI / políticas de seguridad desde una tarea | forbidden | forbidden |

## Reglas de `Approval`

- Ligado a **un** target concreto (proyecto + acción + objeto, p. ej. PR #238 / commit SHA).
- Tiene vencimiento; si el target cambia (nuevo commit), el `Approval` deja de valer.
- Registra quién aprobó, cuándo y desde dónde; genera `AuditEvent`.
- Un `Approval` no se puede crear ni aprobar desde un worker ni desde un mensaje de cliente.

## Permisos por integración

### GitHub App

Privada, instalada solo en los repositorios gestionados.

| Permiso | Nivel | Motivo |
|---|---|---|
| Metadata | Read | Identificar repositorio |
| Contents | Read/Write | Leer código, subir ramas |
| Pull requests | Read/Write | Crear y actualizar PRs |
| Checks | Read | Ver resultado de CI |
| Actions | Read | Consultar ejecuciones |
| Issues | Read/Write (opcional) | Tickets vía Issues |
| **Workflows** | **Sin permiso** | El agente no puede modificar su propia red de seguridad |

Rutas protegidas (rechazadas por el guard aunque GitHub lo permitiera): `.github/`, archivos de
política de Jarvis, `tests/security/`, configuración de infraestructura.

### WhatsApp (Meta Cloud API)

- `whatsapp_business_messaging` para enviar/recibir; `whatsapp_business_management` solo para gestionar suscripciones.
- Auto-respuesta limitada a: `acknowledgement`, `request_for_debug_information`, `status_update`.
- Requiere aprobación: precios, plazos, cambios de contrato, compensaciones.
- Palabras de escalamiento ("hablar con Jonas", "necesito una persona", "urgente") desactivan la
  auto-respuesta de esa conversación y alertan al owner (exigido por la política de Meta).

### Gmail

- Inicial: `gmail.readonly` + `gmail.send`. `gmail.modify` solo si se justifica (etiquetado).
- Nunca contraseña SMTP; solo OAuth 2.0.

### Google Cloud (Fase 4+)

| Service account | Puede | No puede |
|---|---|---|
| `jarvis-api` | Leer secretos de canales, encolar tareas, lanzar jobs | Desplegar, leer credencial de IA del worker |
| `jarvis-coder` | Obtener token GitHub vía broker, leer credencial de IA | WhatsApp, Gmail, DB prod, otros secretos |
| `jarvis-deployer` | Desplegar servicios de clientes | Todo lo demás; solo se usa tras `Approval` |

Acceso a Secret Manager concedido **por secreto**, solo lectura, nunca a nivel de proyecto.

### Claude (worker)

- Herramientas explícitas por `allowed_actions` del `JobSpec`; sin Bash irrestricto.
- Sin variables de entorno con secretos más allá de la credencial de IA.
- Límites: `max_turns`, `timeout_seconds`, `max_ai_cost_usd`.
