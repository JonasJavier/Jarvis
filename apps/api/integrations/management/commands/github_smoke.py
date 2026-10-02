"""Phase 2 exit criterion: a manual job creates branch + trivial commit + Draft PR, idempotently."""

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations.github.broker import BrokerError
from integrations.github.factory import default_broker, default_host
from integrations.github.guard import ProtectedPathError
from integrations.github.host import ChangeSet, FileChange, RepoHostError
from integrations.github.preflight import RepositoryUnprotected
from jobs.services import create_job
from policies.engine import default_engine
from projects.models import Project
from tickets.services import open_manual_code_task


class Command(BaseCommand):
    help = "Open a trivial Draft PR on a project's repository through the RepoBroker."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--project", required=True, help="Project slug.")
        parser.add_argument(
            "--ref", required=True, help="Stable id of this smoke run (same id = same PR)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        project = Project.objects.filter(slug=options["project"], is_active=True).first()
        if project is None:
            raise CommandError(f"unknown or inactive project {options['project']!r}")
        engine = default_engine()
        ticket = open_manual_code_task(
            project, ref=f"github-smoke:{options['ref']}", summary="Phase 2 smoke test"
        )
        job = create_job(ticket, "smoke", engine=engine).job
        host = default_host()
        broker = default_broker(engine)
        try:
            base = host.default_branch_sha(project.repository, project.default_branch)
            changes = ChangeSet(
                base_sha=base,
                message="chore: Jarvis Phase 2 smoke test",
                files=(
                    FileChange(
                        path="jarvis-smoke.md",
                        content=(
                            f"Jarvis smoke test {options['ref']} at {timezone.now():%Y-%m-%d}\n"
                        ).encode(),
                    ),
                ),
            )
            result = broker.publish(
                job,
                changes,
                title=f"[Jarvis] Smoke test {options['ref']}",
                body="Draft PR created by Jarvis to verify the GitHub App integration.",
            )
        except (BrokerError, RepoHostError, RepositoryUnprotected, ProtectedPathError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"branch {result.branch} @ {result.commit_sha[:12]} "
                f"(reused={result.reused_branch}); PR #{result.pull_request.number} "
                f"{result.pull_request.url} (reused={result.reused_pull_request})"
            )
        )
