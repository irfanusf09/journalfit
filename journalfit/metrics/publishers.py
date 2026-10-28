"""Where each publisher shows its journal metrics, and how to fetch them.

Each adapter turns a Journal (name, ISSNs, homepage from OpenAlex) into the
URLs most likely to carry metrics. Unknown publishers fall back to the
journal homepage. Pages are fetched with plain HTTP first and, if the site
blocks scripted clients, with a headless browser (requires Playwright).
"""

from __future__ import annotations

import csv
import re
from typing import Callable
from urllib.parse import urlparse

from .. import config
from ..http import FetchError, HttpClient
from ..models import Journal, Metrics
from .extract import extract_metrics, page_text


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def elsevier_urls(j: Journal) -> list[str]:
    urls = []
    if "sciencedirect.com/journal/" in j.homepage:
        base = j.homepage.split("?")[0].rstrip("/")
        urls.append(base + "/about/insights")
    urls.append(f"https://www.sciencedirect.com/journal/{_slug(j.name)}/about/insights")
    return urls


def springer_urls(j: Journal) -> list[str]:
    urls = []
    m = re.search(r"(?:link\.springer\.com|springer\.com)/journal/(\d+)", j.homepage)
    if m:
        urls.append(f"https://link.springer.com/journal/{m.group(1)}")
        urls.append(f"https://link.springer.com/journal/{m.group(1)}/journal-metrics")
    elif j.homepage:
        urls.append(j.homepage)
    return urls


def tandf_urls(j: Journal) -> list[str]:
    m = re.search(r"tandfonline\.com/(?:toc|loi|journals)/([a-z0-9]+)", j.homepage, re.I)
    if not m:
        return [j.homepage] if j.homepage else []
    code = m.group(1)
    return [f"https://www.tandfonline.com/action/journalInformation?show=journalMetrics&journalCode={code}"]


def wiley_urls(j: Journal) -> list[str]:
    issn = j.issns[-1] if j.issns else ""
    urls = [j.homepage] if j.homepage else []
    if issn:
        urls.append(f"https://onlinelibrary.wiley.com/journal/{issn.replace('-', '')}")
    return urls


def mdpi_urls(j: Journal) -> list[str]:
    m = re.search(r"mdpi\.com/journal/([a-z0-9\-]+)", j.homepage, re.I)
    slug = m.group(1) if m else _slug(j.name)
    return [f"https://www.mdpi.com/journal/{slug}", f"https://www.mdpi.com/journal/{slug}/stats"]


def homepage_urls(j: Journal) -> list[str]:
    return [j.homepage] if j.homepage else []


ADAPTERS: list[tuple[re.Pattern, Callable[[Journal], list[str]]]] = [
    (re.compile(r"elsevier|cell press", re.I), elsevier_urls),
    (re.compile(r"springer|nature|bmc|palgrave", re.I), springer_urls),
    (re.compile(r"taylor|francis|routledge", re.I), tandf_urls),
    (re.compile(r"wiley|hindawi", re.I), wiley_urls),
    (re.compile(r"mdpi|multidisciplinary digital", re.I), mdpi_urls),
]


def candidate_urls(j: Journal) -> list[str]:
    for pattern, fn in ADAPTERS:
        if pattern.search(j.publisher or "") or pattern.search(urlparse(j.homepage).netloc):
            urls = fn(j)
            break
    else:
        urls = homepage_urls(j)
    return list(dict.fromkeys(u for u in urls if u))


class PageFetcher:
    """Fetches HTML via HTTP, falling back to a headless browser when blocked."""

    def __init__(self, http: HttpClient, use_browser: bool = True):
        self.http = http
        self.use_browser = use_browser
        self._pw = None
        self._browser = None

    def get(self, url: str) -> str | None:
        try:
            html = self.http.get_text(url)
            if not _looks_blocked(html):
                return html
        except FetchError:
            pass
        return self._browser_get(url) if self.use_browser else None

    def _browser_get(self, url: str) -> str | None:
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            page = self._browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)  # let client-side metric widgets render
                html = page.content()
            finally:
                page.close()
            return None if _looks_blocked(html) else html
        except Exception:
            return None

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


def _looks_blocked(html: str) -> bool:
    head = html[:5000].lower()
    return any(s in head for s in ("just a moment...", "attention required", "access denied",
                                   "are you a robot", "captcha"))


def scrape_metrics(j: Journal, fetcher: PageFetcher) -> Metrics:
    found = Metrics()
    for url in candidate_urls(j):
        html = fetcher.get(url)
        if not html:
            continue
        found.merge(extract_metrics(page_text(html), url))
        if found.days_to_first_decision is not None and found.acceptance_rate is not None:
            break
    return found


def load_overrides(path=None) -> dict[str, Metrics]:
    """User-maintained CSV: issn,days_to_first_decision,acceptance_rate,impact_factor,citescore,note.

    Lets you record numbers you got from an editor or a publisher page the tool
    cannot read. Overrides always win over scraped values.
    """
    from ..sources.indexing import norm_issn

    path = path or config.OVERRIDES_PATH
    out: dict[str, Metrics] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            issn = norm_issn(row.get("issn", ""))
            if not issn:
                continue
            m = Metrics()
            for name in ("days_to_first_decision", "acceptance_rate", "impact_factor", "citescore"):
                v = (row.get(name) or "").strip()
                if v:
                    setattr(m, name, float(v))
                    m.sources[name] = "override: " + (row.get("note") or str(path))
            out[issn] = m
    return out
