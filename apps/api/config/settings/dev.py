import os

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-insecure-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "postgres://jarvis:jarvis@localhost:55432/jarvis")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")
os.environ.setdefault("JARVIS_IDENTITY_VERIFIER", "identity.verifier.FakeVerifier")
os.environ.setdefault("JARVIS_LLM_PROVIDER", "fake")
os.environ.setdefault("JARVIS_REPO_HOST", "fake")
os.environ.setdefault("JARVIS_LLM_MODEL_CHEAP", "fake-small")
os.environ.setdefault("JARVIS_LLM_MODEL_CODING", "fake-coding")
os.environ.setdefault("JARVIS_LLM_MODEL_REASONING", "fake-large")

from .base import *  # noqa: F403

DEBUG = True
