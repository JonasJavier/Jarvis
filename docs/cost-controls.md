# Control de costos

## Principio

Jarvis no consume tokens por estar encendido. Todo se despierta por eventos (webhooks, Pub/Sub,
Cloud Scheduler); la infraestructura HTTP escala a cero y los workers existen solo durante el job.

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
| Clasificación simple, resumen de ticket | Modelo económico (familia Haiku) |
| Código | Modelo de coding configurado |
| Diagnóstico difícil | Escalar a modelo superior, con aprobación de presupuesto |
| Auditoría mensual masiva | Batch API cuando aplique |

Reutilizar prompt caching para contenido estable (política global, contexto del proyecto).
El contexto de un job incluye solo: política global + política del cliente + contexto del
proyecto + ticket + archivos relevantes.

**Precios:** las tarifas por modelo se guardan como configuración y se verifican contra la
documentación oficial de Anthropic al activar cada fase. No se hardcodean.

## Presupuestos

Jerarquía; se aplica el límite más restrictivo.

```yaml
budgets:
  global:  { daily_usd: 15, monthly_usd: 200 }
  client:  { daily_usd: 8,  monthly_usd: 75 }   # por defecto; el manifest puede sobrescribir
  job:     { max_usd: 3 }
limits:
  concurrent_ai_jobs: 2
  max_turns: 12
  max_retries: 1
```

`LLMGateway` hace `reserve(estimado)` antes de invocar y `reconcile(real)` después. Si la reserva
falla ⇒ `BudgetExceeded` y el job pasa a `BlockedBudget`.

## Umbrales y circuit breaker

| Consumo | Acción |
|---|---|
| 50 % | Registro |
| 70 % | Visible en el panel |
| 80 % | Aviso al owner (WhatsApp/email) |
| 90 % | Bloquear tareas de baja prioridad |
| 100 % | **Hard stop**: pausar todos los jobs IA del alcance + alerta |

## Barreras externas (defensa en profundidad)

1. Límites internos de `BudgetGuard` (primera barrera).
2. Si se usa Claude API: créditos prepagados con **auto-reload desactivado**.
3. Alertas de presupuesto de facturación de GCP.
4. Límites de infraestructura: timeout y retries de Cloud Run Jobs; concurrencia máxima.

## Suscripción Claude Max vs. API

La suscripción Max sirve para desarrollar Jarvis y para la primera etapa. Para operación comercial
predecible se prepara el `LLMGateway` para usar la API. Las condiciones de uso de la suscripción
con Agent SDK / `claude -p` han cambiado en 2026 y deben **verificarse al iniciar la Fase 3**
(ver ADR-005).

## WhatsApp

No se asume que los mensajes de servicio sean gratuitos. Las tarifas de Meta están en proceso de
cambio (anuncio para 1 de octubre de 2026): se guardan como configuración y se consulta el rate
card oficial al iniciar la Fase 5. Se cuentan los mensajes salientes por cliente.

## Presupuesto operativo inicial (orientativo)

| Partida | Estimación mensual |
|---|---|
| Claude Max | US$100 |
| GCP / Firebase | US$10–40 (el Postgres gestionado suele ser el mayor fijo, ver ADR-006) |
| Claude API (experimentación) | US$0–50 con hard cap |
| WhatsApp | Variable según tarifas vigentes |

Medir **coste por cliente, por ticket y por PR**, no solo tokens totales.

## Alertas

| Señal | Warning | Crítica |
|---|---|---|
| Presupuesto diario IA | 80 % | 100 % → corte |
| Fallos consecutivos de worker | 2 | 3 |
| Antigüedad de la cola | > 5 min | > 15 min |
| Mensajes salientes/minuto | anómalo | cortar outbound |
| Runtime del job | 80 % del timeout | cancelar |
