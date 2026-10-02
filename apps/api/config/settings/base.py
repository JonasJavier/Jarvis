"""Settings shared by every environment. Values come from environment variables.

Nothing here assumes a specific hosting provider (ADR-001).
"""

import os
from pathlib import Path

import dj_database_url
import django_stubs_ext
from django.core.exceptions import ImproperlyConfigured

# Allows generic annotations such as ModelAdmin[Model] at runtime.
django_stubs_ext.monkeypatch()

API_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = API_DIR.parents[1]


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "audit",
    "clients",
    "projects",
    "policies",
    "idempotency",
    "budgets",
    "approvals",
    "tickets",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": dj_database_url.parse(env("DATABASE_URL"), conn_max_age=60)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = REPO_DIR / "staticfiles"

# Every API caller is verified by the configured IdentityVerifier; nobody is anonymous.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["identity.authentication.BearerIdentityAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "UNAUTHENTICATED_USER": None,
}

# Versioned manifests are the source of truth for critical policy (ADR-017).
JARVIS_MANIFESTS_DIR = Path(env("JARVIS_MANIFESTS_DIR", str(REPO_DIR / "project_manifests")))

# Versioned price catalog (cost-controls.md): never hardcode a tariff.
JARVIS_PRICING_FILE = Path(env("JARVIS_PRICING_FILE", str(REPO_DIR / "pricing" / "pricing.yaml")))

# Identity: dotted path of the IdentityVerifier implementation; owner recognised by email allowlist.
JARVIS_IDENTITY_VERIFIER = env("JARVIS_IDENTITY_VERIFIER")
JARVIS_OWNER_EMAILS = env_list("JARVIS_OWNER_EMAILS")

# LLM access goes through LLMGateway only. Models are configuration, resolved by role.
JARVIS_LLM_PROVIDER = env("JARVIS_LLM_PROVIDER")
JARVIS_LLM_MODELS = {
    "cheap": env("JARVIS_LLM_MODEL_CHEAP"),
    "coding": env("JARVIS_LLM_MODEL_CODING"),
    "reasoning": env("JARVIS_LLM_MODEL_REASONING"),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}
