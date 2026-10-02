from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from policies.manifests.loader import ManifestError, load_bundle
from policies.manifests.materialize import detect_drift


class Command(BaseCommand):
    help = "Fail if the database differs from the versioned manifests (drift)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--dir", type=Path, default=None, help="Manifests directory.")

    def handle(self, *args: Any, **options: Any) -> None:
        root: Path = options["dir"] or settings.JARVIS_MANIFESTS_DIR
        try:
            bundle = load_bundle(root)
        except ManifestError as exc:
            raise CommandError("Invalid manifests:\n  " + "\n  ".join(exc.errors)) from exc

        drift = detect_drift(bundle)
        if drift:
            raise CommandError("Drift detected:\n  " + "\n  ".join(drift))
        self.stdout.write(self.style.SUCCESS("Database matches the manifests."))
