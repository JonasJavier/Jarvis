"""Owner declares a ticket's fix deployed to production (bridge until the Deployer, Phase 6).

    manage.py mark_deployed <ticket_id> --email owner@example.com

Walks the ticket to `production` and triggers the resolution notice to the client, which is
sent or parked for approval according to the project's autonomy level.
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from identity.verifier import is_owner_email
from messaging.resolution import notify_resolution
from tickets.models import Ticket, TicketStatus
from tickets.states import InvalidTransition, transition

PATH = [
    TicketStatus.PULL_REQUEST,
    TicketStatus.CI,
    TicketStatus.STAGING,
    TicketStatus.PRODUCTION,
]


class Command(BaseCommand):
    help = "Mark a ticket as deployed to production and notify the client."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("ticket_id", type=int)
        parser.add_argument("--email", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"].strip().lower()
        if not is_owner_email(email):
            raise CommandError("--email must be one of JARVIS_OWNER_EMAILS")
        ticket = (
            Ticket.objects.select_related("project__contract_policy", "client")
            .filter(pk=options["ticket_id"])
            .first()
        )
        if ticket is None:
            raise CommandError("ticket not found")
        actor = f"owner:{email}"
        try:
            if ticket.status == TicketStatus.WAITING_APPROVAL:
                transition(ticket, TicketStatus.PRODUCTION, actor=actor, reason="owner deployed")
            elif ticket.status in PATH[:-1]:
                for status in PATH[PATH.index(TicketStatus(ticket.status)) + 1 :]:
                    transition(ticket, status, actor=actor, reason="owner deployed")
            elif ticket.status != TicketStatus.PRODUCTION:
                raise CommandError(f"ticket {ticket.pk} is {ticket.status}; nothing to deploy")
        except InvalidTransition as exc:
            raise CommandError(str(exc)) from exc
        message = notify_resolution(ticket)
        if message is None:
            self.stdout.write(
                f"ticket #{ticket.pk} in production; no WhatsApp conversation to notify"
            )
            return
        self.stdout.write(
            f"ticket #{ticket.pk} -> {ticket.status}; notice #{message.pk} {message.status}"
        )
