"""Build the configured `WorkerExecutor` from settings."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from jobs.executor import InProcessExecutor, LocalDockerExecutor, WorkerExecutor


def default_executor() -> WorkerExecutor:
    kind = settings.JARVIS_WORKER_EXECUTOR
    if kind == "docker":
        return LocalDockerExecutor(
            settings.JARVIS_WORKER_IMAGE,
            llm_target=settings.JARVIS_LLM_PROXY_TARGET if settings.ANTHROPIC_API_KEY else None,
        )
    if kind == "inprocess":
        return InProcessExecutor()
    raise ImproperlyConfigured(f"unknown JARVIS_WORKER_EXECUTOR {kind!r}")
