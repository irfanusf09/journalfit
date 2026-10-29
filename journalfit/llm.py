"""AI providers for the judgment steps: manuscript profiling, scope review,
metric lookup, author-guideline checks and cover letters.

Every provider implements two primitives:
  structured(system, prompt, schema) -> dict   JSON that matches `schema`
  with_web(system, prompt) -> str              answer using the provider's web search
Providers without web search (DeepSeek) are given page text the tool fetched itself.

Choose one with make_llm(provider, keys). Usage and an estimated cost are tracked
on `.cost` so the app can show what a run cost.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from . import trace
from .models import Candidate, Journal, Manuscript, Metrics

# Approximate USD prices per million tokens (input, output) and per web search.
# Estimates for display only; check each provider's pricing page.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gemini-flash-latest": (0.75, 3.75),
    "deepseek-flash": (0.30, 1.20),  # peak rates; off-peak is half
    "deepseek-v4-pro": (1.32, 3.96),
}
SEARCH_PRICE = {"anthropic": 0.010, "openai": 0.010, "gemini": 0.014, "deepseek": 0.00075}

PROVIDERS = {
    # id: (label, default model, key setting / env var)
    "claude-haiku": ("Claude Haiku 5.5: cheap, automatic web lookups", "claude-haiku-5-5", "ANTHROPIC_API_KEY"),
    "claude-mix": ("Claude Opus 5.5 + Haiku: best quality, Haiku for lookups", "claude-opus-5-5", "ANTHROPIC_API_KEY"),
    "claude-opus": ("Claude Opus 5.5: best quality, highest cost", "claude-opus-5-5", "ANTHROPIC_API_KEY"),
    "openai": ("OpenAI (ChatGPT API)", "gpt-5-mini", "OPENAI_API_KEY"),
    "gemini": ("Google Gemini", "gemini-flash-latest", "GEMINI_API_KEY"),
    "deepseek": ("DeepSeek: cheapest", "deepseek-flash", "DEEPSEEK_API_KEY"),
}


class LLMError(RuntimeError):
    pass


@dataclass
class Cost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    usd: float = 0.0
    unknown_price: set = field(default_factory=set)

    def add(self, model: str, provider: str, inp: int, out: int, searches: int = 0) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.searches += searches
        price = PRICES.get(model)
        if price is None:
            self.unknown_price.add(model)
        else:
            self.usd += inp / 1e6 * price[0] + out / 1e6 * price[1]
        self.usd += searches * SEARCH_PRICE.get(provider, 0.0)

    def add_requests(self, provider: str, n: int = 1) -> None:
        """Per-request web search/fetch fees billed outside model calls."""
        self.searches += n
        self.usd += n * SEARCH_PRICE.get(provider, 0.0)

    def summary(self) -> str:
        s = (f"AI usage: {self.calls} calls, {self.input_tokens:,} input + {self.output_tokens:,} output tokens"
             + (f", {self.searches} web searches/page reads" if self.searches else "")
             + f". Estimated cost: ${self.usd:.2f}")
        if self.unknown_price:
            s += f" (plus {', '.join(sorted(self.unknown_price))}, price unknown)"
        return s


def _json_from_text(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise LLMError("The AI did not return a JSON result")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise LLMError(f"The AI returned malformed JSON: {e}") from e


# ====================================================================== shared pipeline

PROFILE_SCHEMA = {
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
SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "journals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "fit": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["id", "fit", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["journals"],
    "additionalProperties": False,
}
LETTER_SCHEMA = {
    "type": "object",
    "properties": {"letter": {"type": "string"}},
    "required": ["letter"],
    "additionalProperties": False,
}
METRIC_FIELDS = ("days_to_first_decision", "days_to_acceptance", "acceptance_rate", "impact_factor", "citescore")
METRICS_JSON = ('{"days_to_first_decision": number|null, "days_to_acceptance": number|null, '
                '"acceptance_rate": number|null, "impact_factor": number|null, "citescore": number|null, '
                '"source_url": "URL the values came from"}')
GUIDE_JSON = ('{"guidelines_url": str, "submission_system": "ScholarOne|Editorial Manager|'
              'Elsevier Editorial|Snapp|MDPI SuSy|OJS|other", "submission_url": str|null, '
              '"word_limit": int|null, "abstract_word_limit": int|null, "keyword_limit": str|null, '
              '"reference_style": str, "format_free_first_submission": bool|null, '
              '"required_items": [str], '
              '"issues": [{"item": str, "manuscript": str, "requirement": str}], '
              '"minor_issues": [{"item": str, "manuscript": str, "requirement": str}], '
              '"can_wait": [str], "article_type_accepted": bool|null}')
GUIDE_RULES = (
    "Sort everything by whether it matters at FIRST submission:\n"
    "- required_items: only what the submission system or editor needs now, without which the paper "
    "is returned or desk-rejected: e.g. required declarations/statements (competing interests, funding, "
    "data availability, ethics), a separate title page, an anonymised manuscript for double-blind review, "
    "highlights or a graphical abstract only if mandatory at submission.\n"
    "- issues: only serious mismatches of that kind: well over the word or abstract limit, an article type "
    "the journal doesn't publish, missing abstract or keywords, wrong language.\n"
    "- minor_issues and can_wait: formatting and style details that editors don't desk-reject for and that "
    "are normally fixed at revision or acceptance: reference style and punctuation, fonts, line spacing, "
    "heading levels, figure file formats and resolution, slightly over a limit.\n"
    "- format_free_first_submission: true if the guidelines say any reasonable format/reference style is "
    "accepted for the first submission (e.g. 'Your Paper Your Way', 'format-free submission').\n"
    "Be brief; don't list routine things every journal asks for after acceptance.")


class LLM:
    """Pipeline steps shared by all providers."""

    name = "AI"
    provider = ""
    has_web = True
    lookup: "LLM | None" = None  # optional cheaper model for metric lookups

    @property
    def cost(self) -> Cost:
        if "_cost" not in self.__dict__:
            self.__dict__["_cost"] = Cost()
        return self.__dict__["_cost"]

    def structured(self, system: str, prompt: str, schema: dict, effort: str = "medium") -> dict:
        raise NotImplementedError

    def with_web(self, system: str, prompt: str, effort: str = "medium") -> str:
        raise NotImplementedError

    def web_pages(self, query: str, j: Journal, limit: int = 2, skip: set | None = None) -> dict[str, str]:
        """For providers without built-in web search: {url: text} of official pages found by search."""
        return {}

    # -- steps --------------------------------------------------------------

    def profile_manuscript(self, ms: Manuscript) -> None:
        prompt = (
            "Profile this manuscript for journal matching.\n"
            f"Parsed title: {ms.title or '(not found)'}\n"
            f"Parsed abstract: {ms.abstract or '(not found)'}\n"
            f"Parsed keywords: {', '.join(ms.keywords) or '(not found)'}\n\n"
            f"<manuscript_excerpt>\n{ms.body_text[:20000]}\n</manuscript_excerpt>\n\n"
            "Return the exact title and abstract as written (fix only extraction artifacts), "
            "4-8 keywords, the specific field/sub-field, the article type (e.g. original research, "
            "review, short communication, case report), and 5 literature-search queries of 3-7 words "
            "each that together cover the paper's topic, methods and application area. Queries are "
            "run against a scholarly search engine to find papers similar to this one."
        )
        data = self.structured("You are an experienced academic editor.", prompt, PROFILE_SCHEMA, effort="low")
        ms.title = ms.title or data["title"]
        ms.abstract = ms.abstract if len(ms.abstract) > 200 else data["abstract"]
        ms.keywords = ms.keywords or data["keywords"]
        ms.field_of_study = data["field_of_study"]
        ms.article_type = data["article_type"]
        ms.search_queries = [q for q in data["search_queries"] if q.strip()][:6]

    def review_scope(self, ms: Manuscript, candidates: list[Candidate]) -> None:
        blocks = []
        for c in candidates:
            j = c.journal
            closest = c.example_works or [{"title": t, "year": "", "similarity": ""} for t in c.example_titles[:3]]
            blocks.append(
                f"<journal id=\"{j.openalex_id}\">\nName: {j.name}\nPublisher: {j.publisher}\n"
                f"Subject areas: {', '.join(j.subject_areas) or 'unknown'}\n"
                + (f"Aims and scope (from the journal's site): {j.aims_scope[:1500]}\n" if j.aims_scope
                   else "Aims and scope: not available; judge from the papers it publishes.\n")
                + f"Papers it published in the last few years that are closest to this manuscript "
                f"({c.matched_works} related papers found; similarity 0-1 to this manuscript):\n"
                + "\n".join(f"- {w['title']} ({w.get('year') or 'n.d.'}; similarity {w.get('similarity', '')})"
                             for w in closest[:3])
                + "\n</journal>"
            )
        prompt = (
            f"Manuscript title: {ms.title}\nArticle type: {ms.article_type or 'unknown'}\n"
            f"Field: {ms.field_of_study or 'unknown'}\nKeywords: {', '.join(ms.keywords)}\n"
            f"Abstract: {ms.abstract}\n\n" + "\n".join(blocks) + "\n\n"
            "For each journal, rate 0-10 how well this manuscript fits, as an experienced editor of that "
            "journal would:\n"
            "1. Check the manuscript against the journal's stated aims and scope (when given): topic, "
            "approach (e.g. empirical/econometric vs. theoretical/managerial), discipline and readership. "
            "A journal whose aims exclude this kind of work gets 3 or less even if the topic sounds related.\n"
            "2. Check whether the journal actually publishes papers like this one: the closest papers listed "
            "and how similar they are.\n"
            "3. Consider the article type and whether the contribution level suits the journal.\n"
            "10 = core topic and approach; 5 = plausible but peripheral; 0 = out of scope (desk rejection "
            "likely). The reason (one or two sentences) must say how the paper matches or misses the aims "
            "and scope, and name the closest published paper when there is a good one. Use the ids given."
        )
        data = self.structured("You are a senior journal editor advising an author.", prompt, SCOPE_SCHEMA)
        by_id = {c.journal.openalex_id: c for c in candidates}
        for row in data.get("journals", []):
            c = by_id.get(str(row.get("id")))
            if c and isinstance(row.get("fit"), (int, float)):
                c.scope_fit = max(0, min(10, row["fit"])) / 10
                c.scope_reason = str(row.get("reason", ""))

    def lookup_metrics(self, j: Journal, pages: dict[str, str] | None = None) -> Metrics:
        """Editorial metrics from official pages: web search, or the given page texts."""
        if self.lookup is not None and self.lookup is not self:
            return self.lookup.lookup_metrics(j, pages)
        head = (f"Journal: {j.name}\nPublisher: {j.publisher}\nISSN: {', '.join(j.issns)}\n"
                f"Homepage: {j.homepage or 'unknown'}\n\n")
        ask = ("Find, for the most recent period shown: median days from submission to first decision; "
               "median days from submission to acceptance; acceptance rate (%); Journal Impact Factor; "
               "CiteScore. Convert weeks to days (x7), months to days (x30). End your reply with a fenced "
               f"json block:\n```json\n{METRICS_JSON}\n```")
        system = ("You extract journal facts from official sources only: the publisher's journal pages or "
                  "the journal's own website. Never estimate or use third-party aggregators. If a value is "
                  "not published on an official page, return null for it.")
        if self.has_web:
            text = self.with_web(system, head + ask, effort="low")
        else:
            pages = dict(pages or {})
            pub = re.split(r"[\s,(]", (j.publisher or "").strip())[0]
            for query in (f"{j.name} {pub} journal metrics submission to first decision",
                          f"{j.name} {pub} about the journal insights"):
                found = self.web_pages(query, j, limit=2, skip=set(pages))
                pages.update(found)
                if found:
                    break
            if not pages:
                return Metrics()
            docs = "\n\n".join(f"<page url=\"{u}\">\n{t[:15000]}\n</page>" for u, t in pages.items())
            text = json.dumps(self.structured(system, head + docs + "\n\nFrom these pages only. " + ask,
                                              _metrics_schema(), effort="low"))
        data = _json_from_text(text)
        m = Metrics()
        url = _clean_url(str(data.get("source_url") or "")) or "AI lookup"
        for name in METRIC_FIELDS:
            v = data.get(name)
            if isinstance(v, (int, float)) and v > 0:
                setattr(m, name, float(v))
                m.sources[name] = f"{self.name} lookup: {url}"
        return m

    def guidelines(self, ms: Manuscript, j: Journal, page_text: str = "") -> dict:
        facts = (
            f"Manuscript facts: article type {ms.article_type or 'unknown'}; {ms.word_count} words "
            f"(main text incl. abstract, excl. references); abstract {len(ms.abstract.split())} words; "
            f"{len(ms.keywords)} keywords; {ms.figure_count} figures; {ms.table_count} tables; "
            f"{len(ms.references)} references. Reference sample:\n" + "\n".join(ms.references[:3]) + "\n\n"
        )
        tail = f"End your reply with a fenced json block:\n```json\n{GUIDE_JSON}\n```\n" + GUIDE_RULES
        system = "You are a meticulous editorial assistant preparing a journal submission."
        intro = f"Journal: {j.name} ({j.publisher}), ISSN {', '.join(j.issns)}, homepage {j.homepage or 'unknown'}.\n"
        if self.has_web:
            return _json_from_text(self.with_web(
                system, intro + "Find the journal's official author guidelines / guide for authors and read them.\n\n"
                + facts + tail))
        if not page_text:
            found = self.web_pages(f"{j.name} journal guide for authors submission guidelines", j, limit=1)
            page_text = next(iter(found.values()), "")
        if not page_text:
            raise LLMError("could not find the journal's author guidelines page")
        return _json_from_text(json.dumps(self.structured(
            system, intro + f"<author_guidelines>\n{page_text[:40000]}\n</author_guidelines>\n\n" + facts
            + "Using only these guidelines. " + tail.replace("End your reply with a fenced json block:", "Return JSON:"),
            _guide_schema())))

    def cover_letter(self, ms: Manuscript, j: Journal, author: dict, scope_reason: str = "",
                     requirements: list[str] | None = None, declarations: dict | None = None) -> str:
        needs = [r for r in requirements or [] if re.search(r"cover letter|preprint|declar|statement", r, re.I)]
        prompt = (
            f"Write a submission cover letter to the editor of {j.name}.\n"
            f"Corresponding author: {json.dumps(author)}\n"
            f"Title: {ms.title}\nArticle type: {ms.article_type}\nAbstract: {ms.abstract}\n"
            f"Why this journal: {scope_reason}\n"
            + ("This journal's author guidelines require the following, so include each one that belongs "
               "in a cover letter (use [placeholders] for facts not given, e.g. whether a preprint exists):\n"
               + "\n".join(f"- {r}" for r in needs) + "\n" if needs else "")
            + ("The authors' own declarations, to use word for word (no placeholders for these):\n"
               + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in declarations.items()
                           if isinstance(v, str)) + "\n"
               if declarations else "")
            + "\n"
            "Plain text, under 350 words: address the Editor-in-Chief (use 'Dear Editor' if the name "
            "is unknown), state the title and article type, the problem, the two or three main findings "
            "in plain words (do not copy the abstract) and why they matter to this journal's readers. Include the standard declarations: the work is "
            "original, not published or under consideration elsewhere, all authors approved the "
            "submission, and competing interests are declared. Do not invent results, numbers, "
            "names or affiliations that are not given above; use [placeholders] where needed."
        )
        return self.structured("You write concise, professional academic correspondence.",
                               prompt, LETTER_SCHEMA)["letter"]


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


def _metrics_schema() -> dict:
    props = {n: _nullable("number") for n in METRIC_FIELDS}
    props["source_url"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _issue_list() -> dict:
    return {"type": "array", "items": {
        "type": "object",
        "properties": {"item": {"type": "string"}, "manuscript": {"type": "string"}, "requirement": {"type": "string"}},
        "required": ["item", "manuscript", "requirement"], "additionalProperties": False}}


def _guide_schema() -> dict:
    props = {
        "guidelines_url": {"type": "string"}, "submission_system": {"type": "string"},
        "submission_url": _nullable("string"), "word_limit": _nullable("integer"),
        "abstract_word_limit": _nullable("integer"), "keyword_limit": _nullable("string"),
        "reference_style": {"type": "string"},
        "format_free_first_submission": _nullable("boolean"),
        "required_items": {"type": "array", "items": {"type": "string"}},
        "issues": _issue_list(),
        "minor_issues": _issue_list(),
        "can_wait": {"type": "array", "items": {"type": "string"}},
        "article_type_accepted": _nullable("boolean"),
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ====================================================================== Claude

class Claude(LLM):
    provider = "anthropic"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    WEB_TOOLS = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
    ]
    # Haiku takes the basic tool versions (the dynamic-filtering ones need Opus/Sonnet).
    WEB_TOOLS_BASIC = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5},
    ]

    def __init__(self, model: str = "claude-opus-5-5", api_key: str | None = None):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.name = "Claude"

    def _create(self, **kw):
        import anthropic

        extra = {}
        if self.model != "claude-haiku-5-5":  # server-side fallbacks apply to Opus/Sonnet
            extra = {"betas": [self.FALLBACK_BETA], "fallbacks": "default"}
        try:
            resp = self.client.beta.messages.create(model=self.model, **extra, **kw)
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API key missing or invalid (Settings)") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude rate limit hit; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API") from e
        u = getattr(resp, "usage", None)
        if u is not None:
            stu = getattr(u, "server_tool_use", None)
            self.cost.add(self.model, self.provider,
                          (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0),
                          getattr(u, "output_tokens", 0) or 0,
                          getattr(stu, "web_search_requests", 0) or 0 if stu else 0)
        return resp

    @staticmethod
    def _text(resp) -> str:
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        return "".join(b.text for b in resp.content if b.type == "text")

    def structured(self, system, prompt, schema, effort="medium"):
        resp = self._create(
            max_tokens=16000, system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude response was cut off (max_tokens)")
        return json.loads(self._text(resp))

    def with_web(self, system, prompt, effort="medium", max_continuations=4):
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(max_continuations + 1):
            tools = self.WEB_TOOLS_BASIC if "haiku" in self.model else self.WEB_TOOLS
            resp = self._create(max_tokens=16000, system=system, tools=tools,
                                output_config={"effort": effort}, messages=messages)
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        return self._text(resp)


# ====================================================================== OpenAI (Responses API)

class OpenAI(LLM):
    provider = "openai"
    URL = "https://api.openai.com/v1/responses"

    def __init__(self, model: str, api_key: str):
        self.model, self.key, self.name = model, api_key, "OpenAI"

    def _post(self, body: dict) -> dict:
        try:
            r = requests.post(self.URL, json=body, timeout=600,
                              headers={"Authorization": f"Bearer {self.key}"})
        except requests.RequestException as e:
            raise LLMError(f"Could not reach OpenAI: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"OpenAI error {r.status_code}: {_err(r)}")
        data = r.json()
        u = data.get("usage") or {}
        searches = sum(1 for o in data.get("output", []) if o.get("type") == "web_search_call")
        self.cost.add(self.model, self.provider, u.get("input_tokens", 0), u.get("output_tokens", 0), searches)
        return data

    @staticmethod
    def _text(data: dict) -> str:
        out = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        out.append(c.get("text", ""))
                    elif c.get("type") == "refusal":
                        raise LLMError("OpenAI declined this request")
        return "".join(out)

    def structured(self, system, prompt, schema, effort="medium"):
        data = self._post({
            "model": self.model, "instructions": system, "input": prompt,
            "text": {"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}},
        })
        return json.loads(self._text(data))

    def with_web(self, system, prompt, effort="medium"):
        return self._text(self._post({"model": self.model, "instructions": system, "input": prompt,
                                      "tools": [{"type": "web_search"}]}))


# ====================================================================== Gemini

class Gemini(LLM):
    provider = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str, api_key: str):
        self.model, self.key, self.name = model, api_key, "Gemini"

    def _post(self, body: dict) -> dict:
        try:
            r = requests.post(self.URL.format(model=self.model), json=body, timeout=600,
                              headers={"x-goog-api-key": self.key})
        except requests.RequestException as e:
            raise LLMError(f"Could not reach Gemini: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Gemini error {r.status_code}: {_err(r)}")
        data = r.json()
        u = data.get("usageMetadata") or {}
        cand = (data.get("candidates") or [{}])[0]
        searches = len(((cand.get("groundingMetadata") or {}).get("webSearchQueries")) or [])
        self.cost.add(self.model, self.provider, u.get("promptTokenCount", 0),
                      (u.get("candidatesTokenCount", 0) or 0) + (u.get("thoughtsTokenCount", 0) or 0), searches)
        return data

    @staticmethod
    def _text(data: dict) -> str:
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("Gemini returned no answer (possibly blocked)")
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    @staticmethod
    def _body(system: str, prompt: str) -> dict:
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}]}

    def structured(self, system, prompt, schema, effort="medium"):
        body = self._body(system, prompt)
        body["generationConfig"] = {"responseMimeType": "application/json", "responseJsonSchema": schema}
        return json.loads(self._text(self._post(body)))

    def with_web(self, system, prompt, effort="medium"):
        body = self._body(system, prompt)
        body["tools"] = [{"google_search": {}}]
        return self._text(self._post(body))


# ====================================================================== DeepSeek (OpenAI-compatible chat)

# Publisher and journal-host domains treated as official sources.
OFFICIAL_HOSTS = (
    "sciencedirect.com", "elsevier.com", "cell.com", "springer.com", "springeropen.com", "nature.com",
    "biomedcentral.com", "tandfonline.com", "wiley.com", "mdpi.com", "sagepub.com", "ieee.org",
    "acs.org", "rsc.org", "oup.com", "academic.oup.com", "cambridge.org", "frontiersin.org",
    "plos.org", "emerald.com", "inderscience.com", "iop.org", "aip.org", "degruyter.com",
    "hindawi.com", "bmj.com", "thelancet.com", "jamanetwork.com", "aps.org", "asce.org", "asme.org",
    "taylorfrancis.com", "routledge.com", "karger.com", "thieme.com", "lww.com", "aaas.org", "science.org",
)


PUBLISHER_HOSTS = [
    (r"elsevier|cell press", ("sciencedirect.com", "elsevier.com", "cell.com")),
    (r"springer|palgrave|nature|bmc|biomed central", ("springer.com", "springernature.com", "nature.com",
                                                     "biomedcentral.com", "palgrave.com")),
    (r"wiley|hindawi", ("wiley.com", "hindawi.com")),
    (r"taylor|francis|routledge", ("tandfonline.com", "taylorandfrancis.com")),
    (r"sage", ("sagepub.com",)),
    (r"emerald", ("emerald.com", "emeraldinsight.com")),
    (r"informs|operations research", ("informs.org",)),
    (r"mdpi", ("mdpi.com",)),
    (r"oxford", ("oup.com",)),
    (r"cambridge", ("cambridge.org",)),
    (r"ieee", ("ieee.org",)),
    (r"american chemical", ("acs.org",)),
    (r"royal society of chemistry", ("rsc.org",)),
    (r"frontiers", ("frontiersin.org",)),
    (r"de gruyter", ("degruyter.com",)),
    (r"iop", ("iop.org",)),
    (r"inderscience", ("inderscience.com",)),
]


def publisher_hosts(j: Journal) -> tuple[str, ...]:
    """Domains that count as the journal's own: its homepage plus its publisher's sites."""
    from urllib.parse import urlparse

    hosts = []
    if j.homepage:
        hosts.append(urlparse(j.homepage).netloc.lower().removeprefix("www."))
    for pattern, domains in PUBLISHER_HOSTS:
        if re.search(pattern, j.publisher or "", re.I):
            hosts.extend(domains)
    return tuple(h for h in dict.fromkeys(hosts) if h) or OFFICIAL_HOSTS


def _clean_url(url: str) -> str:
    """Drop #fragments and tracking parameters that search results carry."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    if not url:
        return ""
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query)
         if not k.lower().startswith(("utm_", "gad_", "gclid", "fbclid", "_gl", "_ga", "issharedlink", "page"))]
    return urlunparse(u._replace(query=urlencode(q), fragment=""))


