"""Review speed from Crossref: many publishers record each paper's received and
accepted dates in its Crossref record. The median over a journal's recent papers
gives a submission-to-acceptance time that needs no access to the journal's website.

API docs: https://api.crossref.org/swagger-ui/index.html
"""

from __future__ import annotations

import datetime as dt
import re
import statistics

from .. import config
from ..http import FetchError, HttpClient

API = "https://api.crossref.org"
RECEIVED = ("received", "submitted", "date_received", "received_date")
ACCEPTED = ("accepted", "date_accepted", "accepted_date")
FORMATS = ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y")


def parse_date(text: str) -> dt.date | None:
    text = re.sub(r"\s+", " ", str(text)).strip().rstrip(".")
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
    for fmt in FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_of(item: dict, names: tuple[str, ...]) -> dt.date | None:
    for a in item.get("assertion") or []:
        if str(a.get("name", "")).lower() in names:
            d = parse_date(a.get("value", ""))
            if d:
                return d
    return None


def acceptance_days(items: list[dict]) -> list[int]:
    """Received-to-accepted days for each paper that records both dates."""
    out = []
    for it in items:
        r, a = _date_of(it, RECEIVED), _date_of(it, ACCEPTED)
        if r and a:
            days = (a - r).days
            if 0 < days < 1500:  # ignore typos and placeholder dates
                out.append(days)
    return out


def review_speed(issn: str, http: HttpClient, years: int = 2, rows: int = 200,
                 min_papers: int = 15) -> tuple[float, int] | None:
    """(median days from submission to acceptance, papers used), or None if too few papers record dates."""
    since = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    params = {"rows": rows, "filter": f"from-pub-date:{since},type:journal-article",
              "select": "DOI,assertion"}
    if config.CONTACT_EMAIL:
        params["mailto"] = config.CONTACT_EMAIL
    try:
        data = http.get_json(f"{API}/journals/{issn}/works", params)
    except FetchError:
        return None
    days = acceptance_days((data.get("message") or {}).get("items") or [])
    if len(days) < min_papers:
        return None
    return float(statistics.median(days)), len(days)
