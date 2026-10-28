"""The journal-finding pipeline: candidates -> filters -> metrics -> scope review -> ranking."""

from __future__ import annotations

import math
from collections import defaultdict
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import config, trace
from .config import Criteria
from .http import FetchError, HttpClient
from .metrics.publishers import PageFetcher, load_overrides, scrape_metrics
from .models import Candidate, Journal, Manuscript, Metrics
from .similarity import tfidf_similarities, top_terms
from .sources.crossref import review_speed
from .sources.indexing import IndexingData, norm_issn
from .sources.openalex import OpenAlex, Work

QUARTILE_SCORE = {"Q1": 1.0, "Q2": 0.75, "Q3": 0.5, "Q4": 0.25}


@dataclass
class FindResult:
    manuscript: Manuscript
    criteria: Criteria
    ranked: list[Candidate]
    excluded: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


class Finder:
    def __init__(self, http: HttpClient, llm=None, indexing: IndexingData | None = None,
                 use_browser: bool = True, log: Callable[[str], None] = print):
        self.http = http
        self.oa = OpenAlex(http)
        self.llm = llm
        self.indexing = indexing or IndexingData.load()
        # The AI provider's own page reader (DeepSeek Fetch) helps with pages that need it.
        self.fetcher = PageFetcher(http, use_browser=use_browser, remote=getattr(llm, "fetch_text", None))
        self.log = log

    # ---------------------------------------------------------------- stage 1
    def gather_works(self, ms: Manuscript, criteria: Criteria) -> tuple[list[Work], set[str], list[str]]:
        publisher_ids = self.oa.resolve_publishers(criteria.publishers) if criteria.publishers else None
        queries = list(ms.search_queries)
        if ms.title:
            queries.insert(0, ms.title)
        if ms.keywords:
            queries.append(" ".join(ms.keywords[:5]))
        if len(queries) < 3:
            queries.append(" ".join(top_terms(ms.summary_text() or ms.body_text[:5000])))
        queries = list(dict.fromkeys(q for q in queries if q.strip()))

        works: dict[str, Work] = {}
        failures = 0
        for q in queries:
            try:
                for w in self.oa.search_works(q, criteria.years, publisher_ids):
                    works.setdefault(w.id, w)
            except FetchError as e:
                failures += 1
                self.log(f"  search failed for '{q}': {e}")
        if failures == len(queries):
            raise RuntimeError("Could not reach OpenAlex (api.openalex.org). Check your internet connection or proxy.")
        ref_works = self.oa.works_by_doi(ms.reference_dois) if ms.reference_dois else []
        ref_sources = [w.source_id for w in ref_works]
        return list(works.values()), set(publisher_ids or []), ref_sources

    def score_sources(self, ms: Manuscript, works: list[Work], ref_sources: list[str]) -> dict[str, Candidate]:
        sims = tfidf_similarities(ms.summary_text() or ms.body_text[:5000],
                                  [f"{w.title}. {w.abstract}" for w in works])
        by_source: dict[str, list[tuple[float, Work]]] = defaultdict(list)
        for s, w in zip(sims, works):
            by_source[w.source_id].append((float(s), w))
        ref_counts: dict[str, int] = defaultdict(int)
        for sid in ref_sources:
            ref_counts[sid] += 1

        cands: dict[str, Candidate] = {}
        for sid in set(by_source) | set(ref_counts):
            pairs = sorted(by_source.get(sid, []), key=lambda p: -p[0])
            top = [s for s, _ in pairs[:5]]
            # Strength of the best matches, plus breadth (how many related papers it publishes).
            strength = (sum(top) / 5) if top else 0.0
            breadth = min(1.0, math.log1p(len(pairs)) / math.log1p(15))
            c = Candidate(
                journal=Journal(openalex_id=sid, name=pairs[0][1].source_name if pairs else sid),
                matched_works=len(pairs), reference_hits=ref_counts.get(sid, 0),
                text_fit=0.75 * strength + 0.25 * breadth * strength,
                example_titles=[w.title for _, w in pairs[:6]],
            )
            cands[sid] = c
        top_fit = max((c.text_fit for c in cands.values()), default=0) or 1
        for c in cands.values():
            c.text_fit /= top_fit
        return cands

    # ---------------------------------------------------------------- stage 2
    def apply_static_filters(self, c: Candidate, criteria: Criteria, publisher_ids: set[str]) -> str:
        j = c.journal
        pub = j.publisher.lower()
        if criteria.publishers and not publisher_ids and not any(p.lower() in pub for p in criteria.publishers):
            return "publisher not selected"
        if any(p.lower() in pub for p in criteria.exclude_publishers):
            return "publisher excluded"
        fully_oa = j.is_oa or j.in_doaj
        if criteria.no_oa and fully_oa:
            return "fully open access"
        if criteria.oa_only and not fully_oa:
            return "not open access"
        if criteria.scopus:
            if j.scopus_indexed is None:
                return "Scopus status unknown (run `journalfit data update`)"
            if not j.scopus_indexed:
                return "not Scopus-indexed"
            if j.scopus_active is False:
                return "discontinued in Scopus"
        if criteria.min_quartile:
            limit = config.QUARTILE_ORDER[criteria.min_quartile]
            q = j.metrics.quartile
            if q is None and criteria.if_missing == "skip":
                return "quartile unknown"
            if q is not None and config.QUARTILE_ORDER[q] > limit:
                return f"{q} below {criteria.min_quartile}"
        if criteria.max_apc_usd is not None and fully_oa and j.metrics.apc_usd is not None \
                and j.metrics.apc_usd > criteria.max_apc_usd:
            return f"APC ${j.metrics.apc_usd:,.0f} above limit"
        return ""

    def apply_metric_filters(self, c: Candidate, criteria: Criteria) -> str:
        m = c.journal.metrics
        if criteria.max_first_decision_days is not None:
            d = m.days_to_first_decision
            if d is None:
                if criteria.if_missing == "skip":
                    return "time to first decision not published"
                c.warnings.append("time to first decision not published")
            elif d > criteria.max_first_decision_days:
                return f"first decision {d:g} days > {criteria.max_first_decision_days:g}"
        if criteria.max_acceptance_days is not None:
            d = m.days_to_acceptance
            if d is None:
                if criteria.if_missing == "skip":
                    return "time to acceptance unknown"
                c.warnings.append("time to acceptance unknown")
            elif d > criteria.max_acceptance_days:
                return f"acceptance takes {d:g} days > {criteria.max_acceptance_days:g}"
        if criteria.min_acceptance_rate is not None:
            a = m.acceptance_rate
            if a is None:
                if criteria.if_missing == "skip":
                    return "acceptance rate not published"
                c.warnings.append("acceptance rate not published")
            elif a < criteria.min_acceptance_rate:
                return f"acceptance rate {a:g}% < {criteria.min_acceptance_rate:g}%"
        return ""

    # ---------------------------------------------------------------- stage 3
    def collect_metrics(self, cands: list[Candidate], criteria: Criteria, web_lookup: bool,
                        lookup_top: int = 8) -> None:
        """Overrides, then publisher pages, then (for the best `lookup_top` only) an AI lookup.

        AI lookups are remembered for 90 days, so journals seen before cost nothing.
        """
        overrides = load_overrides()
        cache = MetricCache()
        for i, c in enumerate(cands, 1):
            j = c.journal
            self.log(f"  [{i}/{len(cands)}] metrics: {j.name}")
            trace.note(f"\n[{i}] {j.name} ({j.publisher}) homepage={j.homepage or '-'}")
            for issn in j.issns:
                if norm_issn(issn) in overrides:
                    j.metrics.merge(overrides[norm_issn(issn)])
            cached = cache.get(j.openalex_id)
            if cached:
                j.metrics.merge(cached)
                trace.note("    using AI metrics remembered from an earlier run")
            scraped, pages = scrape_metrics(j, self.fetcher)
            j.metrics.merge(scraped)
            if j.metrics.days_to_acceptance is None:
                self._crossref_speed(j)
            missing = j.metrics.days_to_first_decision is None or \
                (criteria.min_acceptance_rate is not None and j.metrics.acceptance_rate is None)
            if web_lookup and self.llm and missing and not cached and i <= lookup_top:
                try:
                    found = self.llm.lookup_metrics(j, pages)
                    j.metrics.merge(found)
                    if found.sources:  # remember only real findings; "nothing found" is retried next run
                        cache.put(j.openalex_id, found)
                    trace.note(f"    AI lookup: {', '.join(f'{k}={getattr(found, k)}' for k in found.sources) or 'nothing found'}")
                except Exception as e:  # a failed lookup leaves the metric unknown, never guessed
                    c.warnings.append(f"metric lookup failed: {e}")
                    trace.note(f"    AI lookup failed: {e}")
            elif web_lookup and self.llm and missing and not cached:
                trace.note(f"    no AI lookup (only the best {lookup_top} journals get one)")
        cache.save()

    def _crossref_speed(self, j: Journal) -> None:
        """Submission-to-acceptance time from received/accepted dates publishers record in Crossref."""
        for issn in j.issns[:2]:
            got = review_speed(issn, self.http)
            if got:
                days, n = got
                j.metrics.days_to_acceptance = days
                j.metrics.sources["days_to_acceptance"] = f"Crossref: median of {n} recent papers (received to accepted)"
                trace.note(f"    Crossref: median {days:g} days from submission to acceptance ({n} papers)")
                return
        trace.note("    Crossref: too few recent papers record received/accepted dates")

    # ---------------------------------------------------------------- pipeline
    def find(self, ms: Manuscript, criteria: Criteria, top: int = 10, metrics_for: int = 25,
             web_lookup: bool = True, lookup_top: int | None = None) -> FindResult:
        notes: list[str] = []
        trace.start()
        if criteria.scopus and not self.indexing.available:
            raise RuntimeError("Scopus data not downloaded yet. Run: journalfit data update")

        self.log("Searching published literature for similar papers...")
        works, publisher_ids, ref_sources = self.gather_works(ms, criteria)
        if not works and not ref_sources:
            raise RuntimeError("No similar papers found; check the manuscript text or loosen filters.")
        cands = self.score_sources(ms, works, ref_sources)
        self.log(f"  {len(works)} similar papers across {len(cands)} journals")

        pre = sorted(cands.values(), key=lambda c: -(c.text_fit + 0.1 * min(c.reference_hits, 5)))[:120]
        details = self.oa.journals([c.journal.openalex_id for c in pre])
        survivors, excluded = [], []
        for c in pre:
            j = details.get(c.journal.openalex_id)
            if j is None:
                continue  # not a journal (repository, book series, conference)
            c.journal = j
            self.indexing.annotate(j)
            reason = self.apply_static_filters(c, criteria, publisher_ids)
            if reason:
                c.excluded_reason = reason
                excluded.append(c)
            else:
                survivors.append(c)
        self.log(f"  {len(survivors)} journals pass publisher/OA/indexing/quartile filters")

        survivors.sort(key=lambda c: -(c.text_fit + 0.1 * min(c.reference_hits, 5)))
        shortlist = survivors[:metrics_for]
        self.log("Collecting editorial metrics...")
        if lookup_top is None:  # cheap providers can afford to check more journals
            lookup_top = getattr(self.llm, "lookup_top", 8)
        self.collect_metrics(shortlist, criteria, web_lookup, lookup_top)
        self.fetcher.close()

        kept = []
        for c in shortlist:
            reason = self.apply_metric_filters(c, criteria)
            if reason:
                c.excluded_reason = reason
                excluded.append(c)
            else:
                kept.append(c)

        if self.llm and kept:
            self.log("Reviewing scope fit of the finalists...")
            # Every finalist gets a scope rating, so none is ranked without an editor's view.
            review = sorted(kept, key=lambda c: -(c.text_fit + 0.1 * min(c.reference_hits, 5)))[:30]
            try:
                self.llm.review_scope(ms, review)
            except Exception as e:
                notes.append(f"Scope review unavailable: {e}")

        max_refs = max((c.reference_hits for c in kept), default=0) or 1
        for c in kept:
            c.score = final_score(c, max_refs)
        kept.sort(key=lambda c: -c.score)
        if not kept:
            notes.append("No journal met every rule. Loosen a threshold or use --if-missing keep.")
        return FindResult(ms, criteria, kept[:top], excluded, notes, trace.lines())


