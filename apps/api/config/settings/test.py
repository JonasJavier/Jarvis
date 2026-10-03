import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-key")
os.environ.setdefault("DATABASE_URL", "postgres://jarvis:jarvis@localhost:55432/jarvis")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("JARVIS_IDENTITY_VERIFIER", "identity.verifier.FakeVerifier")
os.environ.setdefault("JARVIS_OWNER_EMAILS", "owner@example.com")
os.environ.setdefault("JARVIS_LLM_PROVIDER", "fake")
os.environ.setdefault("JARVIS_REPO_HOST", "fake")
os.environ.setdefault("JARVIS_WORKER_EXECUTOR", "inprocess")
os.environ.setdefault("JARVIS_TASK_QUEUE", "inprocess")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("JARVIS_LLM_MODEL_CHEAP", "fake-small")
os.environ.setdefault("JARVIS_MESSAGING_PROVIDER", "fake")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-whatsapp-secret")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "100000000000001")
os.environ.setdefault("JARVIS_OWNER_WHATSAPP", "+18095550199")
os.environ.setdefault("JARVIS_LLM_MODEL_CODING", "fake-coding")
os.environ.setdefault("JARVIS_LLM_MODEL_REASONING", "fake-large")

from .base import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
