# Control plane image: the same image serves the API (gunicorn) and the queue worker; the start
# command selects the role. Docker compose overrides the command for local development.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY apps ./apps
COPY workers ./workers
COPY project_manifests ./project_manifests
COPY pricing ./pricing

# Admin static files, collected at build time with throwaway values: no real secret or database
# is needed to collect static assets. WhiteNoise serves them at runtime.
RUN cd apps/api && DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY=build-only DATABASE_URL=sqlite://:memory: DJANGO_ALLOWED_HOSTS=build \
    JARVIS_IDENTITY_VERIFIER=identity.verifier.FakeVerifier JARVIS_LLM_PROVIDER=fake \
    JARVIS_LLM_MODEL_CHEAP=x JARVIS_LLM_MODEL_CODING=x JARVIS_LLM_MODEL_REASONING=x \
    JARVIS_REPO_HOST=fake JARVIS_WORKER_EXECUTOR=inprocess JARVIS_TASK_QUEUE=inprocess \
    python manage.py collectstatic --noinput --clear

RUN useradd --create-home --uid 1000 jarvis && chown -R jarvis:jarvis /app/staticfiles
USER jarvis

WORKDIR /app/apps/api
EXPOSE 8000
# $PORT is injected by the platform; 8000 is the local default.
CMD ["sh", "-c", "gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout 120 --access-logfile - --error-logfile -"]
