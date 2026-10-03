"""Connectivity check: send one owner alert through the configured messaging provider.

    manage.py whatsapp_smoke [--text "..."]

Only the owner's number (`JARVIS_OWNER_WHATSAPP`) can be targeted; never a client.
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from messaging.models import Message
from messaging.notifications import notify_owner


class Command(BaseCommand):
    help = "Send a test alert to the owner's WhatsApp."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--text", default="Jarvis conectado a WhatsApp.")

    def handle(self, *args: Any, **options: Any) -> None:
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        message = notify_owner("smoke", options["text"], reference=stamp)
        if message is None:
            raise CommandError("JARVIS_OWNER_WHATSAPP is not configured or invalid")
        message = Message.objects.get(pk=message.pk)
        self.stdout.write(
            f"message #{message.pk} -> {message.status} {message.external_id} {message.error}"
        )
