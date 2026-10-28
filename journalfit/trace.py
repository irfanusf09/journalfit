"""Run diagnostics: what was fetched, blocked or found, per journal.

Written to diagnostics.txt next to the shortlist so problems with a publisher
site or a search API can be seen without re-running. Never contains API keys.
"""

from __future__ import annotations

import re
import threading

_local = threading.local()


def start() -> None:
    _local.lines = []


def note(msg: str) -> None:
    lines = getattr(_local, "lines", None)
    if lines is not None:
        lines.append(msg)


def lines() -> list[str]:
    return list(getattr(_local, "lines", None) or [])


def snippets(text: str, words=("decision", "acceptance", "review time", "impact factor"), width=90) -> list[str]:
    """Short excerpts around metric words, to see why a pattern did or didn't match."""
    out = []
    for w in words:
        for m in list(re.finditer(re.escape(w), text, re.I))[:2]:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            out.append("…" + text[a:b].replace("\n", " ") + "…")
    return out[:6]
