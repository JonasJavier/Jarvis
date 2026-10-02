from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from integrations.github.events import connect_repository
from projects.models import Project


class Command(BaseCommand):
    help = "Link a project's repository to a GitHub App installation id."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--project", required=True, help="Project slug from the manifests.")
        parser.add_argument("--installation", required=True, type=int, help="Installation id.")

    def handle(self, *args: Any, **options: Any) -> None:
        project = Project.objects.filter(slug=options["project"], is_active=True).first()
        if project is None:
            raise CommandError(f"unknown or inactive project {options['project']!r}")
        connection = connect_repository(project.repository, options["installation"])
        self.stdout.write(self.style.SUCCESS(f"Connected {connection}"))
