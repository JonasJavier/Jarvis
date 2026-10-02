# Control de costos

## Alcance

Los controles de presupuesto se centran en servicios cuyo coste depende del uso: **IA (Claude/API),
WhatsApp y APIs externas de pago**. Los recursos de cómputo de Railway no se optimizan agresivamente
por coste; se limitan por **seguridad y estabilidad** (ver architecture.md §11).

## Principio

Jarvis no consume tokens por estar encendido. Todo se activa por eventos (webhooks, notificaciones,
tareas programadas); ningún modelo hace polling ni conversa en segundo plano.

## Qué NO usa IA

Verificación de webhooks · deduplicación · identificación por teléfono/email · búsqueda de
`project_id` · horarios · consulta de presupuestos · cron · tests · health checks · creación de
ramas · espera de CI · mensajes triviales ("gracias", "ok", "perfecto").

## Qué SÍ usa IA

Interpretar descripciones ambiguas · diagnosticar stack traces · relacionar logs con código ·
modificar código · explicar fallos de CI · clasificar bug vs. feature cuando no es obvio ·
redactar respuestas técnicas · resumir mantenimiento.

## Routing de modelos

El modelo se elige por tipo de tarea y se configura fuera del código (`ANTHROPIC_MODEL_*`).

| Trabajo | Estrategia |
|---|---|
| Reglas, routing, dedupe | Sin LLM |
| Clasificación simple, resumen de ticket | Modelo económico |
| Código | Modelo de coding configurado |
| Diagnóstico difícil | Escalar a modelo superior, dentro del presupuesto |
| Auditoría mensual masiva | Batch API cuando aplique |

Prompt caching para contenido estable (política global, contexto del proyecto). El contexto de un job
incluye solo: política global + del cliente + del proyecto + ticket + archivos relevantes.

## Precios versionados

Las tarifas (por modelo, por mensaje de WhatsApp, por API) viven en un catálogo de precios versionado
fuera del código, cada entrada con su `pricing_version`. Se verifican contra la documentación oficial
de cada proveedor al activar la fase correspondiente. Nunca se hardcodean.

## UsageLedger

Registro append-only de cada consumo real. Campos mínimos:

| Campo | Descripción |
|---|---|
| `provider`, `service` | p. ej. `anthropic`/`llm`, `meta`/`whatsapp` |
| `model` | Modelo usado (si aplica) |
| `input_tokens`, `output_tokens` | Tokens facturados |
| `cache_read_tokens`, `cache_write_tokens` | Cuando aplique |
| `units` | Para servicios no basados en tokens (mensajes, llamadas) |
| `cost_usd` | Coste real calculado |
| `pricing_version` | Snapshot de precios usado para el cálculo |
| `client`, `project`, `job`, `job_run`, `correlation_id` | Atribución |
| `created_at` | Timestamp |

Permite medir **coste por cliente, por proyecto, por ticket y por PR**, no solo tokens totales.

## Presupuestos

Scopes jerárquicos: **global → client → project → job**. Fuente de verdad: manifests versionados
(`global.yaml`, `clients/*.yaml`, `projects/*.yaml`); se aplica el límite más restrictivo.

```yaml
# global.yaml (techos)                  # clients/<id>.yaml           # projects/<id>.yaml
budgets:                                budget:                       budget:
  daily_usd: 15                           daily_usd: 8                  max_ai_usd_per_run: 3.00
  monthly_usd: 200                        monthly_usd: 75               daily_usd: 5
limits:                                                                 monthly_usd: 40
  concurrent_ai_jobs: 2
```

**Reserva.** `LLMGateway` (y el Outbound Gateway para WhatsApp) llama a `BudgetGuard.reserve(estimado)`
antes de consumir:

1. Una transacción bloquea las filas de `Budget` en **orden fijo `global → client → project → job`**
   (`select_for_update`). El orden fijo evita deadlocks entre reservas concurrentes.
2. Si cualquier scope no tiene saldo suficiente, la reserva completa falla ⇒ `BudgetExceeded` y el
   job pasa a `BlockedBudget`.
3. Si todos tienen saldo, se crea una `BudgetReservation` por scope.

**Conciliación.** Tras el consumo, `reconcile(real)` escribe en `UsageLedger`, mueve el importe de
reservado a gastado en cada scope y libera la diferencia. Reservas no conciliadas expiran y se liberan.

Los periodos diarios y mensuales usan la zona horaria definida en `global.yaml`.

## Umbrales y circuit breaker

| Consumo | Acción |
|---|---|
| 50 % | Registro |
| 70 % | Visible en el panel |
| 80 % | Aviso al owner (WhatsApp/email) |
| 90 % | Bloquear tareas de baja prioridad |
| 100 % | **Hard stop**: pausar todo consumo de pago del scope + alerta |

## Barreras externas (defensa en profundidad)

1. Límites internos de `BudgetGuard` (primera barrera).
2. Claude API: créditos prepagados con **auto-reload desactivado**.
3. Límites o alertas de facturación que ofrezca cada proveedor de pago por uso (Anthropic, Meta).
4. Límites de ejecución del worker (turnos, tiempo, reintentos), que también acotan el gasto.

## Suscripción Claude Max vs. API

La suscripción Max sirve para desarrollar Jarvis y para la primera etapa. Para operación comercial
predecible se prepara el `LLMGateway` para usar la API. Las condiciones de uso de la suscripción con
Agent SDK / `claude -p` han cambiado durante 2026 y deben **verificarse al iniciar la Fase 3** (ADR-005).

## WhatsApp

No se asume que los mensajes de servicio sean gratuitos: Meta anunció cambios de tarifas a partir de
octubre de 2026. Las tarifas se guardan en el catálogo de precios y se consulta el rate card oficial al
iniciar la Fase 5. Cada mensaje saliente se reserva en `BudgetGuard` y se registra en `UsageLedger`.

## Presupuesto operativo inicial (orientativo)

| Partida | Estimación mensual |
|---|---|
| Claude Max | US$100 |
| Railway | Plan existente del owner (no se optimiza agresivamente) |
| Firebase (si se usa en Fase 6) | Bajo a este volumen |
| Claude API (experimentación) | US$0–50 con hard cap |
| WhatsApp | Variable según tarifas vigentes |

## Alertas

| Señal | Warning | Crítica |
|---|---|---|
| Presupuesto diario IA | 80 % | 100 % → corte |
| Fallos consecutivos de worker | 2 | 3 |
| Antigüedad de la cola | > 5 min | > 15 min |
| Mensajes salientes/minuto | anómalo | cortar outbound |
| Runtime del job | 80 % del timeout | cancelar |
