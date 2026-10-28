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


# --------------------------------------------------------------------------- other providers

from journalfit import llm as llm_mod  # noqa: E402
from journalfit.llm import DeepSeek, Gemini, OpenAI, make_llm  # noqa: E402


class FakePost:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return NS(status_code=200, json=lambda p=self.payloads.pop(0): p, text="")


SCHEMA = {"type": "object", "properties": {"letter": {"type": "string"}}, "required": ["letter"],
          "additionalProperties": False}


def test_openai_structured_and_web(monkeypatch):
    post = FakePost([
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"letter": "Hi"}'}]}],
         "usage": {"input_tokens": 1000, "output_tokens": 100}},
        {"output": [{"type": "web_search_call"}, {"type": "web_search_call"},
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
         "usage": {"input_tokens": 5000, "output_tokens": 200}},
    ])
    monkeypatch.setattr(llm_mod.requests, "post", post)
    o = OpenAI("gpt-5-mini", "sk-x")
    assert o.structured("s", "p", SCHEMA) == {"letter": "Hi"}
    assert post.calls[0]["json"]["text"]["format"]["strict"] is True
    assert post.calls[0]["headers"]["Authorization"] == "Bearer sk-x"
    assert o.with_web("s", "p") == "done"
    assert post.calls[1]["json"]["tools"] == [{"type": "web_search"}]
    assert o.cost.searches == 2 and o.cost.input_tokens == 6000 and o.cost.usd > 0


def test_gemini_structured_and_web(monkeypatch):
    post = FakePost([
        {"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": '{"letter": "Yo"}'}]}}],
         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}},
        {"candidates": [{"content": {"parts": [{"text": "web answer"}]},
                         "groundingMetadata": {"webSearchQueries": ["a", "b", "c"]}}],
         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}},
    ])
    monkeypatch.setattr(llm_mod.requests, "post", post)
    g = Gemini("gemini-flash-latest", "AIza")
    assert g.structured("s", "p", SCHEMA) == {"letter": "Yo"}
    assert post.calls[0]["json"]["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert g.with_web("s", "p") == "web answer"
    assert post.calls[1]["json"]["tools"] == [{"google_search": {}}]
    assert g.cost.searches == 3


def test_deepseek_reads_given_pages_for_metrics(monkeypatch):
    answer = '{"days_to_first_decision": 21, "days_to_acceptance": null, "acceptance_rate": 30, ' \
             '"impact_factor": null, "citescore": null, "source_url": "https://pub/j"}'
    post = FakePost([{"choices": [{"message": {"content": answer}}],
                      "usage": {"prompt_tokens": 3000, "completion_tokens": 50}}])
    monkeypatch.setattr(llm_mod.requests, "post", post)
    d = DeepSeek("deepseek-chat", "sk-d")
    assert d.lookup_metrics(Journal("S", "J")) .days_to_first_decision is None  # no pages, no call
    m = d.lookup_metrics(Journal("S", "J"), {"https://pub/j": "Review time 3 weeks. Acceptance rate 30%"})
    assert m.days_to_first_decision == 21 and m.acceptance_rate == 30
    sent = post.calls[0]["json"]
    assert sent["response_format"] == {"type": "json_object"}
    assert "Review time 3 weeks" in sent["messages"][1]["content"]
    assert len(post.calls) == 1


def test_deepseek_rejects_incomplete_json(monkeypatch):
    post = FakePost([{"choices": [{"message": {"content": '{"nope": 1}'}}], "usage": {}}])
    monkeypatch.setattr(llm_mod.requests, "post", post)
    with pytest.raises(LLMError, match="missing letter"):
        DeepSeek("deepseek-chat", "k").structured("s", "p", SCHEMA)


def test_api_error_is_readable(monkeypatch):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: NS(
        status_code=401, json=lambda: {"error": {"message": "Incorrect API key"}}, text=""))
    with pytest.raises(LLMError, match="OpenAI error 401: Incorrect API key"):
        OpenAI("gpt-5-mini", "bad").structured("s", "p", SCHEMA)


def test_make_llm():
    with pytest.raises(LLMError, match="No API key"):
        make_llm("openai", {})
    mix = make_llm("claude-mix", {"ANTHROPIC_API_KEY": "sk-ant"})
    assert mix.model == "claude-opus-5-5" and mix.lookup.model == "claude-haiku-5-5"
    assert mix.cost is mix.lookup.cost  # one combined bill
    assert make_llm("deepseek", {"DEEPSEEK_API_KEY": "k"}).has_web is False
    assert make_llm("gemini", {"GEMINI_API_KEY": "k"}, model="gemini-x").model == "gemini-x"


def test_haiku_uses_basic_web_tools():
    c, msgs = make([resp("ok")])
    c.model = "claude-haiku-5-5"
    c.with_web("s", "p")
    assert msgs.calls[0]["tools"][0]["type"] == "web_search_20250305"
    assert "fallbacks" not in msgs.calls[0]
