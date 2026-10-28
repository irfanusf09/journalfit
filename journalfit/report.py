"""Render results to the terminal and to a Markdown report."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .finder import FindResult
from .models import Candidate, Metrics


def _fmt(v, suffix="", none="—"):
    if v is None:
        return none
    if isinstance(v, float):
        v = f"{v:g}"
    return f"{v}{suffix}"


def metrics_line(m: Metrics) -> str:
    return (f"first decision {_fmt(m.days_to_first_decision, ' d', 'n/p')} · "
            f"acceptance {_fmt(m.acceptance_rate, '%', 'n/p')} · "
            f"submission to acceptance {_fmt(m.days_to_acceptance, ' d', 'n/p')} · {m.quartile or 'Q?'} · "
            f"SJR {_fmt(m.sjr)} · IF {_fmt(m.impact_factor)} · CiteScore {_fmt(m.citescore)}")


def print_result(res: FindResult, console: Console | None = None) -> None:
    console = console or Console()
    ms = res.manuscript
    console.print(f"\n[bold]{ms.title or ms.path}[/bold]")
    console.print(f"[dim]{res.criteria.describe()}[/dim]\n")
    t = Table(show_lines=True)
    for col in ("#", "Journal", "Publisher", "Score", "Fit", "1st decision", "Accept.", "Quartile", "Why"):
        t.add_column(col, overflow="fold")
    for i, c in enumerate(res.ranked, 1):
        m = c.journal.metrics
        why = c.scope_reason or (f"{c.matched_works} similar papers" +
                                 (f", {c.reference_hits} of your references" if c.reference_hits else ""))
        if c.warnings:
            why += "\n[yellow]" + "; ".join(c.warnings) + "[/yellow]"
        t.add_row(str(i), c.journal.name, c.journal.publisher, f"{c.score:g}",
                  f"{c.text_fit:.2f}" + (f" / {c.scope_fit:.1f}" if c.scope_fit is not None else ""),
                  _fmt(m.days_to_first_decision, " d", "n/p"), _fmt(m.acceptance_rate, "%", "n/p"),
                  m.quartile or "—", why)
    console.print(t)
    console.print("[dim]Fit = text similarity / editor-style scope rating. n/p = not published by the journal.[/dim]")
    for n in res.notes:
        console.print(f"[yellow]{n}[/yellow]")


def write_report(res: FindResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ms = res.manuscript
    lines = [
        f"# Journal shortlist: {ms.title}",
        "",
        f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} · Rules: {res.criteria.describe()}",
        "",
    ]
    for i, c in enumerate(res.ranked, 1):
        j, m = c.journal, c.journal.metrics
        lines += [
            f"## {i}. {j.name} — score {c.score:g}",
            "",
            f"- **Publisher:** {j.publisher} · **ISSN:** {', '.join(j.issns)} · "
            f"**Access:** {'open access' if (j.is_oa or j.in_doaj) else 'subscription/hybrid'}",
            f"- **Metrics:** {metrics_line(m)}",
            f"- **Fit:** text similarity {c.text_fit:.2f}"
            + (f", scope rating {c.scope_fit * 10:.0f}/10" if c.scope_fit is not None else "")
            + f", {c.matched_works} similar recent papers, {c.reference_hits} of your references",
        ]
        if c.scope_reason:
            lines.append(f"- **Why:** {c.scope_reason}")
        if c.warnings:
            lines.append(f"- **Note:** {'; '.join(c.warnings)}")
        if c.example_titles:
            lines.append("- **Similar papers it published:**")
            lines += [f"  - {t}" for t in c.example_titles[:4]]
        if m.sources:
            lines.append("- **Metric sources:** " + "; ".join(f"{k}: {v}" for k, v in sorted(m.sources.items())))
        if j.homepage:
            lines.append(f"- **Homepage:** {j.homepage}")
        lines.append("")
    if res.excluded:
        lines += ["## Excluded journals that matched the topic", ""]
        for c in excluded_order(res.excluded)[:40]:
            lines.append(f"- {c.journal.name} ({c.journal.publisher}): {c.excluded_reason}")
    path = out_dir / "shortlist.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    data = {
        "manuscript": ms.to_dict(),
        "criteria": asdict(res.criteria),
        "ranked": [asdict(c) for c in res.ranked],
        "excluded": [{"journal": c.journal.name, "reason": c.excluded_reason} for c in res.excluded],
    }
    (out_dir / "shortlist.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    if res.trace:
        (out_dir / "diagnostics.txt").write_text(
            "JournalFit diagnostics: what each metric source returned.\n" + "\n".join(res.trace) + "\n",
            encoding="utf-8")
    return path


def candidate_from_dict(d: dict) -> Candidate:
    from .models import Journal

    jd = dict(d["journal"])
    jd["metrics"] = Metrics(**jd["metrics"])
    d = dict(d)
    d["journal"] = Journal(**jd)
    return Candidate(**d)


def excluded_order(excluded):
    """Near misses first (speed, acceptance, scope), then access/indexing exclusions; best topic match first."""
    basic = ("fully open access", "not open access", "not scopus", "scopus status", "discontinued", "publisher")
    return sorted(excluded, key=lambda c: (c.excluded_reason.lower().startswith(basic), -c.text_fit))
