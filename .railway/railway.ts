// Railway Infrastructure as Code for Jarvis Ops (Phase 4, ADR-034).
//
//   npm install                 # installs the `railway` SDK used to evaluate this file
//   railway config plan         # preview against the linked project/environment
//   railway config apply        # apply after review
//
// Secrets are never written here: they are set once by the owner with `railway variables --set`
// and kept with `preserve()`. Everything else is configuration and lives in this file.
import { defineRailway, github, postgres, preserve, project, service } from "railway/iac";

const REPO = "JonasJavier/Jarvis";

export default defineRailway(() => {
  const db = postgres("postgres");

  // Settings shared by every control-plane process. Values that depend on the public domain or
  // the deployed commit use Railway's own reference variables.
  const common = {
    DJANGO_SETTINGS_MODULE: "config.settings.prod",
    DATABASE_URL: db.env.DATABASE_URL,
    // Railway probes the healthcheck with its own Host header.
    DJANGO_ALLOWED_HOSTS: "${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app",
    CSRF_TRUSTED_ORIGINS: "https://${{RAILWAY_PUBLIC_DOMAIN}}",
    DJANGO_BEHIND_TLS_PROXY: "true",
    JARVIS_SOURCE_COMMIT: "${{RAILWAY_GIT_COMMIT_SHA}}",
    JARVIS_TASK_QUEUE: "postgres",
    // No API caller can authenticate until the real verifier arrives (Phase 6): fail closed.
    JARVIS_IDENTITY_VERIFIER: "identity.verifier.FakeVerifier",
    JARVIS_REPO_HOST: "github",
    GITHUB_APP_ID: "5168220",
    JARVIS_LLM_PROVIDER: "anthropic",
    JARVIS_LLM_MODEL_CODING: "claude-opus-5-5",
    JARVIS_LLM_MODEL_CHEAP: "claude-haiku-4-5",
    JARVIS_LLM_MODEL_REASONING: "claude-opus-5-5",
    // Railway containers are not privileged: the Docker sandbox never runs here (ADR-015).
    JARVIS_WORKER_EXECUTOR: "inprocess",
    JARVIS_CODER_AGENT: "auto",
    // WhatsApp Cloud API (Phase 5). Secrets are set by the owner and preserved below.
    JARVIS_MESSAGING_PROVIDER: "whatsapp",
    WHATSAPP_API_URL: "https://graph.facebook.com/v23.0",
  };

  // Secrets are set once by the owner (`railway variables --set ... --service api`) and only
  // referenced here, so a later apply keeps them instead of deleting them.
  const api = service("api", {
    source: github(REPO, { branch: "main" }),
    // Migrations and manifests go in the pre-deploy step so every deploy leaves the database
    // aligned with the versioned policy before traffic arrives.
    preDeploy: "python manage.py prepare_release",
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    env: {
      ...common,
      DJANGO_SECRET_KEY: preserve(),
      JARVIS_OWNER_EMAILS: preserve(),
      GITHUB_WEBHOOK_SECRET: preserve(),
      GITHUB_APP_PRIVATE_KEY: preserve(),
      ANTHROPIC_API_KEY: preserve(),
      WHATSAPP_PHONE_NUMBER_ID: preserve(),
      WHATSAPP_BUSINESS_ACCOUNT_ID: preserve(),
      WHATSAPP_ACCESS_TOKEN: preserve(),
      WHATSAPP_APP_SECRET: preserve(),
      WHATSAPP_VERIFY_TOKEN: preserve(),
      JARVIS_OWNER_WHATSAPP: preserve(),
    },
  });

  // Processes webhooks, client replies and other control-plane tasks from the durable queue. It
  // never consumes `job` tasks: those need a Docker-capable runner outside Railway (ADR-015).
  // Replying to clients needs the WhatsApp token and the model key; nothing else.
  const worker = service("worker", {
    source: github(REPO, { branch: "main" }),
    start: "python manage.py run_worker --kinds inbound_event,outbound_message",
    env: {
      ...common,
      DJANGO_SECRET_KEY: preserve(),
      ANTHROPIC_API_KEY: preserve(),
      WHATSAPP_PHONE_NUMBER_ID: preserve(),
      WHATSAPP_ACCESS_TOKEN: preserve(),
      JARVIS_OWNER_WHATSAPP: preserve(),
    },
  });

  return project("jarvis-ops", { resources: [db, api, worker] });
});
