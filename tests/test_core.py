"""Offline tests: parsing, metric extraction, indexing data, filters and the full
finder pipeline against a stubbed OpenAlex/publisher web."""

from __future__ import annotations

import json

import docx
import pytest

from journalfit import manuscript
from journalfit.config import Criteria
from journalfit.finder import Finder, final_score
from journalfit.http import FetchError
from journalfit.metrics.extract import extract_metrics
from journalfit.metrics.publishers import candidate_urls
from journalfit.models import Candidate, Journal, Metrics
from journalfit.sources.indexing import IndexingData, parse_scimago

ABSTRACT = ("Lithium-sulfur batteries suffer from polysulfide shuttling. We design a graphene oxide "
            "interlayer doped with nitrogen that traps polysulfides and improves cycling stability "
            "of lithium sulfur cells over 500 cycles at high sulfur loading.")


def make_docx(path):
    d = docx.Document()
    d.add_paragraph("Original article")
    d.add_paragraph("Nitrogen-doped graphene oxide interlayers suppress polysulfide shuttling in lithium-sulfur batteries")
    d.add_paragraph("A. Author, B. Author")
    d.add_paragraph("Abstract")
    d.add_paragraph(ABSTRACT)
    d.add_paragraph("Keywords: lithium-sulfur battery; graphene oxide; interlayer; polysulfide")
    d.add_paragraph("1. Introduction")
    d.add_paragraph("Batteries matter. As shown in Figure 1 and Figure 2 and Table 1, results improve.")
    d.add_paragraph("References")
    d.add_paragraph("[1] Smith J, et al. A paper. Carbon 2020. https://doi.org/10.1016/j.carbon.2020.01.001")
    d.add_paragraph("[2] Doe K. Another paper on sulfur cathodes. J Mater Sci 2021. doi:10.1007/s10853-021-0001-2.")
    d.save(path)


def test_parse_docx(tmp_path):
    p = tmp_path / "paper.docx"
    make_docx(p)
    ms = manuscript.parse(p)
    assert ms.title.startswith("Nitrogen-doped graphene oxide")
    assert ms.abstract == ABSTRACT
    assert ms.keywords == ["lithium-sulfur battery", "graphene oxide", "interlayer", "polysulfide"]
    assert ms.reference_dois == ["10.1007/s10853-021-0001-2", "10.1016/j.carbon.2020.01.001"]
    assert len(ms.references) == 2
    assert (ms.figure_count, ms.table_count) == (2, 1)


def test_parse_latex(tmp_path):
    p = tmp_path / "paper.tex"
    p.write_text(r"""
\documentclass{article}
\title{Deep learning for crop yield prediction from satellite images}
\begin{document}
\maketitle
\begin{abstract}
We predict \emph{crop yield} using convolutional networks on Sentinel-2 imagery across 300 districts.
\end{abstract}
\keywords{remote sensing, crop yield, deep learning}
\section{Introduction}
Text here.
\begin{thebibliography}{9}
\bibitem{a} Author A. Paper about yield. Remote Sens. 2020. doi:10.3390/rs12010001
\end{thebibliography}
\end{document}
""")
    ms = manuscript.parse(p)
    assert ms.title == "Deep learning for crop yield prediction from satellite images"
    assert "crop yield using convolutional networks" in ms.abstract
    assert ms.keywords == ["remote sensing", "crop yield", "deep learning"]
    assert ms.reference_dois == ["10.3390/rs12010001"]


@pytest.mark.parametrize("text,field,expected", [
    ("Submission to first decision (median) 21 days Downloads 2023", "days_to_first_decision", 21),
    ("Review time 6.4 weeks Submission to acceptance 14.2 weeks", "days_to_first_decision", 44.8),
    ("Review time 6.4 weeks Submission to acceptance 14.2 weeks", "days_to_acceptance", 99.4),
    ("Acceptance rate 18% Impact factor 9.4", "acceptance_rate", 18),
    ("Acceptance rate 18% Impact factor 9.4", "impact_factor", 9.4),
    ("5-year impact factor 7.0 CiteScore 15.2", "impact_factor", None),
    ("5-year impact factor 7.0 CiteScore 15.2", "citescore", 15.2),
    ("First decision: 17.3 days (median values for papers published in this journal)", "days_to_first_decision", 17.3),
    ("Time to first decision 2 months", "days_to_first_decision", 60.8),
    ("Our acceptance rate is low", "acceptance_rate", None),
])
def test_extract_metrics(text, field, expected):
    assert getattr(extract_metrics(text, "u"), field) == expected


SCIMAGO = """Rank;Sourceid;Title;Type;Issn;SJR;SJR Best Quartile;H index;Publisher;Categories
1;1;Carbon;journal;"00086223, 18733891";2,171;Q1;300;Elsevier Ltd;Materials Science (miscellaneous) (Q1); Chemistry (Q1)
2;2;Journal of Materials Science;journal;"00222461, 15734803";0,8;Q2;200;Springer;Materials Science (Q2)
3;3;Some Proceedings;conference and proceedings;12345678;0,2;Q4;10;X;Y
"""


