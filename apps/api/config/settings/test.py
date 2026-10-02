import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-key")
os.environ.setdefault("DATABASE_URL", "postgres://jarvis:jarvis@localhost:55432/jarvis")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")

from .base import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
