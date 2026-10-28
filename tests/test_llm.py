"""Claude layer with a stubbed API client: request shape, pause_turn resume, parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from journalfit.llm import Claude, LLMError
from journalfit.models import Candidate, Journal, Manuscript


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def resp(text, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=text)])


def make(responses):
    c = Claude.__new__(Claude)
    c.model = "claude-opus-5-5"
    msgs = FakeMessages(responses)
    c.client = NS(beta=NS(messages=msgs))
    return c, msgs


def test_profile_fills_gaps_and_uses_structured_output():
    data = {"title": "T", "abstract": "A" * 300, "keywords": ["k1", "k2"], "field_of_study": "Materials",
            "article_type": "original research", "search_queries": ["q one", "q two", " "]}
    c, msgs = make([resp(json.dumps(data))])
    ms = Manuscript(path="x", title="Parsed title", body_text="body")
    c.profile_manuscript(ms)
    assert ms.title == "Parsed title" and ms.abstract == "A" * 300
    assert ms.search_queries == ["q one", "q two"]
    call = msgs.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]


def test_web_lookup_resumes_pause_turn_and_parses_json():
    final = 'Found it.\n```json\n{"days_to_first_decision": 35, "acceptance_rate": null, ' \
            '"days_to_acceptance": null, "impact_factor": 6.1, "citescore": null, ' \
            '"source_url": "https://example.org/j/insights"}\n```'
    c, msgs = make([resp("", stop="pause_turn"), resp(final)])
    m = c.lookup_metrics(Journal("S1", "J", issns=["1234-5678"]))
    assert m.days_to_first_decision == 35 and m.impact_factor == 6.1 and m.acceptance_rate is None
    assert "example.org" in m.sources["days_to_first_decision"]
    assert len(msgs.calls) == 2 and msgs.calls[1]["messages"][1]["role"] == "assistant"


def test_refusal_raises():
    c, _ = make([NS(stop_reason="refusal", content=[])])
    with pytest.raises(LLMError):
        c.structured("s", "p", {"type": "object", "properties": {}, "additionalProperties": False})


def test_review_scope_maps_ratings():
    cands = [Candidate(journal=Journal("S1", "A")), Candidate(journal=Journal("S2", "B"))]
    out = {"journals": [{"id": "S1", "fit": 9, "reason": "core"}, {"id": "S2", "fit": 2, "reason": "off"}]}
    c, _ = make([resp(json.dumps(out))])
    c.review_scope(Manuscript(path="x", title="t"), cands)
    assert (cands[0].scope_fit, cands[1].scope_fit) == (0.9, 0.2)
