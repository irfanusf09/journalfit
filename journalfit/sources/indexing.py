"""Scopus indexing and SJR/quartile data.

Two sources, used together when available:

* SCImago Journal Rank (free CSV, built from Scopus): SJR, best quartile,
  h-index, subject categories. Downloaded automatically.
* Elsevier's official Scopus Source List (free .xlsx from
  https://www.elsevier.com/products/scopus/content/content-policy-and-selection):
  authoritative indexing status incl. discontinued titles, plus CiteScore when the
  sheet includes it. Import it once with `journalfit data import-scopus FILE`.

If only SCImago is present, a journal listed there counts as Scopus-indexed.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..models import Journal, Metrics

SCIMAGO_URL = "https://www.scimagojr.com/journalrank.php?out=xls"


def norm_issn(s: str) -> str:
    s = re.sub(r"[^0-9Xx]", "", str(s)).upper()
    return s if len(s) == 8 else ""


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class IndexRecord:
    title: str
    sjr: float | None = None
    quartile: str | None = None
    h_index: int | None = None
    citescore: float | None = None
    active: bool | None = None
    is_oa: bool | None = None
    last_year: int | None = None  # last year of Scopus coverage
    categories: list[str] = field(default_factory=list)
    origin: str = ""


class IndexingData:
    def __init__(self):
        self.scimago: dict[str, IndexRecord] = {}
        self.scopus: dict[str, IndexRecord] = {}

    @classmethod
    def load(cls, scimago_path: Path | None = None, scopus_path: Path | None = None) -> "IndexingData":
        self = cls()
        scimago_path = scimago_path or config.SCIMAGO_PATH
        scopus_path = scopus_path or config.SCOPUS_PATH
        if scimago_path.exists():
            self.scimago = parse_scimago(scimago_path.read_text(encoding="utf-8", errors="replace"))
        if scopus_path.exists():
            self.scopus = parse_scopus_xlsx(scopus_path)
        return self

    @property
    def available(self) -> bool:
        return bool(self.scimago or self.scopus)

    def annotate(self, journal: Journal) -> None:
        issns = [norm_issn(i) for i in journal.issns]
        issns = [i for i in issns if i]
        sco = next((self.scopus[i] for i in issns if i in self.scopus), None)
        sci = next((self.scimago[i] for i in issns if i in self.scimago), None)
        if self.scopus:
            journal.scopus_indexed = sco is not None
            journal.scopus_active = sco.active if sco else None
        elif self.scimago:
            journal.scopus_indexed = sci is not None
            journal.scopus_active = sci.active if sci else None
        if sci and sci.is_oa:
            journal.is_oa = True  # SCImago flags fully open-access titles
        if sco:
            journal.metrics.merge(Metrics(citescore=sco.citescore), origin="Scopus Source List")
        if sci:
            journal.metrics.merge(
                Metrics(sjr=sci.sjr, quartile=sci.quartile, h_index=sci.h_index),
                origin="SCImago (Scopus data)",
            )
            if sci.categories and not journal.subject_areas:
                journal.subject_areas = sci.categories[:5]


def parse_scimago(text: str) -> dict[str, IndexRecord]:
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    out: dict[str, IndexRecord] = {}
    for row in reader:
        if (row.get("Type") or "journal").strip().lower() != "journal":
            continue
        q = (row.get("SJR Best Quartile") or "").strip()
        h = _num(row.get("H index"))
        rec = IndexRecord(
            title=(row.get("Title") or "").strip(),
            sjr=_num(row.get("SJR")),
            quartile=q if q in config.QUARTILE_ORDER else None,
            h_index=int(h) if h is not None else None,
            categories=[re.sub(r"\s*\(Q\d\)$", "", c).strip() for c in (row.get("Categories") or "").split(";") if c.strip()],
            is_oa=_yes(row.get("Open Access")),
            last_year=max((int(y) for y in re.findall(r"\d{4}", row.get("Coverage") or "")), default=None),
            origin="scimago",
        )
        for issn in (row.get("Issn") or "").split(","):
            n = norm_issn(issn)
            if n:
                out[n] = rec
    # Coverage that stopped before the latest year in the file means Scopus no longer indexes it.
    latest = max((r.last_year for r in out.values() if r.last_year), default=None)
    if latest:
        for r in out.values():
            if r.last_year:
                r.active = r.last_year >= latest - 1
    return out


def _yes(v) -> bool | None:
    v = (v or "").strip().lower()
    return True if v == "yes" else False if v == "no" else None


def parse_scopus_xlsx(path: Path) -> dict[str, IndexRecord]:
    """Parse Elsevier's Scopus Source List. Column names vary between releases, so match loosely."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: dict[str, IndexRecord] = {}
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = None
        for row in rows:
            cells = [str(c or "").strip().lower() for c in row]
            if any("issn" in c for c in cells) and any("title" in c for c in cells):
                header = cells
                break
        if not header:
            continue

        def col(*names):
            for n in names:
                for i, c in enumerate(header):
                    if n in c:
                        return i
            return None

        c_title = col("source title", "title")
        c_issn, c_eissn = col("print-issn", "issn"), col("e-issn", "eissn")
        c_active = col("active or inactive", "active")
        c_type = col("source type")
        c_cs = col("citescore")
        discontinued_sheet = "discontinued" in ws.title.lower()
        for row in rows:
            if c_type is not None and row[c_type] and "journal" not in str(row[c_type]).lower():
                continue
            active = None
            if c_active is not None and row[c_active]:
                active = str(row[c_active]).strip().lower().startswith("active")
            if discontinued_sheet:
                active = False
            rec = IndexRecord(
                title=str(row[c_title] or "") if c_title is not None else "",
                citescore=_num(row[c_cs]) if c_cs is not None else None,
                active=active, origin="scopus",
            )
            for c in (c_issn, c_eissn):
                if c is not None and row[c]:
                    n = norm_issn(row[c])
                    if n and (n not in out or (discontinued_sheet and out[n].active is not False)):
                        out[n] = rec
    return out
