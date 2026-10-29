"""User settings shared by the app and the command line: AI provider, API keys,
author details. Stored in ~/.journalfit/settings.json (readable only by you)."""

from __future__ import annotations

import json
import os

from . import config
from .llm import PROVIDERS

KEY_NAMES = sorted({k for _, _, k in PROVIDERS.values()})
DEFAULT_PROVIDER = "claude-haiku"


def path():
    return config.DATA_DIR / "settings.json"


def load() -> dict:
    try:
        s = json.loads(path().read_text())
    except (OSError, ValueError):
        s = {}
    if s.get("api_key"):  # settings from the first version stored only a Claude key
        s.setdefault("keys", {})["ANTHROPIC_API_KEY"] = s.pop("api_key")
    s.setdefault("keys", {})
    s.setdefault("provider", DEFAULT_PROVIDER)
    return s


def save(s: dict) -> None:
    config.ensure_data_dir()
    path().write_text(json.dumps(s, indent=2))
    os.chmod(path(), 0o600)


def keys(s: dict | None = None) -> dict[str, str]:
    """Saved keys, with environment variables taking priority."""
    s = s if s is not None else load()
    out = dict(s.get("keys", {}))
    for k in KEY_NAMES:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return {k: v for k, v in out.items() if v}


def author(s: dict | None = None) -> dict:
    s = s if s is not None else load()
    return {k: s[f"author_{k}"] for k in ("name", "email", "affiliation") if s.get(f"author_{k}")}


DECLARATION_FIELDS = {
    "competing_interests": "Competing interests",
    "funding": "Funding",
    "data_availability": "Data availability",
    "ethics": "Ethics approval / consent",
    "preprint": "Preprint",
    "author_contributions": "Author contributions",
}


def declarations(s: dict | None = None) -> dict:
    s = s if s is not None else load()
    d = s.get("declarations") or {}
    out = {k: str(d.get(k, "")).strip() for k in DECLARATION_FIELDS if str(d.get(k, "")).strip()}
    if d.get("confirm_standard"):
        out["confirm_standard"] = True
    return out


def make(provider: str | None = None, model: str | None = None):
    """Build the configured AI provider, or raise LLMError explaining what's missing."""
    from .llm import make_llm

    s = load()
    provider = provider or s.get("provider") or DEFAULT_PROVIDER
    model = model or (s.get("models") or {}).get(provider) or None
    return make_llm(provider, keys(s), model)
