from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"

    def ready(self) -> None:
        from jobs.queue import register_handler
        from jobs.runner import run_queued_job

        register_handler("job", run_queued_job)