def test_scimago_parse_and_annotate():
    data = parse_scimago(SCIMAGO)
    assert data["00086223"].quartile == "Q1" and data["18733891"].sjr == 2.171
    assert "12345678" not in data
    idx = IndexingData()
    idx.scimago = data
    j = Journal("S1", "Carbon", issns=["0008-6223"])
    idx.annotate(j)
    assert j.scopus_indexed is True and j.metrics.quartile == "Q1" and j.metrics.h_index == 300
    j2 = Journal("S9", "Unknown", issns=["1111-2222"])
    idx.annotate(j2)
    assert j2.scopus_indexed is False


def test_publisher_urls():
    j = Journal("S1", "Carbon", publisher="Elsevier BV", homepage="https://www.sciencedirect.com/journal/carbon")
    assert candidate_urls(j)[0] == "https://www.sciencedirect.com/journal/carbon/about/insights"
    j = Journal("S2", "J Mater Sci", publisher="Springer Science+Business Media",
                homepage="http://link.springer.com/journal/10853")
    assert "https://link.springer.com/journal/10853" in candidate_urls(j)
    j = Journal("S3", "Mat Res Lett", publisher="Taylor & Francis", homepage="https://www.tandfonline.com/toc/tmrl20/current")
    assert "journalCode=tmrl20" in candidate_urls(j)[0]


def _cand(**m):
    c = Candidate(journal=Journal("S1", "J", metrics=Metrics(**m)), text_fit=0.8)
    return c


def test_metric_filters_and_missing_policy():
    f = Finder.__new__(Finder)
    crit = Criteria(max_first_decision_days=90, min_acceptance_rate=20)
    assert f.apply_metric_filters(_cand(days_to_first_decision=120, acceptance_rate=30), crit).startswith("first decision")
    assert f.apply_metric_filters(_cand(days_to_first_decision=40, acceptance_rate=10), crit).startswith("acceptance rate")
    keep = _cand(days_to_first_decision=40)
    assert f.apply_metric_filters(keep, crit) == "" and keep.warnings == ["acceptance rate not published"]
    crit.if_missing = "skip"
    assert f.apply_metric_filters(_cand(days_to_first_decision=40), crit) == "acceptance rate not published"


def test_scope_veto_lowers_score():
    good = Candidate(journal=Journal("a", "A", metrics=Metrics(quartile="Q1")), text_fit=0.9, scope_fit=0.9)
    off = Candidate(journal=Journal("b", "B", metrics=Metrics(quartile="Q1")), text_fit=1.0, scope_fit=0.2)
    assert final_score(good, 1) > final_score(off, 1)


# --------------------------------------------------------------------------- end to end

def _src(sid, name, publisher, issn, homepage, is_oa=False):
    return {"id": f"https://openalex.org/{sid}", "type": "journal", "display_name": name,
            "host_organization_name": publisher, "issn_l": issn, "issn": [issn],
            "homepage_url": homepage, "is_oa": is_oa, "is_in_doaj": is_oa, "apc_usd": 2500 if is_oa else None,
            "summary_stats": {"h_index": 100}}


SOURCES = {
    "S1": _src("S1", "Carbon", "Elsevier BV", "0008-6223", "https://www.sciencedirect.com/journal/carbon"),
    "S2": _src("S2", "Journal of Materials Science", "Springer", "0022-2461", "http://link.springer.com/journal/10853"),
    "S3": _src("S3", "Batteries", "MDPI", "2313-0105", "https://www.mdpi.com/journal/batteries", is_oa=True),
    "S4": _src("S4", "Slow Journal", "Springer", "1234-5679", "http://link.springer.com/journal/99999"),
}
PAGES = {
    "https://www.sciencedirect.com/journal/carbon/about/insights": "<p>Review time 4 weeks</p><p>Acceptance rate 25%</p>",
    "https://link.springer.com/journal/10853": "<div>Submission to first decision (median) 30 days</div>",
    "https://link.springer.com/journal/99999": "<div>Submission to first decision (median) 200 days</div>",
}


def _work(i, title, abstract, sid):
    return {"id": f"https://openalex.org/W{i}", "title": title, "publication_year": 2024,
            "abstract_inverted_index": {w: [k] for k, w in enumerate(abstract.split())},
            "primary_location": {"source": {"id": f"https://openalex.org/{sid}", "display_name": SOURCES[sid]["display_name"]}}}


