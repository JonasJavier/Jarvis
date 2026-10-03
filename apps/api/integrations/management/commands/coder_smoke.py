"""Phase 3 exit criterion: a bug ticket -> sandboxed worker -> tests green -> Draft PR."""

from argparse import ArgumentParser
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from budgets.services import BudgetGuard
from integrations.github.factory import default_broker
from jobs.factory import default_executor
from jobs.models import JobStatus
from jobs.orchestrator import run_job
from jobs.queue import InProcessQueue, default_queue
from jobs.services import check_budget_and_queue, create_job
from jobs.states import transition
from policies.engine import default_engine
from projects.models import Project
from tickets.services import open_manual_code_task
from workers.coder.job_spec import AgentConfig


class Command(BaseCommand):
    help = "Run the coding worker (mock agent) on a project and open the resulting Draft PR."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--project", required=True, help="Project slug.")
        parser.add_argument("--ref", required=True, help="Stable id of this run.")
        parser.add_argument("--summary", default="Bug reported by the client", help="Ticket text.")
        parser.add_argument(
            "--agent", choices=["mock", "claude_code"], default="mock", help="Agent kind."
        )
        parser.add_argument(
            "--enqueue-only",
            action="store_true",
            help="Create the ticket and job and leave the job in the configured queue "
            "(a `run_worker --kinds job` runner executes it).",
        )
        parser.add_argument(
            "--replace",
            nargs=3,
            action="append",
            metavar=("PATH", "OLD", "NEW"),
            default=[],
            help="Scripted edit for the mock agent: replace OLD with NEW in PATH.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        project = Project.objects.filter(slug=options["project"], is_active=True).first()
        if project is None:
            raise CommandError(f"unknown or inactive project {options['project']!r}")
        engine = default_engine()
        ticket = open_manual_code_task(
            project, ref=f"coder-smoke:{options['ref']}", summary=options["summary"]
        )
        job = create_job(ticket, "fix", engine=engine).job
        guard = BudgetGuard()
        if options["enqueue_only"]:
            if job.status == "pending":
                check_budget_and_queue(job, guard=guard, queue=default_queue())
            self.stdout.write(
                self.style.SUCCESS(f"job {job.pk} is {job.status}; left for a runner")
            )
            return
        if job.status == "pending":
            check_budget_and_queue(job, guard=guard, queue=InProcessQueue())
        elif job.status in ("failed", "timed_out") and job.can_retry:
            transition(job, JobStatus.QUEUED, actor="owner", reason="manual retry")
        if job.status != "queued":
            raise CommandError(f"job {job.pk} is {job.status}; nothing to run")
        agent = AgentConfig(
            kind=options["agent"],
            params={
                "replace": [
                    {"path": path, "old": old, "new": new} for path, old, new in options["replace"]
                ],
                "commit_message": f"fix: {options['summary'][:60]}",
            },
        )
        outcome = run_job(
            job,
            engine=engine,
            broker=default_broker(engine),
            guard=guard,
            executor=default_executor(),
            agent=agent,
            workspace_root=settings.JARVIS_WORKSPACE_ROOT,
        )
        run = outcome.run
        line = (
            f"run #{run.attempt} {run.status}: turns={run.turns} files={run.files_changed} "
            f"tests_passed={run.tests_passed} error={run.error!r}"
        )
        if run.status != "succeeded":
            raise CommandError(line)
        self.stdout.write(self.style.SUCCESS(f"{line}\nPR: {outcome.pr_url}"))
