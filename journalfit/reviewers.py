"""Suggested reviewers, the way a careful author picks them: active researchers who recently
published close to this paper, from different institutions and countries, with no link to any
of the authors.

Sources: OpenAlex (authors of the most similar recent papers). OpenAlex has no email
addresses, so emails are left for the author to add; a reviewer without an email is never
typed into a submission form.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from . import config
from .http import FetchError, HttpClient
from .submit import people

API = "https://api.openalex.org"
MIN_WORKS = 15  # papers in OpenAlex
MIN_H = 6  # h-index
FIELDS = "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index"
# Text similarity to the manuscript (title + abstract), relative to the closest paper found.
RELATIVE_SIMILARITY = 0.35


def _params(**kw) -> dict:
    p = {k: v for k, v in kw.items() if v is not None}
    if config.CONTACT_EMAIL:
        p["mailto"] = config.CONTACT_EMAIL
    if config.OPENALEX_API_KEY:
        p["api_key"] = config.OPENALEX_API_KEY
    return p


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _inst_key(s: str) -> str:
    s = re.sub(r"\b(the|of|and|at|for|de|di|der|la|le)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z]", "", s)


def _same_person(a: dict, b_name: str, b_orcid: str = "") -> bool:
    if a.get("orcid") and b_orcid and a["orcid"] in b_orcid:
        return True
    given, family = people.split_name(b_name)
    return bool(a.get("family")) and _norm(a["family"]) == _norm(family) and \
        _norm(a.get("given", ""))[:1] == _norm(given)[:1]


def works(http: HttpClient, queries: list[str], years: int = 5, per_query: int = 50,
          indexed: set[str] | None = None) -> list[dict]:
    """Recent journal articles close to the paper; only Scopus-indexed journals when that list is loaded."""
    since = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    out, seen = [], set()
    for q in queries:
        try:
            data = http.get_json(f"{API}/works", _params(
                search=q, per_page=per_query, select=FIELDS,
                filter=f"from_publication_date:{since},type:article,primary_location.source.type:journal"))
        except FetchError:
            continue
        for r in data.get("results", []):
            if r["id"] in seen:
                continue
            src = ((r.get("primary_location") or {}).get("source")) or {}
            issns = {re.sub(r"[^0-9X]", "", i.upper()) for i in [src.get("issn_l"), *(src.get("issn") or [])] if i}
            if indexed is not None and not issns & indexed:
                continue  # not a Scopus journal: not the record a reviewer is chosen on
            seen.add(r["id"])
            out.append(r)
    return out


def author_records(http: HttpClient, ids: list[str]) -> dict[str, dict]:
    """OpenAlex author profiles: number of papers, citations, h-index, current institution."""
    out: dict[str, dict] = {}
    short = [i.rsplit("/", 1)[-1] for i in ids]
    for k in range(0, len(short), 40):
        chunk = short[k:k + 40]
        try:
            data = http.get_json(f"{API}/authors", _params(
                filter="openalex:" + "|".join(chunk), per_page=50,
                select="id,display_name,orcid,works_count,cited_by_count,summary_stats,last_known_institutions"))
        except FetchError:
            continue
        for a in data.get("results", []):
            out[a["id"].rsplit("/", 1)[-1]] = a
    return out


def _indexed_issns() -> set[str] | None:
    from .sources.indexing import IndexingData

    idx = IndexingData.load()
    keys = set(idx.scimago) | set(getattr(idx, "scopus", {}) or {})
    return {re.sub(r"[^0-9X]", "", k.upper()) for k in keys} or None


def closest(found: list[dict], summary: str, keep: int = 60) -> list[dict]:
    """The papers really about the same thing as the manuscript, closest first."""
    if not summary or not found:
        return found
    from .similarity import tfidf_similarities
    from .sources.openalex import _abstract

    docs = [(w.get("title") or "") + " " + _abstract(w.get("abstract_inverted_index")) for w in found]
    sims = tfidf_similarities(summary, docs)
    ranked = sorted(zip(sims, range(len(found))), key=lambda t: -t[0])
    floor = RELATIVE_SIMILARITY * float(ranked[0][0]) if ranked else 0.0
    return [dict(found[i], _sim=float(sv) / (float(ranked[0][0]) or 1.0)) for sv, i in ranked[:keep]
            if sv > 0 and sv >= floor]


def suggest(http: HttpClient, queries: list[str], authors: list[dict], n: int = 6, years: int = 5,
            journal_name: str = "", summary: str = "") -> list[dict]:
    """Up to n reviewer suggestions with the papers that make them a good fit (no emails)."""
    found = closest(works(http, queries, years, indexed=_indexed_issns()), summary)
    my_insts = {_inst_key(a.get("institution", "")) for a in authors if a.get("institution")}
    # People who wrote with any of the authors in these papers are not independent.
    linked: set[str] = set()
    for w in found:
        names = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []]
        orcids = [(a.get("author") or {}).get("orcid") or "" for a in w.get("authorships") or []]
        if any(_same_person(me, nm, oc) for me in authors for nm, oc in zip(names, orcids)):
            linked.update(_norm(nm) for nm in names)
    pool: dict[str, dict] = {}
    papers: dict[str, list[dict]] = defaultdict(list)
    for rank, w in enumerate(found):
        for a in w.get("authorships") or []:
            au = a.get("author") or {}
            name, aid = au.get("display_name") or "", au.get("id") or ""
            if not name or not aid or _norm(name) in linked:
                continue
            if any(_same_person(me, name, au.get("orcid") or "") for me in authors):
                continue
            insts = [i.get("display_name", "") for i in a.get("institutions") or []]
            if not insts or any(_inst_key(i) in my_insts for i in insts if i):
                continue  # no affiliation shown, or the same institution as an author
            entry = pool.setdefault(aid, {"name": name, "orcid": (au.get("orcid") or "").rsplit("/", 1)[-1],
                                          "institution": insts[0], "country": (a.get("countries") or [""])[0],
                                          "score": 0.0})
            # Closer papers count more; a senior (last or corresponding) author counts more.
            position = a.get("author_position")
            closeness = w.get("_sim", 1.0 / (1 + rank / 10))
            entry["score"] += closeness * (1.3 if position == "last" or a.get("is_corresponding") else 1.0)
            papers[aid].append({"title": w.get("title") or "", "year": w.get("publication_year"),
                                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                                "journal": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name", "")})
    ranked = sorted(pool.items(), key=lambda kv: -kv[1]["score"])[:60]
    # An established researcher: a real publication record, not a one-off paper.
    records = author_records(http, [aid for aid, _ in ranked])
    import math

    def standing(aid: str) -> float:
        r = records.get(aid.rsplit("/", 1)[-1]) or {}
        h = (r.get("summary_stats") or {}).get("h_index") or 0
        return h

    ranked = [(aid, e) for aid, e in ranked
              if (records.get(aid.rsplit("/", 1)[-1]) or {}).get("works_count", 0) >= MIN_WORKS
              and standing(aid) >= MIN_H and len(people.split_name(e["name"])[0]) >= 2]
    ranked.sort(key=lambda kv: -kv[1]["score"] * (1 + math.log1p(standing(kv[0]))))
    out, used_insts, countries, used_papers = [], set(), defaultdict(int), set()
    for aid, e in ranked:
        rec = records.get(aid.rsplit("/", 1)[-1]) or {}
        e["h_index"] = (rec.get("summary_stats") or {}).get("h_index")
        e["works"] = rec.get("works_count")
        key = _inst_key(e["institution"])
        if key in used_insts or (e["country"] and countries[e["country"]] >= 2):
            continue  # spread over institutions and countries
        titles = {pp["title"] for pp in papers[aid]}
        if titles & used_papers:
            continue  # co-authors of a reviewer already chosen are not independent of them
        used_papers |= titles
        used_insts.add(key)
        countries[e["country"]] += 1
        given, family = people.split_name(e["name"])
        ps = sorted(papers[aid], key=lambda p: -(p["year"] or 0))[:3]
        reason = "Recently published closely related work: " + "; ".join(
            f"\"{p['title']}\" ({p['journal']}, {p['year']})" for p in ps)
        out.append({"given": given, "family": family, "email": "", "institution": e["institution"],
                    "country": _country_name(e["country"]), "orcid": e["orcid"], "reason": reason[:900],
                    "papers": ps, "openalex": aid, "h_index": e["h_index"], "works": e["works"]})
        if len(out) >= n:
            break
    return out


COUNTRIES = {"US": "United States", "GB": "United Kingdom", "IN": "India", "CN": "China", "DE": "Germany",
             "FR": "France", "IT": "Italy", "ES": "Spain", "CA": "Canada", "AU": "Australia", "JP": "Japan",
             "KR": "Korea, Republic of", "NL": "Netherlands", "BR": "Brazil", "PK": "Pakistan",
             "BD": "Bangladesh", "MY": "Malaysia", "SG": "Singapore", "TR": "Türkiye", "IR": "Iran, Islamic Republic of",
             "SA": "Saudi Arabia", "AE": "United Arab Emirates", "ZA": "South Africa", "NG": "Nigeria", "SE": "Sweden",
             "NO": "Norway", "DK": "Denmark", "FI": "Finland", "CH": "Switzerland", "AT": "Austria", "BE": "Belgium",
             "PT": "Portugal", "GR": "Greece", "PL": "Poland", "IE": "Ireland", "NZ": "New Zealand", "MX": "Mexico",
             "CL": "Chile", "AR": "Argentina", "CO": "Colombia", "ID": "Indonesia", "TH": "Thailand", "VN": "Viet Nam",
             "PH": "Philippines", "EG": "Egypt", "IL": "Israel", "HK": "Hong Kong", "TW": "Taiwan", "RU": "Russian Federation",
             "CZ": "Czechia", "HU": "Hungary", "RO": "Romania", "KE": "Kenya", "GH": "Ghana", "ET": "Ethiopia",
             "LK": "Sri Lanka", "NP": "Nepal", "QA": "Qatar", "OM": "Oman", "KW": "Kuwait", "JO": "Jordan",
             "LB": "Lebanon", "MA": "Morocco", "TN": "Tunisia"}


def _country_name(code: str) -> str:
    return COUNTRIES.get((code or "").upper(), code or "")
