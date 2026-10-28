"""Pull editorial metrics out of publisher web pages.

Publishers word these differently ("Submission to first decision (median)",
"Review time", "Time to first decision", "Acceptance rate"), so extraction works
on the page's visible text with tolerant patterns and unit normalisation.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Metrics

UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "wk": 7, "wks": 7, "month": 30.4, "months": 30.4}
NUM = r"(\d+(?:[.,]\d+)?)"
UNIT = r"(days?|weeks?|wks?|months?)"
GAP = r"[^0-9%]{0,60}?"  # label-to-value gap without crossing another number

FIRST_DECISION = [
    rf"submission to first (?:editorial )?decision{GAP}{NUM}\s*{UNIT}",
    rf"(?:time|days) to first (?:editorial )?decision{GAP}{NUM}\s*{UNIT}?",
    rf"first decision{GAP}{NUM}\s*{UNIT}",
    rf"review time{GAP}{NUM}\s*{UNIT}",
    rf"submission to decision{GAP}{NUM}\s*{UNIT}",
    rf"{NUM}\s*{UNIT}\s*(?:from )?submission to first decision",
]
ACCEPTANCE_TIME = [
    rf"submission to acceptance{GAP}{NUM}\s*{UNIT}",
    rf"(?:time|days) to acceptance{GAP}{NUM}\s*{UNIT}?",
]
ACCEPTANCE_RATE = [
    rf"acceptance rate{GAP}{NUM}\s*%",
    rf"{NUM}\s*%\s*acceptance rate",
]
IMPACT_FACTOR = [
    rf"(?<!5[- ]year )(?<!five[- ]year )impact factor{GAP}{NUM}",
]
CITESCORE = [rf"citescore{GAP}{NUM}"]


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def _find(patterns: list[str], text: str, default_unit: str | None = None) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        value = float(m.group(1).replace(",", "."))
        if default_unit is None:
            return value
        unit = (m.group(2) if m.lastindex and m.lastindex >= 2 else None) or default_unit
        return round(value * UNIT_DAYS.get(unit.lower(), 1), 1)
    return None


def extract_metrics(text: str, url: str = "") -> Metrics:
    m = Metrics(
        days_to_first_decision=_find(FIRST_DECISION, text, "days"),
        days_to_acceptance=_find(ACCEPTANCE_TIME, text, "days"),
        acceptance_rate=_find(ACCEPTANCE_RATE, text),
        impact_factor=_find(IMPACT_FACTOR, text),
        citescore=_find(CITESCORE, text),
    )
    if m.acceptance_rate is not None and not 0 < m.acceptance_rate <= 100:
        m.acceptance_rate = None
    if m.days_to_first_decision is not None and not 0 < m.days_to_first_decision < 1000:
        m.days_to_first_decision = None
    if m.impact_factor is not None and m.impact_factor > 200:
        m.impact_factor = None
    for name in ("days_to_first_decision", "days_to_acceptance", "acceptance_rate", "impact_factor", "citescore"):
        if getattr(m, name) is not None:
            m.sources[name] = url or "publisher page"
    return m
