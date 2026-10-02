import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-key")
os.environ.setdefault("DATABASE_URL", "postgres://jarvis:jarvis@localhost:55432/jarvis")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("JARVIS_IDENTITY_VERIFIER", "identity.verifier.FakeVerifier")
os.environ.setdefault("JARVIS_OWNER_EMAILS", "owner@example.com")
os.environ.setdefault("JARVIS_LLM_PROVIDER", "fake")
os.environ.setdefault("JARVIS_REPO_HOST", "fake")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("JARVIS_LLM_MODEL_CHEAP", "fake-small")
os.environ.setdefault("JARVIS_LLM_MODEL_CODING", "fake-coding")
os.environ.setdefault("JARVIS_LLM_MODEL_REASONING", "fake-large")

from .base import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
