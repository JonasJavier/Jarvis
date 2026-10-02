"""Versioned price catalog (cost-controls.md). Prices live in YAML, never in code.

The catalog is read from `settings.JARVIS_PRICING_FILE`; every ledger entry records the
`pricing_version` used so historical costs stay reproducible.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonNegativeMoney = Annotated[Decimal, Field(ge=0)]
ONE_MILLION = Decimal("1000000")
COST_PRECISION = Decimal("0.000001")


class PricingError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LLMPrice(_Strict):
    input_per_mtok: NonNegativeMoney
    output_per_mtok: NonNegativeMoney
    cache_read_per_mtok: NonNegativeMoney = Decimal("0")
    cache_write_per_mtok: NonNegativeMoney = Decimal("0")


class UnitPrice(_Strict):
    per_unit: NonNegativeMoney


class PricingFile(_Strict):
    version: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,32}$")]
    llm: dict[str, dict[str, LLMPrice]] = {}  # provider -> model -> price
    units: dict[str, dict[str, UnitPrice]] = {}  # provider -> service -> price


@dataclass(frozen=True)
class Usage:
    provider: str
    service: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    units: int = 0


class PriceCatalog:
    def __init__(self, data: PricingFile) -> None:
        self._data = data

    @property
    def version(self) -> str:
        return self._data.version

    def cost(self, usage: Usage) -> Decimal:
        """Cost in USD of `usage`; unknown provider/model/service is an error, never free."""
        if usage.service == "llm":
            try:
                price = self._data.llm[usage.provider][usage.model]
            except KeyError as exc:
                raise PricingError(
                    f"no price for LLM {usage.provider}/{usage.model} (version {self.version})"
                ) from exc
            total = (
                Decimal(usage.input_tokens) * price.input_per_mtok
                + Decimal(usage.output_tokens) * price.output_per_mtok
                + Decimal(usage.cache_read_tokens) * price.cache_read_per_mtok
                + Decimal(usage.cache_write_tokens) * price.cache_write_per_mtok
            ) / ONE_MILLION
        else:
            try:
                unit = self._data.units[usage.provider][usage.service]
            except KeyError as exc:
                raise PricingError(
                    f"no price for {usage.provider}/{usage.service} (version {self.version})"
                ) from exc
            total = Decimal(usage.units) * unit.per_unit
        return total.quantize(COST_PRECISION, rounding=ROUND_HALF_UP)


@lru_cache(maxsize=4)
def _load(path: str) -> PriceCatalog:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PricingError(f"cannot read price catalog {path}: {exc}") from exc
    return PriceCatalog(PricingFile.model_validate(raw))


def load_pricing(path: Path | str) -> PriceCatalog:
    return _load(str(path))


def default_pricing() -> PriceCatalog:
    from django.conf import settings

    return load_pricing(settings.JARVIS_PRICING_FILE)
