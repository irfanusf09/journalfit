"""Claude-powered steps: manuscript profiling, scope review, metric lookup,
author-guideline checks and cover letters.

Every call uses structured JSON output (or a JSON block for web-tool calls) so
results are machine-checked, and opts into server-side refusal fallbacks.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from . import config
from .models import Candidate, Journal, Manuscript, Metrics

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6},
]


class LLMError(RuntimeError):
    pass


class Claude:
    def __init__(self, model: str | None = None):
        self.client = anthropic.Anthropic()
        self.model = model or config.CLAUDE_MODEL

    # -- low-level helpers -------------------------------------------------

    def _create(self, **kw):
        try:
            return self.client.beta.messages.create(
                model=self.model, betas=[FALLBACK_BETA], fallbacks="default", **kw
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic credentials missing or invalid: set ANTHROPIC_API_KEY") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit hit; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API") from e

    @staticmethod
    def _text(resp) -> str:
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        return "".join(b.text for b in resp.content if b.type == "text")

    def structured(self, system: str, prompt: str, schema: dict, effort: str = "medium",
                   max_tokens: int = 16000) -> dict:
        resp = self._create(
            max_tokens=max_tokens, system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude response was cut off (max_tokens)")
        return json.loads(self._text(resp))

    def with_web(self, system: str, prompt: str, effort: str = "medium", max_continuations: int = 4) -> str:
        """Run a request with web search/fetch, resuming server-side pauses."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(max_continuations + 1):
            resp = self._create(max_tokens=16000, system=system, tools=WEB_TOOLS,
                                output_config={"effort": effort}, messages=messages)
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        return self._text(resp)

    @staticmethod
    def _json_block(text: str) -> dict:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
        if not m:
            raise LLMError("Claude did not return a JSON result")
        return json.loads(m.group(1))

    # -- pipeline steps ----------------------------------------------------

    def profile_manuscript(self, ms: Manuscript) -> None:
        """Fill in title/abstract/keywords if parsing missed them and derive search queries."""
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "abstract": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "field_of_study": {"type": "string"},
                "article_type": {"type": "string"},
                "search_queries": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "abstract", "keywords", "field_of_study", "article_type", "search_queries"],
            "additionalProperties": False,
        }
        excerpt = ms.body_text[:20000]
        prompt = (
            "Profile this manuscript for journal matching.\n"
            f"Parsed title: {ms.title or '(not found)'}\n"
            f"Parsed abstract: {ms.abstract or '(not found)'}\n"
            f"Parsed keywords: {', '.join(ms.keywords) or '(not found)'}\n\n"
            f"<manuscript_excerpt>\n{excerpt}\n</manuscript_excerpt>\n\n"
            "Return the exact title and abstract as written (fix only extraction artifacts), "
            "4-8 keywords, the specific field/sub-field, the article type (e.g. original research, "
            "review, short communication, case report), and 5 literature-search queries of 3-7 words "
            "each that together cover the paper's topic, methods and application area. Queries are "
            "run against a scholarly search engine to find papers similar to this one."
        )
        data = self.structured("You are an experienced academic editor.", prompt, schema, effort="low")
        ms.title = ms.title or data["title"]
        ms.abstract = ms.abstract if len(ms.abstract) > 200 else data["abstract"]
        ms.keywords = ms.keywords or data["keywords"]
        ms.field_of_study = data["field_of_study"]
        ms.article_type = data["article_type"]
        ms.search_queries = [q for q in data["search_queries"] if q.strip()][:6]

    def review_scope(self, ms: Manuscript, candidates: list[Candidate]) -> None:
        """Judge each shortlisted journal's scope fit with the evidence we have."""
        schema = {
            "type": "object",
            "properties": {
                "journals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "fit": {"type": "integer", "description": "0-10"},
                            "reason": {"type": "string"},
                        },
                        "required": ["id", "fit", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["journals"],
            "additionalProperties": False,
        }
        blocks = []
        for c in candidates:
            j = c.journal
            blocks.append(
                f"<journal id=\"{j.openalex_id}\">\nName: {j.name}\nPublisher: {j.publisher}\n"
                f"Subject areas: {', '.join(j.subject_areas) or 'unknown'}\n"
                f"Similar papers it published recently ({c.matched_works} found):\n"
                + "\n".join(f"- {t}" for t in c.example_titles[:6]) + "\n</journal>"
            )
        prompt = (
            f"Manuscript title: {ms.title}\nArticle type: {ms.article_type or 'unknown'}\n"
            f"Field: {ms.field_of_study or 'unknown'}\nKeywords: {', '.join(ms.keywords)}\n"
            f"Abstract: {ms.abstract}\n\n" + "\n".join(blocks) + "\n\n"
            "For each journal, rate 0-10 how well this manuscript fits the journal's scope and "
            "readership, as an experienced editor would. 10 = core topic of the journal; 5 = "
            "plausible but peripheral; 0 = out of scope (desk rejection likely). Consider whether "
            "the journal publishes this article type and whether the paper's contribution level "
            "suits it. Give a one-sentence reason grounded in the evidence."
        )
        data = self.structured("You are a senior journal editor advising an author.", prompt, schema)
        by_id = {c.journal.openalex_id: c for c in candidates}
        for row in data["journals"]:
            c = by_id.get(row["id"])
            if c:
                c.scope_fit = max(0, min(10, row["fit"])) / 10
                c.scope_reason = row["reason"]

    def lookup_metrics(self, j: Journal) -> Metrics:
        """Find editorial metrics on the publisher's official pages via web search."""
        system = (
            "You look up journal facts on official sources only: the publisher's journal pages "
            "or the journal's own website. Never estimate, and never use third-party aggregators. "
            "If a value is not published on an official page, return null for it."
        )
        prompt = (
            f"Journal: {j.name}\nPublisher: {j.publisher}\nISSN: {', '.join(j.issns)}\n"
            f"Homepage: {j.homepage or 'unknown'}\n\n"
            "Find, for the most recent period shown: median days from submission to first decision; "
            "median days from submission to acceptance; acceptance rate (%); Journal Impact Factor; "
            "CiteScore. Convert weeks to days (x7). End your reply with a fenced json block:\n"
            '```json\n{"days_to_first_decision": number|null, "days_to_acceptance": number|null, '
            '"acceptance_rate": number|null, "impact_factor": number|null, "citescore": number|null, '
            '"source_url": "official URL the values came from"}\n```'
        )
        data = self._json_block(self.with_web(system, prompt, effort="low"))
        m = Metrics()
        url = data.get("source_url") or "web lookup"
        for name in ("days_to_first_decision", "days_to_acceptance", "acceptance_rate", "impact_factor", "citescore"):
            v = data.get(name)
            if isinstance(v, (int, float)):
                setattr(m, name, float(v))
                m.sources[name] = f"Claude web lookup: {url}"
        return m

    def guidelines(self, ms: Manuscript, j: Journal) -> dict:
        """Read the journal's author guidelines and check the manuscript against them."""
        system = "You are a meticulous editorial assistant preparing a journal submission."
        prompt = (
            f"Journal: {j.name} ({j.publisher}), ISSN {', '.join(j.issns)}, homepage {j.homepage or 'unknown'}.\n"
            "Find the journal's official author guidelines / guide for authors and read them.\n\n"
            f"Manuscript facts: article type {ms.article_type or 'unknown'}; {ms.word_count} words "
            f"(main text incl. abstract, excl. references); abstract {len(ms.abstract.split())} words; "
            f"{len(ms.keywords)} keywords; {ms.figure_count} figures; {ms.table_count} tables; "
            f"{len(ms.references)} references. Reference sample:\n"
            + "\n".join(ms.references[:3]) + "\n\n"
            "End your reply with a fenced json block:\n"
            '```json\n{"guidelines_url": str, "submission_system": "ScholarOne|Editorial Manager|'
            'Elsevier Editorial|Snapp|MDPI SuSy|OJS|other", "submission_url": str|null, '
            '"word_limit": int|null, "abstract_word_limit": int|null, "keyword_limit": str|null, '
            '"reference_style": str, "required_items": [str], '
            '"issues": [{"item": str, "manuscript": str, "requirement": str}], '
            '"article_type_accepted": bool|null}\n```\n'
            "required_items = files/statements needed at submission (e.g. highlights, graphical "
            "abstract, data availability, CRediT, conflict of interest, ethics, suggested reviewers, "
            "title page). issues = concrete mismatches between the manuscript facts and the rules."
        )
        return self._json_block(self.with_web(system, prompt, effort="medium"))

    def cover_letter(self, ms: Manuscript, j: Journal, author: dict, scope_reason: str = "") -> str:
        schema = {
            "type": "object",
            "properties": {"letter": {"type": "string"}},
            "required": ["letter"],
            "additionalProperties": False,
        }
        prompt = (
            f"Write a submission cover letter to the editor of {j.name}.\n"
            f"Corresponding author: {json.dumps(author)}\n"
            f"Title: {ms.title}\nArticle type: {ms.article_type}\nAbstract: {ms.abstract}\n"
            f"Why this journal: {scope_reason}\n\n"
            "Plain text, under 350 words: address the Editor-in-Chief (use 'Dear Editor' if the name "
            "is unknown), state the title and article type, the problem, the main findings and why "
            "they matter to this journal's readers. Include the standard declarations: the work is "
            "original, not published or under consideration elsewhere, all authors approved the "
            "submission, and competing interests are declared. Do not invent results, numbers, "
            "names or affiliations that are not given above; use [placeholders] where needed."
        )
        return self.structured("You write concise, professional academic correspondence.",
                               prompt, schema, effort="medium")["letter"]
