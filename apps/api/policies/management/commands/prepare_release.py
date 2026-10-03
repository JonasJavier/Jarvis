"""Pre-deploy step (ADR-034): migrate, then materialize the manifests that ship with the code.

One command instead of a shell pipeline, so the platform's pre-deploy runner needs no shell and
a failure in either step stops the deployment before traffic arrives.
"""

from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply migrations and load the versioned manifests (pre-deploy)."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write("prepare_release: applying migrations")
        call_command("migrate", "--noinput", stdout=self.stdout)
        self.stdout.write("prepare_release: loading manifests")
        call_command("load_manifests", stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("prepare_release: done"))
