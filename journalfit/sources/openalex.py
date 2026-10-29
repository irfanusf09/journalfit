"""OpenAlex: find papers similar to the manuscript and the journals that published them.

OpenAlex (https://openalex.org) is a free index of ~250M scholarly works and
every journal across all publishers. Docs: https://docs.openalex.org
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .. import config
from ..http import FetchError, HttpClient
from ..models import Journal, Metrics

API = "https://api.openalex.org"
WORK_FIELDS = "id,doi,title,publication_year,primary_location,abstract_inverted_index,cited_by_count"


@dataclass
class Work:
    id: str
    title: str
    abstract: str
    year: int
    source_id: str
    source_name: str
    doi: str = ""


def _short(openalex_url: str) -> str:
    return openalex_url.rsplit("/", 1)[-1]


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    positions = [(p, w) for w, ps in inv.items() for p in ps]
    return " ".join(w for _, w in sorted(positions))


class OpenAlex:
    def __init__(self, http: HttpClient):
        self.http = http

    def _params(self, **kw) -> dict:
        p = {k: v for k, v in kw.items() if v is not None}
        if config.CONTACT_EMAIL:
            p["mailto"] = config.CONTACT_EMAIL
        if config.OPENALEX_API_KEY:
            p["api_key"] = config.OPENALEX_API_KEY
        return p

    def resolve_publishers(self, names: list[str]) -> list[str]:
        """Map publisher names ("springer", "elsevier") to OpenAlex publisher ids."""
        ids: list[str] = []
        for name in names:
            data = self.http.get_json(f"{API}/publishers", self._params(search=name, per_page=5))
            results = data.get("results", [])
            if not results:
                raise ValueError(f"OpenAlex has no publisher matching '{name}'")
            # Top hit plus close variants (e.g. "Springer Nature" and "Springer Science+Business Media").
            top = results[0]
            ids.append(_short(top["id"]))
            for r in results[1:]:
                if name.lower() in r["display_name"].lower() and r.get("works_count", 0) > 0.05 * top.get("works_count", 1):
                    ids.append(_short(r["id"]))
        return sorted(set(ids))

    def search_works(self, query: str, years: int, publisher_ids: list[str] | None = None,
                     limit: int = 200) -> list[Work]:
        since = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
        filters = [f"from_publication_date:{since}", "primary_location.source.type:journal"]
        if publisher_ids:
            filters.append("primary_location.source.host_organization_lineage:" + "|".join(publisher_ids))
        params = self._params(search=query, filter=",".join(filters),
                              per_page=min(limit, 200), select=WORK_FIELDS)
        data = self.http.get_json(f"{API}/works", params)
        return [w for w in (self._to_work(r) for r in data.get("results", [])) if w]

    def works_by_doi(self, dois: list[str]) -> list[Work]:
        out: list[Work] = []
        for i in range(0, len(dois), 40):
            chunk = dois[i:i + 40]
            params = self._params(filter="doi:" + "|".join(chunk), per_page=50, select=WORK_FIELDS)
            try:
                data = self.http.get_json(f"{API}/works", params)
            except FetchError:
                continue
            out.extend(w for w in (self._to_work(r) for r in data.get("results", [])) if w)
        return out

    @staticmethod
    def _to_work(r: dict) -> Work | None:
        src = ((r.get("primary_location") or {}).get("source")) or {}
        if not src.get("id"):
            return None
        return Work(
            id=_short(r["id"]), title=r.get("title") or "",
            abstract=_abstract(r.get("abstract_inverted_index")),
            year=r.get("publication_year") or 0,
            source_id=_short(src["id"]), source_name=src.get("display_name", ""),
            doi=(r.get("doi") or "").replace("https://doi.org/", ""),
        )

    def journals(self, source_ids: list[str]) -> dict[str, Journal]:
        def fetch(sid: str):
            try:
                return sid, self.http.get_json(f"{API}/sources/{sid}", self._params())
            except FetchError:
                return sid, None

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(fetch, source_ids))
        return {sid: self._to_journal(d) for sid, d in rows if d and d.get("type") == "journal"}

    @staticmethod
    def _to_journal(d: dict) -> Journal:
        stats = d.get("summary_stats") or {}
        issns = list(dict.fromkeys([i for i in [d.get("issn_l"), *(d.get("issn") or [])] if i]))
        return Journal(
            openalex_id=_short(d["id"]),
            name=d.get("display_name", ""),
            publisher=d.get("host_organization_name") or "",
            issns=issns,
            homepage=d.get("homepage_url") or "",
            is_oa=bool(d.get("is_oa")),
            in_doaj=bool(d.get("is_in_doaj")),
            subject_areas=[t.get("display_name", "") for t in (d.get("topics") or [])[:5]],
            metrics=Metrics(
                apc_usd=d.get("apc_usd"),
                h_index=stats.get("h_index"),
                sources={"apc_usd": "OpenAlex", "h_index": "OpenAlex"},
            ),
        )
