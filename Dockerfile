# API image. Used by docker compose for local development; the production setup is decided in
# Phase 4 (Railway) from the owner's deployment guide.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

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

RUN useradd --create-home --uid 1000 jarvis
USER jarvis

WORKDIR /app/apps/api
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
