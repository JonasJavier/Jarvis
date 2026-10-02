import os

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-insecure-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "postgres://jarvis:jarvis@localhost:55432/jarvis")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")

from .base import *  # noqa: F403

DEBUG = True