def _is_journal_info_url(url: str, hosts: tuple[str, ...]) -> bool:
    from urllib.parse import urlparse

    u = urlparse(url)
    host = u.netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return False
    path = u.path.lower()
    # Individual articles, PDFs, login/redirect hops and proxies don't carry journal metrics.
    return not (path.endswith(".pdf") or "/content/pdf/" in path or "/doi/" in path or "/article/" in path
                or host.startswith(("idp.", "login.", "auth.")) or "webvpn" in host or "transit" in path)


class DeepSeek(LLM):
    """DeepSeek chat model, with DeepSeek's Search and Fetch APIs for reading official pages.

    The model has no built-in web tool here, so the tool searches, keeps official
    publisher pages, fetches them and gives their text to the model.
    """

    provider = "deepseek"
    has_web = False
    lookup_top = 20  # searches cost ~$0.001, so check more journals
    URL = "https://api.deepseek.com/chat/completions"
    SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
    FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"

    def _tool(self, url: str, body: dict) -> dict | None:
        what = url.rsplit("/", 1)[-1]
        try:
            r = requests.post(url, json=body, timeout=60, headers={"Authorization": f"Bearer {self.key}"})
        except requests.RequestException as e:
            trace.note(f"    DeepSeek {what}: network error {e}")
            return None
        if r.status_code != 200:
            trace.note(f"    DeepSeek {what}: HTTP {r.status_code} {_err(r)[:200]}")
            return None
        self.cost.add_requests(self.provider)
        try:
            return r.json()
        except ValueError:
            trace.note(f"    DeepSeek {what}: response was not JSON: {r.text[:200]!r}")
            return None

    def fetch_text(self, url: str) -> str:
        """Page text via DeepSeek's Fetch API ("" if it couldn't read it)."""
        got = self._tool(self.FETCH_URL, {"url": url})
        return _find_text(got or {})

    def web_pages(self, query, j, limit=2, skip=None):
        from .metrics.publishers import journal_markers

        data = self._tool(self.SEARCH_URL, {"query": query[:128], "count": 10})
        results = _find_list(data or {})
        trace.note(f"    DeepSeek search '{query[:80]}': {len(results)} results"
                   + ("" if results or data is None else f"; response keys: {list(data)[:10] if isinstance(data, dict) else type(data).__name__}"))
        hosts = publisher_hosts(j)
        official = []
        snippets: dict[str, str] = {}
        for r in results:
            url = _clean_url(r.get("url") or r.get("link") or "")
            ok = url and url not in (skip or set()) and _is_journal_info_url(url, hosts)
            trace.note(f"      {'+' if ok else '-'} {url or sorted(r)[:6]}")
            if ok and url not in official:
                official.append(url)
                snippets[url] = " ".join(str(r.get(k) or "") for k in ("title", "summary", "snippet", "description", "content"))
        trace.note(f"    publisher pages kept: {official[:limit] or 'none'}")
        marks = [m.lower() for m in journal_markers(j)]
        pages: dict[str, str] = {}
        for url in official[:limit]:
            text = self.fetch_text(url)
            if text and not any(m in text.lower() for m in marks):
                trace.note(f"    DeepSeek fetch {url}: page is about a different journal, ignored")
                continue
            trace.note(f"    DeepSeek fetch {url}: " + (f"{len(text):,} chars" if text else "no text"))
            if not text:
                # The page couldn't be opened, but the search result's own summary of it may hold the numbers.
                snip = snippets.get(url, "").strip()
                if snip and any(m in snip.lower() for m in marks):
                    text = f"[Search result summary of {url}] {snip}"
                    trace.note(f"    using the search result's summary instead: \"{snip[:160]}\"")
            if text:
                pages[url] = text
        return pages

    def __init__(self, model: str, api_key: str):
        self.model, self.key, self.name = model, api_key, "DeepSeek"

    def structured(self, system, prompt, schema, effort="medium"):
        required = schema.get("required", [])
        ask = (prompt + "\n\nReply with one JSON object only, no other text. Use exactly these keys, "
               "filled with real values (this example only shows the shape):\n"
               + json.dumps(_example(schema), indent=1))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": ask}]
        last_missing: list[str] = []
        for attempt in range(2):
            text = self._chat(messages)
            try:
                out = _unwrap(_json_from_text(text) if not text.strip().startswith("{") else json.loads(text), required)
            except (LLMError, json.JSONDecodeError):
                out = {}
            _fill_optional(out, schema)
            last_missing = [k for k in required if k not in out]
            if not last_missing:
                return out
            trace.note(f"    DeepSeek answer missing {', '.join(last_missing)}; asking again")
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That JSON is missing these keys: " + ", ".join(last_missing)
                          + ". Reply again with one JSON object containing every key from the example."}]
        raise LLMError(f"DeepSeek answer was missing {', '.join(last_missing)}")

    def _chat(self, messages: list[dict]) -> str:
        body = {"model": self.model, "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": 32000}  # room for thinking (on by default) plus the answer
        try:
            r = requests.post(self.URL, json=body, timeout=600, headers={"Authorization": f"Bearer {self.key}"})
        except requests.RequestException as e:
            raise LLMError(f"Could not reach DeepSeek: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"DeepSeek error {r.status_code}: {_err(r)}")
        data = r.json()
        u = data.get("usage") or {}
        self.cost.add(self.model, self.provider, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return data["choices"][0]["message"].get("content") or ""


def _example(schema: dict):
    """A filled-in example of the JSON a schema describes (models copy examples better than schemas)."""
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object":
        return {k: _example(v) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [_example(schema.get("items") or {"type": "string"})]
    return {"string": "...", "number": 0, "integer": 0, "boolean": False}.get(t, "...")


def _fill_optional(out: dict, schema: dict) -> None:
    """Give omitted optional parts (lists, nullable values) their empty value instead of failing."""
    if not isinstance(out, dict):
        return
    for k, sub in (schema.get("properties") or {}).items():
        if k in out:
            continue
        t = sub.get("type")
        if t == "array":
            out[k] = []
        elif isinstance(t, list) and "null" in t:
            out[k] = None


def _unwrap(out, required: list[str]) -> dict:
    """Find the answer when a model nests it, e.g. {"result": {...}} or {"properties": {...}}."""
    if not isinstance(out, dict):
        return {}
    if all(k in out for k in required):
        return out
    for v in out.values():
        if isinstance(v, dict):
            inner = _unwrap(v, required)
            if all(k in inner for k in required):
                return inner
    return out


def _find_list(d) -> list[dict]:
    """Search results under whichever key the API uses ("results", "data", ...)."""
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        for k in ("results", "data", "items", "web_results", "search_results"):
            if k in d:
                return _find_list(d[k])
    return []


def _find_text(d) -> str:
    if isinstance(d, dict):
        for k in ("content", "markdown", "text", "data", "result"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                t = _find_text(v)
                if t:
                    return t
    return ""


def _err(r: requests.Response) -> str:
    try:
        d = r.json()
        e = d.get("error", d)
        return e.get("message", str(e)) if isinstance(e, dict) else str(e)
    except ValueError:
        return r.text[:300]


# ====================================================================== factory

def make_llm(provider: str, keys: dict[str, str], model: str | None = None) -> LLM:
    """keys: {"ANTHROPIC_API_KEY": ..., "OPENAI_API_KEY": ..., ...}"""
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown AI provider '{provider}'. Choose one of: {', '.join(PROVIDERS)}")
    label, default_model, key_name = PROVIDERS[provider]
    key = keys.get(key_name, "")
    if not key:
        raise LLMError(f"No API key for {label.split(':')[0]} (set {key_name} or add it in Settings)")
    model = model or default_model
    if provider == "claude-mix":
        main = Claude(model, key)
        main.lookup = Claude("claude-haiku-5-5", key)
        main.__dict__["_cost"] = main.lookup.cost  # one bill for both models
        return main
    if provider.startswith("claude"):
        return Claude(model, key)
    cls = {"openai": OpenAI, "gemini": Gemini, "deepseek": DeepSeek}[provider]
    return cls(model, key)
