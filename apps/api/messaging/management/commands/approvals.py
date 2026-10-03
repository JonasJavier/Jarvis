"""Owner decisions from the command line until the panel arrives (Phase 6, ADR-035).

    manage.py approvals list
    manage.py approvals approve <id> --email owner@example.com
    manage.py approvals reject <id> --email owner@example.com [--reason "..."]

The email must be in `JARVIS_OWNER_EMAILS`; the decision is audited as the owner's. Approving a
parked client message releases exactly that text; anything else needs a new approval.
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from approvals.models import Approval, ApprovalStatus
from approvals.services import ApprovalError, ApprovalService
from identity.verifier import VerifiedIdentity, is_owner_email
from messaging.models import Message, MessageStatus
from messaging.outbound import reject_by_owner, release_approved


class Command(BaseCommand):
    help = "List, approve or reject pending approvals as the owner."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("verb", choices=["list", "approve", "reject"])
        parser.add_argument("approval_id", nargs="?", type=int)
        parser.add_argument("--email", default="", help="Owner email (allowlisted).")
        parser.add_argument("--reason", default="")

    def handle(self, *args: Any, **options: Any) -> None:
        verb = options["verb"]
        if verb == "list":
            for approval in Approval.objects.filter(status=ApprovalStatus.PENDING).order_by("id"):
                message = Message.objects.filter(approval=approval).first()
                preview = f" «{message.body[:80]}»" if message else ""
                self.stdout.write(
                    f"#{approval.pk} {approval.project.slug} {approval.action} "
                    f"expires {approval.expires_at:%Y-%m-%d %H:%M}{preview}"
                )
            return
        if options["approval_id"] is None:
            raise CommandError("an approval id is required")
        email = options["email"].strip().lower()
        if not email or not is_owner_email(email):
            raise CommandError("--email must be one of JARVIS_OWNER_EMAILS")
        identity = VerifiedIdentity(subject=f"cli:{email}", email=email)
        found = Approval.objects.filter(pk=options["approval_id"]).first()
        if found is None:
            raise CommandError(f"approval {options['approval_id']} not found")
        approval = found
        service = ApprovalService()
        try:
            if verb == "approve":
                service.approve(approval, identity=identity, source="cli")
            else:
                service.reject(approval, identity=identity, source="cli")
        except ApprovalError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"approval #{approval.pk} {verb}d by {email}")

        message = Message.objects.filter(
            approval=approval, status=MessageStatus.PENDING_APPROVAL
        ).first()
        if message is None:
            return
        if verb == "approve":
            released = release_approved(message, approvals=service)
            self.stdout.write(f"message #{released.pk} -> {released.status}")
        else:
            rejected = reject_by_owner(message, reason=options["reason"])
            self.stdout.write(f"message #{rejected.pk} -> {rejected.status}")
