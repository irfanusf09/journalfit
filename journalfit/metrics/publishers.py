"""Where each publisher shows its journal metrics, and how to fetch them.

Each adapter turns a Journal (name, ISSNs, homepage from OpenAlex) into the
URLs most likely to carry metrics. Unknown publishers fall back to the
journal homepage. Pages are fetched with plain HTTP first and, if the site
blocks scripted clients, with a headless browser (requires Playwright).
"""

from __future__ import annotations

import csv
import re
from html import escape
from typing import Callable
from urllib.parse import urlparse

from .. import config
from .. import trace
from ..http import USER_AGENT, FetchError, HttpClient
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
        urls.append(f"https://link.springer.com/journal/{m.group(1)}?detailsPage=aboutThis")
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
    m = re.search(r"wiley\.com/journal/(\d{7}[\dXx])", j.homepage)
    jid = m.group(1) if m else (j.issns[-1].replace("-", "") if j.issns else "")
    return [f"https://onlinelibrary.wiley.com/journal/{jid}/journal-metrics"] if jid else []


def mdpi_urls(j: Journal) -> list[str]:
    m = re.search(r"mdpi\.com/journal/([a-z0-9\-]+)", j.homepage, re.I)
    slug = m.group(1) if m else _slug(j.name)
    return [f"https://www.mdpi.com/journal/{slug}", f"https://www.mdpi.com/journal/{slug}/stats"]


def nature_urls(j: Journal) -> list[str]:
    home = j.homepage.split("?")[0].rstrip("/")
    return [home + "/journal-impact", home] if "nature.com/" in home else homepage_urls(j)


def homepage_urls(j: Journal) -> list[str]:
    return [j.homepage] if j.homepage else []


ADAPTERS: list[tuple[re.Pattern, Callable[[Journal], list[str]]]] = [
    (re.compile(r"elsevier|cell press", re.I), elsevier_urls),
    (re.compile(r"nature portfolio|nature\.com", re.I), nature_urls),
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
    """Fetches a journal page, respecting sites that refuse automated access.

    Order: plain HTTP; a headless browser only when the page loaded but needs
    JavaScript to show its content; then an optional remote reader (the AI
    provider's own fetch service). A site that blocks us (HTTP 403 or a bot
    check) is not retried in ways meant to get around the block.

    A page counts only if it is the real page: not a block page, not a tiny
    redirect stub, and (when `expect` is given) it mentions the journal.
    """

    def __init__(self, http: HttpClient, use_browser: bool = True, remote: Callable[[str], str] | None = None):
        self.http = http
        self.use_browser = use_browser
        self.remote = remote
        self._pw = None
        self._browser = None

    def get(self, url: str, expect: list[str] | None = None) -> str | None:
        blocked = False
        try:
            html = self.http.get_text(url)
            why = _reject(html, expect)
            if not why:
                trace.note(f"    fetched {url} ({len(html):,} chars)")
                return html
            trace.note(f"    {url}: {why}")
            blocked = why == BLOCKED
            if self.use_browser and why.startswith("too short"):  # JavaScript page: render it normally
                html = self._browser_get(url)
                why = _reject(html, expect) if html else "failed to load"
                trace.note(f"    browser {url}: " + (why or f"ok ({len(html):,} chars)"))
                if not why:
                    return html
        except FetchError as e:
            blocked = "HTTP 403" in str(e) or "HTTP 429" in str(e)
            trace.note(f"    {url}: {e}" + (" (site refuses automated access)" if blocked else ""))
        if self.remote and not blocked:  # a site that refused us isn't fetched through someone else
            try:
                text = self.remote(url)
            except Exception as e:
                text = ""
                trace.note(f"    remote reader {url}: {e}")
            if text:
                html = "<html><body>" + escape(text) + "</body></html>"
                why = _reject(html, expect)
                trace.note(f"    remote reader {url}: " + (why or f"ok ({len(text):,} chars)"))
                if not why:
                    return html
        return None

    def _browser_get(self, url: str) -> str | None:
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            page = self._browser.new_page(user_agent=USER_AGENT)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)  # let client-side metric widgets render
                return page.content()
            finally:
                page.close()
        except Exception:
            return None

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


