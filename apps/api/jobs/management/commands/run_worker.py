"""Consume the durable task queue (ADR-003).

    manage.py run_worker --kinds inbound_event          # Railway worker: webhooks, tickets
    manage.py run_worker --kinds job                    # Docker-capable sandbox runner

Exits cleanly on SIGTERM/SIGINT. `--once` processes at most one task (tests, cron).
"""

from __future__ import annotations

import signal
import time
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection

from jobs.queue import PostgresQueue, registered_kinds, worker_name


class Command(BaseCommand):
    help = "Run a queue worker for the given task kinds."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--kinds", default="", help="Comma-separated kinds; default: all.")
        parser.add_argument("--poll-seconds", type=float, default=2.0)
        parser.add_argument("--once", action="store_true", help="Process at most one task.")
        parser.add_argument("--max-iterations", type=int, default=0, help="Stop after N polls.")
        parser.add_argument("--name", default="", help="Worker name recorded on claimed tasks.")

    def handle(self, *args: Any, **options: Any) -> None:
        kinds = frozenset(k.strip() for k in options["kinds"].split(",") if k.strip()) or None
        unknown = (kinds or frozenset()) - registered_kinds()
        if unknown:
            raise CommandError(f"no handler registered for kinds: {sorted(unknown)}")
        queue = PostgresQueue()
        name = options["name"] or worker_name()
        stop = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop
            stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, request_stop)

        self.stdout.write(f"worker {name} consuming {sorted(kinds) if kinds else 'all kinds'}")
        iterations = 0
        while not stop:
            iterations += 1
            if not connection.in_atomic_block:  # keep the test transaction alive under pytest
                close_old_connections()
            queue.requeue_stale()
            worked = queue.run_once(kinds=kinds, worker=name)
            if options["once"] and worked:
                break
            if options["max_iterations"] and iterations >= options["max_iterations"]:
                break
            if not worked:
                time.sleep(options["poll_seconds"])
        self.stdout.write("worker stopped")
