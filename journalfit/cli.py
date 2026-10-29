"""Command-line interface.

    journalfit data update
    journalfit find paper.docx --no-oa --scopus --max-first-decision 90 --min-acceptance 20 --min-quartile Q2
    journalfit prepare paper.docx --pick 1
    journalfit submit paper.docx --no-oa --scopus --publisher springer
    journalfit assist submissions/<journal>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

from . import config
from .config import Criteria
from .llm import PROVIDERS

console = Console()


def _log(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


def _csv(s: str | None) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def add_filter_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("journal rules")
    g.add_argument("--publisher", help="only these publishers, comma separated (e.g. springer,elsevier)")
    g.add_argument("--exclude-publisher", help="never these publishers, comma separated")
    oa = g.add_mutually_exclusive_group()
    oa.add_argument("--no-oa", action="store_true", help="exclude fully open-access journals (keeps hybrid)")
    oa.add_argument("--oa-only", action="store_true", help="only fully open-access journals")
    g.add_argument("--scopus", action="store_true", help="only journals currently indexed in Scopus")
    g.add_argument("--max-first-decision", type=float, metavar="DAYS",
                   help="skip journals slower than this to first decision")
    g.add_argument("--max-acceptance-days", type=float, metavar="DAYS",
                   help="skip journals where acceptance typically takes longer (submission to acceptance)")
    g.add_argument("--min-acceptance", type=float, metavar="PCT", help="skip journals accepting fewer than PCT%%")
    g.add_argument("--min-quartile", choices=["Q1", "Q2", "Q3", "Q4"], help="e.g. Q2 keeps Q1 and Q2")
    g.add_argument("--quartiles", metavar="LIST", help="exact quartiles to keep, e.g. Q2 or Q1,Q3")
    g.add_argument("--max-apc", type=float, metavar="USD", help="max publication fee for open-access journals")
    g.add_argument("--if-missing", choices=["keep", "skip"], default="keep",
                   help="journals that don't publish a metric you filter on: keep (flagged) or skip")
    g.add_argument("--years", type=int, default=5, help="look at papers from the last N years (default 5)")
    r = p.add_argument_group("run options")
    r.add_argument("--top", type=int, default=20, help="how many journals to list (default 20)")
    r.add_argument("--metrics-for", type=int, default=40, help="collect metrics for this many candidates (default 40)")
    r.add_argument("--no-llm", action="store_true", help="don't use AI (text matching and scraping only)")
    r.add_argument("--provider", choices=list(PROVIDERS), help="AI provider (default: the one in Settings)")
    r.add_argument("--model", help="override the provider's model name")
    r.add_argument("--lookup-top", type=int, help="AI metric lookups for the best N journals (default 8; 20 for DeepSeek)")
    r.add_argument("--no-web-lookup", action="store_true", help="don't let the AI look up missing metrics")
    r.add_argument("--no-browser", action="store_true", help="don't use a headless browser for blocked pages")
    r.add_argument("--out", default="submissions", help="output folder (default ./submissions)")


def criteria_from(a: argparse.Namespace) -> Criteria:
    return Criteria(
        publishers=_csv(a.publisher), exclude_publishers=_csv(a.exclude_publisher),
        no_oa=a.no_oa, oa_only=a.oa_only, scopus=a.scopus,
        max_first_decision_days=a.max_first_decision, min_acceptance_rate=a.min_acceptance,
        max_acceptance_days=a.max_acceptance_days,
        min_quartile=a.min_quartile, max_apc_usd=a.max_apc, if_missing=a.if_missing, years=a.years,
        quartiles=[q.strip().upper() for q in (a.quartiles or "").split(",") if q.strip()],
    )


def _llm(a):
    if getattr(a, "no_llm", False):
        return None
    from . import settings

    llm = settings.make(getattr(a, "provider", None), getattr(a, "model", None))
    _log(f"AI: {llm.name} ({llm.model})")
    return llm


def _cost(llm) -> None:
    if llm:
        console.print(f"[dim]{llm.cost.summary()}[/dim]")


def _load_manuscript(path: str, claude):
    from . import manuscript

    ms = manuscript.parse(path)
    if claude:
        _log("AI is reading the manuscript...")
        try:
            claude.profile_manuscript(ms)
        except Exception as e:  # the parsed title/abstract/keywords are enough to continue
            _log(f"AI couldn't read the manuscript ({e}); continuing with what was read from the file.")
    if not (ms.title or ms.abstract):
        raise SystemExit("Could not find a title or abstract in the manuscript.")
    console.print(f"[bold]Manuscript:[/bold] {ms.title}\n  {ms.word_count} words · {len(ms.keywords)} keywords · "
                  f"{len(ms.references)} references ({len(ms.reference_dois)} with DOI)"
                  + (f" · {ms.field_of_study}" if ms.field_of_study else ""))
    return ms


def _find(a, ms, claude):
    from .finder import Finder
    from .http import HttpClient
    from .report import print_result, write_report

    finder = Finder(HttpClient(), llm=claude, use_browser=not a.no_browser, log=_log)
    res = finder.find(ms, criteria_from(a), top=a.top, metrics_for=a.metrics_for,
                      web_lookup=not a.no_web_lookup, lookup_top=a.lookup_top)
    print_result(res, console)
    path = write_report(res, Path(a.out) / _stem(ms.path))
    console.print(f"\nReport: {path}")
    return res


def _stem(p: str) -> str:
    from .prepare import slug

    return slug(Path(p).stem) or "manuscript"


def _author(a) -> dict:
    from . import settings

    path = config.DATA_DIR / "author.json"
    author = settings.author()
    if path.exists():
        author.update(json.loads(path.read_text()))
    for k in ("name", "email", "affiliation"):
        v = getattr(a, f"author_{k}", None)
        if v:
            author[k] = v
    return author


def cmd_find(a):
    claude = _llm(a)
    _find(a, _load_manuscript(a.manuscript, claude), claude)
    _cost(claude)


def cmd_prepare(a, ms=None, cand=None, claude=None):
    from .prepare import build_package
    from .report import candidate_from_dict

    claude = claude if claude is not None else _llm(a)
    if cand is None:
        short = Path(a.out) / _stem(a.manuscript) / "shortlist.json"
        if not short.exists():
            raise SystemExit(f"No shortlist at {short}. Run `journalfit find` first.")
        data = json.loads(short.read_text())
        ranked = [candidate_from_dict(d) for d in data["ranked"]]
        cand = _pick(ranked, a.pick)
    if ms is None:
        ms = _load_manuscript(a.manuscript, claude)
    console.print(f"Preparing submission package for [bold]{cand.journal.name}[/bold]...")
    supplements = a.supplement or []
    for f in supplements:
        if not Path(f).is_file():
            raise SystemExit(f"Supplementary file not found: {f}")
    from . import settings

    out = build_package(ms, cand, claude, _author(a), Path(a.out) / _stem(ms.path), supplements=supplements,
                        declarations=settings.declarations())
    console.print(f"Package: {out}\n  checklist.md · cover_letter.docx · metadata.json · manuscript copy")
    console.print((out / "checklist.md").read_text())
    _cost(claude)
    return out


def _pick(ranked, pick: str):
    if not ranked:
        raise SystemExit("The shortlist is empty.")
    if pick.isdigit():
        i = int(pick) - 1
        if not 0 <= i < len(ranked):
            raise SystemExit(f"--pick must be 1..{len(ranked)}")
        return ranked[i]
    hits = [c for c in ranked if pick.lower() in c.journal.name.lower()]
    if len(hits) != 1:
        raise SystemExit(f"'{pick}' matches {len(hits)} shortlisted journals; use the rank number.")
    return hits[0]


def cmd_submit(a):
    """One command, one journal: find -> confirm -> prepare -> browser assistant."""
    claude = _llm(a)
    ms = _load_manuscript(a.manuscript, claude)
    res = _find(a, ms, claude)
    if not res.ranked:
        raise SystemExit("No journal meets your rules; nothing to submit.")
    cand = _pick(res.ranked, a.pick)
    console.print(f"\nSelected: [bold]{cand.journal.name}[/bold] (score {cand.score:g})")
    if not a.yes and input("Prepare and open the submission for this journal? [y/N] ").strip().lower() != "y":
        raise SystemExit("Stopped. Nothing was submitted.")
    out = cmd_prepare(a, ms=ms, cand=cand, claude=claude)
    _assist(out, a.url)


def _assist(package: Path, url: str | None):
    try:
        from .submit.assistant import run
    except ImportError:
        raise SystemExit("Install the browser extra first: pip install 'journalfit[browser]' && playwright install chromium")
    run(package, url)


def cmd_assist(a):
    _assist(Path(a.package), a.url)


def cmd_data(a):
    from .http import HttpClient
    from .sources.indexing import SCIMAGO_URL, IndexingData

    config.ensure_data_dir()
    if a.action == "update":
        console.print("Downloading SCImago journal list (Scopus-based SJR and quartiles)...")
        tmp = config.SCIMAGO_PATH.with_suffix(".download")
        try:
            HttpClient().download(SCIMAGO_URL, tmp)
            if not _is_scimago_csv(tmp):
                raise RuntimeError("got a web page instead of the data file")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"SCImago doesn't allow automatic downloads ({e}).\n"
                "Download it yourself: open https://www.scimagojr.com/journalrank.php, click "
                "'Download data', then run:\n  journalfit data import-scimago ~/Downloads/scimagojr*.csv"
            )
        tmp.replace(config.SCIMAGO_PATH)
    elif a.action == "import-scimago":
        if not a.file or not _is_scimago_csv(Path(a.file)):
            raise SystemExit("Usage: journalfit data import-scimago PATH_TO_scimagojr.csv (the file from 'Download data')")
        import shutil

        shutil.copy2(a.file, config.SCIMAGO_PATH)
    elif a.action == "import-scopus":
        if not a.file:
            raise SystemExit("Usage: journalfit data import-scopus PATH_TO_SCOPUS_SOURCE_LIST.xlsx")
        import shutil

        shutil.copy2(a.file, config.SCOPUS_PATH)
    idx = IndexingData.load()
    console.print(f"Data folder: {config.DATA_DIR}")
    console.print(f"  SCImago journals (by ISSN): {len(idx.scimago):,}")
    console.print(f"  Scopus Source List entries: {len(idx.scopus):,}"
                  + ("" if idx.scopus else "  (optional; adds discontinued-title checks and CiteScore)"))
    console.print(f"  Metric overrides file: {config.OVERRIDES_PATH} "
                  f"({'present' if config.OVERRIDES_PATH.exists() else 'not created'})")


def _is_scimago_csv(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(500)
    except OSError:
        return False
    return head.lstrip("﻿").startswith("Rank;") and "Issn" in head


def main(argv=None):
    p = argparse.ArgumentParser(prog="journalfit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="rank journals for a manuscript")
    f.add_argument("manuscript")
    add_filter_args(f)
    f.set_defaults(fn=cmd_find)

    for name, fn, hlp in (("prepare", cmd_prepare, "build the submission package for one shortlisted journal"),
                          ("submit", cmd_submit, "find the best journal and open ONE submission for it")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("manuscript")
        s.add_argument("--pick", default="1", help="rank number or journal name from the shortlist (default 1)")
        s.add_argument("--supplement", action="append", metavar="FILE",
                       help="supplementary file to include (repeat for several)")
        s.add_argument("--author-name")
        s.add_argument("--author-email")
        s.add_argument("--author-affiliation")
        if name == "submit":
            add_filter_args(s)
            s.add_argument("--url", help="submission portal URL, if not found automatically")
            s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
        else:
            s.add_argument("--out", default="submissions")
            s.add_argument("--no-llm", action="store_true")
            s.add_argument("--provider", choices=list(PROVIDERS))
            s.add_argument("--model")
        s.set_defaults(fn=fn)

    s = sub.add_parser("assist", help="reopen the browser assistant for a prepared package")
    s.add_argument("package")
    s.add_argument("--url")
    s.set_defaults(fn=cmd_assist)

    ap = sub.add_parser("app", help="open the point-and-click app in your browser")
    ap.add_argument("--shortcut", action="store_true", help="create a double-click icon on the Desktop")
    ap.add_argument("--port", type=int, default=8765)
    ap.set_defaults(fn=lambda a: __import__("journalfit.app", fromlist=["main"]).main(
        ["--shortcut"] if a.shortcut else ["--port", str(a.port)]))

    d = sub.add_parser("data", help="download or import journal indexing data")
    d.add_argument("action", choices=["update", "import-scimago", "import-scopus", "status"])
    d.add_argument("file", nargs="?")
    d.set_defaults(fn=cmd_data)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