class MetricCache:
    """AI-found metrics per journal, kept for 90 days in ~/.journalfit/metric_cache.json."""

    TTL = 90 * 24 * 3600

    def __init__(self, path=None):
        self.path = path or (config.DATA_DIR / "metric_cache.json")
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def get(self, key: str) -> Metrics | None:
        row = self.data.get(key)
        if not row or time.time() - row["ts"] > self.TTL:
            return None
        m = Metrics(**row["metrics"])
        return m if m.sources else None  # ignore empty entries saved by older versions

    def put(self, key: str, m: Metrics) -> None:
        self.data[key] = {"ts": time.time(), "metrics": asdict(m)}
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            config.ensure_data_dir()
            self.path.write_text(json.dumps(self.data))


def final_score(c: Candidate, max_refs: int) -> float:
    m = c.journal.metrics
    parts = [(0.45, c.text_fit), (0.15, c.reference_hits / max_refs)]
    if c.scope_fit is not None:
        parts.append((0.30, c.scope_fit))
    quality = QUARTILE_SCORE.get(m.quartile or "", 0.4)
    if m.days_to_first_decision is not None:
        quality = 0.8 * quality + 0.2 * max(0.0, 1 - m.days_to_first_decision / 180)
    parts.append((0.10, quality))
    total_w = sum(w for w, _ in parts)
    score = sum(w * v for w, v in parts) / total_w
    if c.scope_fit is not None and c.scope_fit < 0.4:
        score *= 0.6  # an editor would likely desk-reject: rank it well down
    return round(score * 100, 1)
