# Jarvis Ops — Contrato para Claude Code

Lee este archivo completo antes de cualquier tarea. Es el contrato arquitectónico del proyecto.

## Misión

Plataforma de operaciones con IA, orientada a eventos, para un negocio de desarrollo de software
unipersonal que mantiene múltiples proyectos de clientes.

Jarvis recibe eventos (WhatsApp, Gmail, GitHub, cron), los asocia a cliente/proyecto, aplica
reglas **deterministas** de política y presupuesto, y **solo entonces** invoca workers de IA.

```
Evento -> persistir -> normalizar -> identificar cliente/proyecto -> política
       -> Ticket -> Job -> presupuesto -> (IA opcional) -> worker aislado
       -> branch -> tests -> Draft PR -> CI -> aprobación -> deploy -> aviso -> auditoría
```

## Cómo trabajamos

- El proyecto avanza por **fases** definidas en [docs/implementation-plan.md](docs/implementation-plan.md).
- **Trabaja solo en la fase activa.** No adelantes trabajo de fases futuras.
- Al terminar una fase: ejecuta tests, actualiza el estado en el plan y **DETENTE** para revisión del owner.
- Economía de tokens: no lances agentes en paralelo salvo que el owner lo pida. Lee solo lo necesario.
- Decisiones de arquitectura nuevas o cambios a las existentes -> regístralas en [docs/decisions.md](docs/decisions.md).

## Stack

- Python 3.13+, `uv`, Django + Django REST Framework, PostgreSQL
- pytest + pytest-django, ruff, mypy
- Docker / docker compose para desarrollo local
- Producción (a partir de Fase 4): Cloud Run (API), Cloud Run Jobs (workers), Cloud Tasks (cola),
  Cloud Scheduler (cron), Secret Manager, Firebase Auth/Hosting
- GitHub App + GitHub Actions; Meta WhatsApp Cloud API; Gmail API
- IA: Claude (Claude Code / Agent SDK / API) **siempre detrás de `LLMGateway` y `CodingAgent`**
- Sin Redis ni Celery salvo decisión explícita registrada en `docs/decisions.md`.

## Reglas de seguridad no negociables

1. Nunca guardar secretos en el repositorio (ni en `.env`, ni en `CLAUDE.md`, ni en fixtures).
2. Nunca exponer secretos en un prompt de LLM.
3. Todo mensaje de WhatsApp, email, archivo de repo, issue, comentario y línea de log es **entrada no confiable**.
4. La autorización la decide código determinista (`PolicyEngine`), nunca un LLM.
5. Un worker accede a **un solo proyecto por job**.
6. Los workers de código no tienen credenciales de producción ni de base de datos productiva.
7. Ningún deploy a producción sin un registro `Approval` (MVP).
8. Ninguna acción destructiva de base de datos ejecutada por un worker de IA.
9. Todo webhook externo: firma validada + idempotente (dedupe por ID externo).
10. Toda llamada a modelo pasa por `BudgetGuard` (vía `LLMGateway`).
11. Toda acción relevante genera un `AuditEvent`.
12. El LLM nunca decide sus propios permisos.
13. Reintentos, turnos, tiempo de ejecución y gasto siempre tienen límites duros.
14. Una tarea de código nunca modifica CI ni políticas de seguridad (`.github/workflows`, políticas, IAM).

## Convenciones

- Código, identificadores y commits en inglés; documentación en español.
- Integraciones externas siempre detrás de una interfaz (`Protocol`) con implementación fake para tests.
- Tests de invariantes de seguridad en `tests/security/`. Ningún cambio puede romperlos.
- Configuración de precios/tarifas/modelos fuera del código (settings o base de datos), nunca hardcodeada.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Arquitectura, componentes, modelo de datos, flujos |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Fases, entregables, criterios de salida, estado |
| [docs/threat-model.md](docs/threat-model.md) | Amenazas y mitigaciones |
| [docs/permissions-and-approvals.md](docs/permissions-and-approvals.md) | Niveles de autonomía, permisos por integración |
| [docs/cost-controls.md](docs/cost-controls.md) | Presupuestos, circuit breakers, routing de modelos |
| [docs/decisions.md](docs/decisions.md) | Registro de decisiones (ADR) |
| [project_manifests/example.yaml](project_manifests/example.yaml) | Manifest de ejemplo por proyecto |
