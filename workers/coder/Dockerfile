# Sandbox image for the coding worker (architecture.md section 11).
# Build from the repository root:  docker build -f workers/coder/Dockerfile -t jarvis-worker:dev .
# The image holds no credentials. The control plane mounts a workspace at /work and runs the
# `prepare` phase (install, network allowed) and the `work` phase (agent + tests, network denied).
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 worker \
    # The workspace is a bind mount owned by another uid; mode bits are noise on Windows hosts.
    && git config --system safe.directory '*' \
    && git config --system core.filemode false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/jarvis \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy

COPY workers /opt/jarvis/workers

USER worker
WORKDIR /work
ENTRYPOINT ["python", "-m", "workers.coder.runner", "--work", "/work"]