WORKS = [
    _work(1, "Graphene interlayer for lithium sulfur batteries", "polysulfide shuttling graphene oxide interlayer lithium sulfur", "S1"),
    _work(2, "Nitrogen doped carbon hosts for sulfur cathodes", "nitrogen doped graphene polysulfide lithium sulfur cycling", "S1"),
    _work(3, "Sulfur cathode interlayers review", "lithium sulfur batteries interlayer polysulfide", "S2"),
    _work(4, "Polysulfide trapping in Li-S cells", "graphene oxide lithium sulfur polysulfide trapping", "S3"),
    _work(5, "Graphene oxide lithium sulfur interlayer cycling", "graphene oxide interlayer lithium sulfur", "S4"),
    _work(6, "Steel fatigue", "steel fatigue crack growth", "S2"),
]


class FakeHttp:
    def get_json(self, url, params=None, ttl=None):
        if url.endswith("/works"):
            if params["filter"].startswith("doi:"):
                return {"results": [WORKS[2]]}
            return {"results": WORKS}
        if "/sources/" in url:
            return SOURCES[url.rsplit("/", 1)[-1]]
        if url.endswith("/publishers"):
            return {"results": [{"id": "https://openalex.org/P1", "display_name": "Springer", "works_count": 10}]}
        raise AssertionError(url)

    def get_text(self, url, params=None, ttl=None, headers=None, timeout=30):
        if url in PAGES:
            return PAGES[url]
        raise FetchError(url)


def test_finder_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("journalfit.config.OVERRIDES_PATH", tmp_path / "none.csv")
    p = tmp_path / "paper.docx"
    make_docx(p)
    ms = manuscript.parse(p)
    idx = IndexingData()
    idx.scimago = parse_scimago(SCIMAGO + '4;4;Slow Journal;journal;"12345679";1;Q1;50;Springer;X\n')
    finder = Finder(FakeHttp(), claude=None, indexing=idx, use_browser=False, log=lambda *_: None)
    crit = Criteria(no_oa=True, scopus=True, max_first_decision_days=90, min_quartile="Q2")
    res = finder.find(ms, crit, top=5)

    names = [c.journal.name for c in res.ranked]
    assert names[0] == "Carbon"  # best topical match, fast, Q1, cited in references
    assert "Journal of Materials Science" in names
    reasons = {c.journal.name: c.excluded_reason for c in res.excluded}
    assert reasons["Batteries"] == "fully open access"
    assert reasons["Slow Journal"].startswith("first decision 200")
    carbon = res.ranked[0].journal.metrics
    assert carbon.days_to_first_decision == 28 and carbon.acceptance_rate == 25 and carbon.quartile == "Q1"
    assert "insights" in carbon.sources["days_to_first_decision"]


def test_report_roundtrip(tmp_path, monkeypatch):
    from journalfit.report import candidate_from_dict, write_report

    monkeypatch.setattr("journalfit.config.OVERRIDES_PATH", tmp_path / "none.csv")
    p = tmp_path / "paper.docx"
    make_docx(p)
    idx = IndexingData()
    idx.scimago = parse_scimago(SCIMAGO)
    finder = Finder(FakeHttp(), indexing=idx, use_browser=False, log=lambda *_: None)
    res = finder.find(manuscript.parse(p), Criteria(scopus=True), top=3)
    write_report(res, tmp_path / "out")
    data = json.loads((tmp_path / "out" / "shortlist.json").read_text())
    c = candidate_from_dict(data["ranked"][0])
    assert c.journal.name == res.ranked[0].journal.name
    assert "## 1." in (tmp_path / "out" / "shortlist.md").read_text()


SCIMAGO_2025 = (
    "Rank;Sourceid;Title;Type;Issn;Publisher;Open Access;Open Access Diamond;SJR;SJR Best Quartile;H index;"
    "Total Docs. (2025);Country;Region;Publisher;Coverage;Categories\n"
    '1;1;Carbon;journal;"00086223, 18733891";Elsevier;No;No;2,171;Q1;300;900;UK;Europe;Elsevier;1963-2025;Materials (Q1)\n'
    '2;2;Batteries;journal;"23130105";MDPI;Yes;No;0,9;Q2;40;300;CH;Europe;MDPI;2015-2025;Energy (Q2)\n'
    '3;3;Old Journal;journal;"11112222";X;No;No;0,3;Q3;20;0;US;NA;X;1990-2001, 2003-2019;Misc (Q3)\n'
)


def test_scimago_2025_layout_oa_and_coverage():
    data = parse_scimago(SCIMAGO_2025)
    assert data["00086223"].quartile == "Q1" and data["00086223"].is_oa is False
    assert data["23130105"].is_oa is True
    assert data["00086223"].active is True and data["11112222"].active is False
    idx = IndexingData()
    idx.scimago = data
    j = Journal("S", "Batteries", issns=["2313-0105"])
    idx.annotate(j)
    assert j.is_oa is True and j.scopus_active is True
    old = Journal("O", "Old Journal", issns=["1111-2222"])
    idx.annotate(old)
    f = Finder.__new__(Finder)
    assert f.apply_static_filters(Candidate(journal=old), Criteria(scopus=True), set()) == "discontinued in Scopus"
