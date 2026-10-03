"""Production settings (Railway, Phase 4). Everything provider-specific comes from variables."""

from .base import *  # noqa: F403
from .base import MIDDLEWARE, env_bool, env_list

# TLS is terminated by the platform proxy; trust its header, redirect plain HTTP, keep the
# healthcheck reachable over HTTP so the platform can probe it.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if env_bool("DJANGO_BEHIND_TLS_PROXY", True) else None
)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# 30 days of HSTS without preload: preload is a one-way door for the whole domain (ADR-034).
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Browser origins allowed to POST to the admin (the public domain, with scheme).
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# Static files (admin) served by the app itself through WhiteNoise, collected at build time.
MIDDLEWARE = [
    MIDDLEWARE[0],  # SecurityMiddleware first
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
