# Jarvis Ops

Sistema de operaciones con agentes de IA para un negocio de desarrollo de software que mantiene
múltiples proyectos de clientes.

> **Objetivo de la primera versión:** Jarvis puede recibir cualquier incidencia, identificar
> correctamente qué cliente y proyecto afecta, diagnosticarla en un entorno aislado, preparar y
> verificar una solución, mantener informado al cliente y traer al owner únicamente las
> decisiones que realmente requieren su autoridad.

No es un chatbot con acceso a tu computadora. Es un **plano de control** determinista
(Django + PostgreSQL) que recibe eventos, aplica política y presupuesto, y lanza **workers
efímeros y aislados** que usan Claude solo cuando hace falta.

## Principios

1. **Event-driven:** ningún modelo consume tokens por estar "encendido".
2. **Determinismo primero:** reglas, routing, dedupe, permisos y presupuesto sin LLM.
3. **Aislamiento:** un job = un proyecto = un contenedor temporal sin secretos de producción.
4. **PR-first / approval-first:** la IA propone, CI verifica, el owner aprueba lo que importa.
5. **Autonomía gradual:** una acción pasa a automática solo con evidencia acumulada.
6. **Todo auditable:** cada acción se puede reconstruir desde el mensaje que la originó.

## Estado

Fase actual: **Fase 0 — Documentación y fundamentos** (ver [plan de implementación](docs/implementation-plan.md)).

## Documentación

- [Arquitectura](docs/architecture.md)
- [Plan de implementación por fases](docs/implementation-plan.md)
- [Modelo de amenazas](docs/threat-model.md)
- [Permisos y aprobaciones](docs/permissions-and-approvals.md)
- [Control de costos](docs/cost-controls.md)
- [Registro de decisiones](docs/decisions.md)
- [Contrato para Claude Code](CLAUDE.md)
