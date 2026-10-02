from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from policies.manifests.loader import ManifestError, load_bundle
from policies.manifests.materialize import apply_bundle, source_commit


class Command(BaseCommand):
    help = "Validate the versioned manifests and materialize them into the database."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--dir", type=Path, default=None, help="Manifests directory.")
        parser.add_argument(
            "--validate-only", action="store_true", help="Validate without writing."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        root: Path = options["dir"] or settings.JARVIS_MANIFESTS_DIR
        try:
            bundle = load_bundle(root)
        except ManifestError as exc:
            raise CommandError("Invalid manifests:\n  " + "\n  ".join(exc.errors)) from exc

        if options["validate_only"]:
            self.stdout.write(self.style.SUCCESS("Manifests are valid."))
            return

        report = apply_bundle(bundle, commit=source_commit(root.parent))
        if not report.changed:
            self.stdout.write(self.style.SUCCESS("Manifests already loaded; nothing changed."))
            return
        for label, items in (
            ("created", report.created),
            ("updated", report.updated),
            ("deactivated", report.deactivated),
        ):
            for item in items:
                self.stdout.write(f"{label}: {item}")
        self.stdout.write(self.style.SUCCESS("Manifests loaded."))