BLOCKED = "site refuses automated access (bot check)"
BLOCK_MARKERS = ("just a moment", "attention required", "access denied", "are you a robot", "captcha",
                 "verify you are human", "unusual traffic", "checking your browser", "pardon our interruption")


def _looks_blocked(html: str) -> bool:
    head = html[:20000].lower()
    return any(s in head for s in BLOCK_MARKERS)


def journal_markers(j: Journal) -> list[str]:
    """Strings that the journal's own pages are expected to contain."""
    name = re.sub(r"\s+", " ", j.name).strip()
    marks = [name, name.replace("&", "and"), name.replace(" and ", " & ")]
    if ":" in name:
        marks.append(name.split(":")[0])
    marks += j.issns
    return [m for m in dict.fromkeys(marks) if m]


def _reject(html: str, expect: list[str] | None) -> str:
    """Why this isn't the real journal page ("" if it is)."""
    if _looks_blocked(html):
        return BLOCKED
    text = page_text(html)
    if len(text) < 1500:
        return f"too short to be the journal page ({len(text):,} chars of text; probably a redirect)"
    if expect:
        low = text.lower()
        if not any(e.lower() in low for e in expect):
            return "page doesn't mention this journal (wrong or placeholder page)"
    return ""


def scrape_metrics(j: Journal, fetcher: PageFetcher) -> tuple[Metrics, dict[str, str]]:
    """Metrics found on the publisher pages, plus the page texts (for AI reading)."""
    found = Metrics()
    pages: dict[str, str] = {}
    expect = journal_markers(j)
    for url in candidate_urls(j):
        html = fetcher.get(url, expect)
        if not html:
            continue
        pages[url] = page_text(html)
        evidence: dict[str, str] = {}
        got = extract_metrics(pages[url], url, evidence)
        found.merge(got)
        hits = {k: v for k, v in got.sources.items()}
        trace.note(f"    metrics on page: {', '.join(f'{k}={getattr(got, k)}' for k in hits) or 'none'}")
        for k in hits:
            trace.note(f"      {k} from: \"{evidence.get(k, '')[:160]}\"")
        if not hits:
            for snip in trace.snippets(pages[url]):
                trace.note(f"      {snip}")
        if found.days_to_first_decision is not None and found.acceptance_rate is not None:
            break
    return found, pages


GUIDE_LINK = re.compile(r"guide for authors|author guidelines|instructions for authors|"
                        r"submission guidelines|information for authors|for authors", re.I)


def guideline_urls(j: Journal) -> list[str]:
    urls = []
    home = j.homepage.rstrip("/")
    if "sciencedirect.com/journal/" in home:
        urls.append(home.split("?")[0] + "/publish/guide-for-authors")
    m = re.search(r"springer\.com/journal/(\d+)", home)
    if m:
        urls.append(f"https://link.springer.com/journal/{m.group(1)}/submission-guidelines")
    m = re.search(r"tandfonline\.com/(?:toc|loi|journals)/([a-z0-9]+)", home, re.I)
    if m:
        urls.append(f"https://www.tandfonline.com/action/authorSubmission?show=instructions&journalCode={m.group(1)}")
    m = re.search(r"mdpi\.com/journal/([a-z0-9\-]+)", home, re.I)
    if m:
        urls.append(f"https://www.mdpi.com/journal/{m.group(1)}/instructions")
    return urls


def find_guidelines_text(j: Journal, fetcher: PageFetcher) -> tuple[str, str]:
    """(url, text) of the journal's author guidelines, found without a web search."""
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    tried = set()
    expect = journal_markers(j)
    for url in guideline_urls(j):
        tried.add(url)
        html = fetcher.get(url, expect)
        if html:
            return url, page_text(html)
    if not j.homepage:
        return "", ""
    html = fetcher.get(j.homepage)
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if GUIDE_LINK.search(a.get_text(" ", strip=True)):
            url = urljoin(j.homepage, a["href"])
            if url in tried:
                continue
            page = fetcher.get(url)
            if page:
                return url, page_text(page)
    return "", ""


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
