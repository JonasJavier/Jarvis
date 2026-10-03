from django.apps import AppConfig


class MessagingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "messaging"

    def ready(self) -> None:
        from jobs.queue import register_handler
        from messaging.outbound import TASK_PREFIX, deliver

        def handle(ident: str) -> None:
            deliver(int(ident))

        register_handler(TASK_PREFIX, handle)
