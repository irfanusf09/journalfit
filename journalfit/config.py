"""Settings and the user's filter criteria."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("JOURNALFIT_HOME", Path.home() / ".journalfit"))
CACHE_PATH = DATA_DIR / "http-cache.sqlite"
SCIMAGO_PATH = DATA_DIR / "scimago.csv"
SCOPUS_PATH = DATA_DIR / "scopus_sources.xlsx"
OVERRIDES_PATH = DATA_DIR / "metrics_overrides.csv"
BROWSER_PROFILE = DATA_DIR / "browser-profile"

CLAUDE_MODEL = os.environ.get("JOURNALFIT_MODEL", "claude-opus-5-5")
CONTACT_EMAIL = os.environ.get("JOURNALFIT_EMAIL", "")  # OpenAlex "polite pool"
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "")

QUARTILE_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}


@dataclass
class Criteria:
    publishers: list[str] = field(default_factory=list)  # empty = all publishers
    exclude_publishers: list[str] = field(default_factory=list)
    no_oa: bool = False  # exclude fully open-access journals (hybrid journals are kept)
    oa_only: bool = False
    scopus: bool = False
    max_first_decision_days: float | None = None
    min_acceptance_rate: float | None = None
    min_quartile: str | None = None  # e.g. "Q2" keeps Q1 and Q2
    max_apc_usd: float | None = None
    if_missing: str = "keep"  # "keep" or "skip" journals whose metric is unknown
    years: int = 5  # how far back to look at published papers

    def describe(self) -> str:
        parts = []
        if self.publishers:
            parts.append("publishers: " + ", ".join(self.publishers))
        if self.exclude_publishers:
            parts.append("excluding: " + ", ".join(self.exclude_publishers))
        if self.no_oa:
            parts.append("not fully open access")
        if self.oa_only:
            parts.append("open access only")
        if self.scopus:
            parts.append("Scopus-indexed")
        if self.max_first_decision_days is not None:
            parts.append(f"first decision <= {self.max_first_decision_days:g} days")
        if self.min_acceptance_rate is not None:
            parts.append(f"acceptance rate >= {self.min_acceptance_rate:g}%")
        if self.min_quartile:
            parts.append(f"{self.min_quartile} or better")
        if self.max_apc_usd is not None:
            parts.append(f"APC <= ${self.max_apc_usd:g}")
        parts.append(f"unknown metrics: {self.if_missing}")
        return "; ".join(parts)


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
