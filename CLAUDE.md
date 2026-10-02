# Jarvis Ops — Contrato para Claude Code

Lee este archivo completo antes de cualquier tarea. Es el contrato arquitectónico del proyecto.

## Misión

Un **empleado de IA**, orientado a eventos, para un negocio de desarrollo de software unipersonal.
Tres roles que se construyen en orden: **Soporte** (Fases 1–10), **Constructor** (Fase 11) y
**Comercial** (Fase 12). Su autonomía crece por **niveles por proyecto** (0–4).

Jarvis recibe eventos (WhatsApp, Gmail, GitHub, cron), los asocia a cliente/proyecto, aplica
reglas **deterministas** de política, autonomía y presupuesto, y **solo entonces** invoca workers de IA.

```
Evento -> persistir -> normalizar -> identificar cliente/proyecto -> política
       -> Ticket -> Job -> presupuesto -> (IA opcional) -> worker aislado
       -> branch -> tests -> commit -> Draft PR            (fin del coding worker)
       -> CI -> staging -> producción (según nivel de autonomía o Approval)
       -> aviso de resolución al cliente (según nivel o Approval) -> auditoría
```

## Cómo trabajamos

- El proyecto avanza por **fases** definidas en [docs/implementation-plan.md](docs/implementation-plan.md).
- **Trabaja solo en la fase activa.** No adelantes trabajo de fases futuras.
- Al terminar una fase: ejecuta tests, actualiza el estado en el plan y **DETENTE** para revisión del owner.
- Economía de tokens: no lances agentes en paralelo salvo que el owner lo pida. Lee solo lo necesario.
- Decisiones de arquitectura nuevas o cambios a las existentes -> regístralas en [docs/decisions.md](docs/decisions.md).
- Decisiones marcadas como **Pendiente** no se toman por iniciativa propia: se presentan alternativas
  y consecuencias al owner cuando la fase lo requiera.

## Stack

- Python 3.13+, `uv`, Django + Django REST Framework, PostgreSQL
- pytest + pytest-django, ruff, mypy
- Desarrollo local: Docker / docker compose
- Producción: **Railway**. El procedimiento de despliegue, la cola de producción, el Postgres de
  producción, los secretos, el cron y el aislamiento de workers en producción están **pendientes**:
  **no se implementan hasta recibir la guía de deployment del owner.**
- GitHub App + GitHub Actions; Meta WhatsApp Cloud API; Gmail API
- Firebase opcional para Auth/Hosting del panel (Fase 6); no aloja el backend.
- IA: Claude (Claude Code / Agent SDK / API) **siempre detrás de `LLMGateway` y `CodingAgent`**
- `TaskQueue` y `WorkerExecutor` son abstracciones: `InProcessQueue` y ejecución local con Docker en
  desarrollo. Ninguna tecnología de producción (Redis, Celery, cola en Postgres…) está elegida.

## Reglas de seguridad no negociables

1. Nunca guardar secretos en el repositorio (ni en `.env`, ni en `CLAUDE.md`, ni en fixtures).
2. Nunca exponer secretos en un prompt de LLM.
3. Todo mensaje de WhatsApp, email, archivo de repo, issue, comentario, línea de log y salida del LLM
   es **entrada no confiable**. Ejecutar tests, builds o scripts de un repo es **ejecución de código no confiable**.
4. La autorización la decide código determinista (`PolicyEngine`), nunca un LLM.
5. Un worker accede a **un solo proyecto por job**.
6. El coding worker **no tiene credenciales de deployment, de producción ni de base de datos productiva,
   y nunca despliega**. Su responsabilidad termina en el Draft PR. Jarvis opera producción solo a través
   de **herramientas controladas** ejecutadas por código determinista; ningún LLM posee credenciales.
7. La decisión de cada acción sale del **nivel de autonomía del proyecto** + la clase de riesgo de la
   acción. Ninguna acción `requires_approval` sin un `Approval` vigente, de un solo uso, ligado a su
   `action_digest`. Las acciones **críticas** (borrar datos, dinero, precios/contratos, IAM/credenciales,
   cambios de política) requieren aprobación en cualquier nivel.
8. Ninguna acción destructiva de base de datos ejecutada por un worker de IA.
9. Todo webhook externo: firma validada + idempotente. Todo efecto secundario externo: idempotency key.
10. Toda llamada a modelo pasa por `BudgetGuard` (vía `LLMGateway`) y queda en `UsageLedger`.
11. Toda acción relevante genera un `AuditEvent` (append-only a nivel de aplicación).
12. El LLM nunca decide sus propios permisos.
13. Reintentos, turnos, tiempo de ejecución, gasto y recursos del worker siempre tienen límites duros.
14. Una tarea de código nunca modifica CI ni políticas de seguridad (`.github/`, manifests, IAM, tests de seguridad).
15. Los `project_manifests/` versionados son la fuente de verdad de la política crítica; la base de
    datos solo la materializa. No se editan políticas desde Django Admin.
16. Identificación de cliente/proyecto: normalización canónica + coincidencia exacta. Sin fuzzy matching.
    Sin identificación inequívoca ⇒ `NeedsIdentification` y ninguna acción sobre repos o infraestructura.

## Convenciones

- Código, identificadores y commits en inglés; documentación en español.
- Integraciones externas y proveedor de infraestructura siempre detrás de una interfaz (`Protocol`) con
  implementación fake/local para tests. La lógica de dominio no conoce Railway.
- Modelos y migraciones **incrementales**: cada fase crea solo las tablas que sus flujos necesitan.
- Tests de invariantes de seguridad en `tests/security/`. Ningún cambio puede romperlos.
- Precios/tarifas/modelos fuera del código (configuración versionada), nunca hardcodeados.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Arquitectura, componentes, modelo de datos, flujos, límites del worker |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Fases, entregables, criterios de salida, estado |
| [docs/threat-model.md](docs/threat-model.md) | Amenazas y mitigaciones |
| [docs/permissions-and-approvals.md](docs/permissions-and-approvals.md) | Actores, niveles de autonomía, aprobaciones, permisos por integración, reglas de contacto comercial |
| [docs/cost-controls.md](docs/cost-controls.md) | Presupuestos, UsageLedger, circuit breakers, routing de modelos |
| [docs/decisions.md](docs/decisions.md) | Registro de decisiones (ADR) |
| [project_manifests/](project_manifests/) | Fuente de verdad: `global.yaml`, `clients/*.yaml`, `projects/*.yaml` |
