# Jarvis Ops

Un **empleado de IA** para un negocio de desarrollo de software: atiende clientes, mantiene y
construye proyectos y, con el tiempo, busca nuevos clientes. Su autonomía crece por niveles a medida
que demuestra que hace bien su trabajo.

## Roles

| Rol | Qué hace | Fase |
|---|---|---|
| **Soporte** | Recibe un reporte de error, responde, crea el ticket, arregla, prueba, publica y avisa al cliente en lenguaje natural | 1–10 |
| **Constructor** | Convierte una idea en un MVP: requisitos, código, QA, publicación y conversación con el cliente | 11 |
| **Comercial** | Investiga clientes potenciales, prepara propuestas y demos, y los contacta respetando las reglas de cada canal | 12 |

## Cómo funciona

No es un chatbot con acceso a tu computadora. Es un **plano de control** determinista
(Django + PostgreSQL) que recibe eventos, aplica política y presupuesto, y lanza **workers
aislados** que usan Claude solo cuando hace falta. Jarvis actúa en producción mediante
**herramientas controladas**, nunca con credenciales en manos del modelo.

## Principios

1. **Event-driven:** ningún modelo consume tokens por estar "encendido".
2. **Determinismo primero:** reglas, identificación, permisos y presupuesto sin LLM.
3. **Aislamiento:** un job = un proyecto; el código se escribe en un entorno sin credenciales de producción.
4. **Autonomía por niveles:** cada proyecto tiene un nivel (0–4) que el owner sube con evidencia.
   Algunas acciones (borrar datos, dinero, contratos, credenciales) requieren siempre su aprobación.
5. **Todo auditable:** cada acción se puede reconstruir desde el mensaje que la originó.

## Estado

Fases 1A (scaffold), 1B (núcleo de seguridad), 2 (GitHub App) y 3 (coding worker en sandbox Docker)
completadas y probadas en real contra un repositorio de prueba: **prototipo local** alcanzado. La
Fase 3b añade el proxy del `LLMGateway` y Claude Code dentro del sandbox (ADR-005 opción A,
ADR-033), probado en real: Claude arregló un bug del repo de prueba desde la caja. Fase 4: Jarvis
vive en Railway (ADR-034). Fase 5: WhatsApp Cloud API con `client_agent` y política de salida
(ADR-035), verificada con el proveedor falso; pendiente de conectar la app de Meta. Ver el
[plan de implementación](docs/implementation-plan.md).

Desarrollo local con Docker; producción en Railway (`jarvis-ops`).

## Desarrollo local

Requisitos: Docker y [uv](https://docs.astral.sh/uv/).

```bash
docker compose up --build          # API en http://localhost:8000 + Postgres en el puerto 55432
uv sync                            # entorno de desarrollo
uv run pytest                      # tests (requiere el Postgres de compose en marcha)
uv run ruff check . && uv run mypy apps/api tests
```

Comandos de política (desde `apps/api/`):

```bash
uv run python manage.py load_manifests     # valida y carga project_manifests/
uv run python manage.py check_manifests    # falla si la base de datos difiere de los manifests
```

## Documentación

- [Arquitectura](docs/architecture.md)
- [Plan de implementación por fases](docs/implementation-plan.md)
- [Modelo de amenazas](docs/threat-model.md)
- [Permisos, autonomía y aprobaciones](docs/permissions-and-approvals.md)
- [Control de costos](docs/cost-controls.md)
- [Registro de decisiones](docs/decisions.md)
- [Despliegue en Railway](docs/railway.md)
- [Contrato para Claude Code](CLAUDE.md)
