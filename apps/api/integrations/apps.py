from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"

    def ready(self) -> None:
        from integrations.github.webhook import TASK_PREFIX
        from integrations.inbound import handle_inbound_event
        from jobs.queue import register_handler

        register_handler(TASK_PREFIX, handle_inbound_event)
