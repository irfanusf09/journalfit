"""Core data structures shared across the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Manuscript:
    path: str
    title: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    body_text: str = ""
    references: list[str] = field(default_factory=list)
    reference_dois: list[str] = field(default_factory=list)
    word_count: int = 0
    figure_count: int = 0
    table_count: int = 0
    # Filled in by the LLM profiler when available.
    field_of_study: str = ""
    article_type: str = ""
    search_queries: list[str] = field(default_factory=list)
    # Statements found in the manuscript itself (funding, competing interests, ...)
    declarations: dict = field(default_factory=dict)

    def summary_text(self) -> str:
        """Text used for similarity matching: title + abstract + keywords."""
        return " ".join([self.title, self.abstract, " ".join(self.keywords)]).strip()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("body_text")
        return d


@dataclass
class Metrics:
    """Editorial and quality metrics for a journal. None means unknown."""

    days_to_first_decision: float | None = None
    days_to_acceptance: float | None = None
    acceptance_rate: float | None = None  # percent, 0-100
    impact_factor: float | None = None
    citescore: float | None = None
    sjr: float | None = None
    quartile: str | None = None  # "Q1".."Q4"
    h_index: int | None = None
    apc_usd: float | None = None
    sources: dict[str, str] = field(default_factory=dict)  # metric name -> URL / origin

    def merge(self, other: "Metrics", origin: str | None = None) -> None:
        """Fill fields that are still unknown from `other`."""
        for name in (
            "days_to_first_decision", "days_to_acceptance", "acceptance_rate",
            "impact_factor", "citescore", "sjr", "quartile", "h_index", "apc_usd",
        ):
            if getattr(self, name) is None and getattr(other, name) is not None:
                setattr(self, name, getattr(other, name))
                src = other.sources.get(name) or origin
                if src:
                    self.sources[name] = src


@dataclass
class Journal:
    openalex_id: str
    name: str
    publisher: str = ""
    issns: list[str] = field(default_factory=list)
    homepage: str = ""
    is_oa: bool = False  # fully open access (gold) journal
    in_doaj: bool = False
    scopus_indexed: bool | None = None  # None = could not verify
    scopus_active: bool | None = None
    subject_areas: list[str] = field(default_factory=list)
    aims_scope: str = ""  # from the journal's own pages, when readable
    metrics: Metrics = field(default_factory=Metrics)


@dataclass
class Candidate:
    journal: Journal
    matched_works: int = 0
    text_fit: float = 0.0  # 0-1, similarity of manuscript to the journal's recent papers
    reference_hits: int = 0  # how many of the manuscript's references appeared in this journal
    scope_fit: float | None = None  # 0-1 from the LLM scope review
    scope_reason: str = ""
    score: float = 0.0
    example_titles: list[str] = field(default_factory=list)
    # Closest papers the journal published: {"title", "year", "doi", "similarity"}
    example_works: list[dict] = field(default_factory=list)
    excluded_reason: str = ""
    warnings: list[str] = field(default_factory=list)
