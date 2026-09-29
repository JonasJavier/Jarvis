# Registro de decisiones (ADR)

Formato: contexto breve → decisión → consecuencias. Estado: `Aceptada`, `Propuesta` (pendiente de
revisión del owner) o `Pendiente` (se decide en la fase indicada).

---

## ADR-001 — Arquitectura híbrida cloud-first
**Estado:** Propuesta

**Contexto:** Jarvis debe atender WhatsApp, email, webhooks y cron 24/7 aunque el PC esté apagado.
**Decisión:** plano de control en Google Cloud (Cloud Run, escala a cero) y workers efímeros
(Cloud Run Jobs). Worker local opcional en el backlog. Desarrollo local con Docker hasta la Fase 4.
**Consecuencias:** más servicios que aprender; a cambio, alta disponibilidad sin mantener servidores.

## ADR-002 — Django + DRF + PostgreSQL como control plane
**Estado:** Propuesta

**Decisión:** el owner ya domina Django/DRF; PostgreSQL es la fuente de verdad de clientes,
contratos, tickets, jobs, presupuestos y auditoría. Firebase se usa solo para Auth y Hosting del panel.
**Consecuencias:** Firebase no es el backend; los trabajos largos nunca viven en un request web.

## ADR-003 — Sin Redis ni Celery; cola abstracta
**Estado:** Propuesta

**Decisión:** interfaz `TaskQueue` con `InProcessQueue` (dev/tests) y `CloudTasksQueue` (prod).
**Consecuencias:** menos infraestructura que operar y pagar; la lógica de negocio no depende de la cola.

## ADR-004 — Integraciones detrás de interfaces con fakes
**Estado:** Propuesta

**Decisión:** GitHub, WhatsApp, Gmail, Firebase y Claude se implementan detrás de `Protocol`s con
implementaciones fake. Las Fases 1A–1B no usan ninguna credencial real.
**Consecuencias:** tests rápidos y deterministas; cambiar de proveedor/modelo no toca el dominio.

## ADR-005 — Credencial de Claude para workers
**Estado:** Pendiente (Fase 3)

**Contexto:** la suscripción Max (US$100) incluye Claude Code; las condiciones de uso de Agent SDK /
`claude -p` con suscripción han cambiado durante 2026. La API usa créditos prepagados con hard stop.
**Decisión provisional:** Max para desarrollar; `LLMGateway` preparado para API. Verificar términos
vigentes al iniciar la Fase 3 y decidir.

## ADR-006 — Proveedor de PostgreSQL en producción
**Estado:** Pendiente (Fase 4)

**Contexto:** Cloud SQL tiene un coste fijo mensual aun sin tráfico y puede ser el mayor gasto fijo.
**Opciones:** Cloud SQL (integración nativa GCP) vs. Postgres serverless gestionado (p. ej. Neon o
Supabase) con escala a cero. Evaluar coste, latencia desde Cloud Run, backups y conexión privada.

## ADR-007 — Django admin como panel provisional
**Estado:** Propuesta · *Ajuste respecto al plan original*

**Decisión:** hasta la Fase 6, el owner opera y aprueba desde Django admin (acceso restringido).
El panel con Firebase Auth llega en la Fase 6, cuando ya hay flujos reales que mostrar.
**Consecuencias:** se entrega valor antes; el panel se diseña con datos reales en lugar de supuestos.

## ADR-008 — Orden de fases
**Estado:** Propuesta · *Ajuste respecto al plan original*

**Decisión:**
1. La fase de fundamentos se divide en **1A (scaffold)** y **1B (núcleo de seguridad)** para
   revisar por separado estructura y lógica crítica.
2. El **despliegue en Google Cloud es una fase propia (4)**, antes de WhatsApp: el número comercial
   no debe depender de un túnel hacia el PC. GitHub y el worker se desarrollan localmente con túnel.
**Consecuencias:** WhatsApp se conecta directamente a la infraestructura definitiva.

## ADR-009 — Canales oficiales
**Estado:** Propuesta

**Decisión:** GitHub App privada (no PAT global); Meta WhatsApp Cloud API directa (no Baileys ni
automatización de navegador); Gmail API con OAuth (no SMTP con contraseña); LinkedIn solo publicación
con `w_member_social` y fuera del MVP.

## ADR-010 — Identificación de cliente solo por coincidencia exacta
**Estado:** Propuesta

**Decisión:** remitente → `Contact` → `Client` por teléfono/email exacto. Sin coincidencia difusa
que desencadene ejecución. Desconocido ⇒ `NeedsIdentification` + alerta al owner.
**Consecuencias:** algún ticket requerirá intervención manual; se elimina el riesgo de tocar el repo equivocado.

## ADR-011 — Monorepo de control
**Estado:** Propuesta

**Decisión:** este repositorio contiene solo Jarvis (control plane, workers, adapters, infra, docs).
El código de los clientes vive en sus propios repositorios y se clona por job.
