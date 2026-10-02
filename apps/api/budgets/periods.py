"""Budget periods. Daily and monthly windows follow the timezone declared in `global.yaml`."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import models


class Period(models.TextChoices):
    DAY = "day"
    MONTH = "month"
    RUN = "run"  # one job run; never resets


def period_key(period: Period | str, now: datetime, tz: str) -> str:
    """Stable identifier of the window `now` falls into: `2026-10-02`, `2026-10` or `run`."""
    local = now.astimezone(ZoneInfo(tz))
    match Period(period):
        case Period.DAY:
            return local.strftime("%Y-%m-%d")
        case Period.MONTH:
            return local.strftime("%Y-%m")
        case Period.RUN:
            return "run"
