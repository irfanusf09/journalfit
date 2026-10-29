"""Build a submission package for one journal: manuscript copy, cover letter,
guideline checklist and the metadata the submission forms ask for."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .models import Candidate, Manuscript


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def build_package(ms: Manuscript, cand: Candidate, llm, author: dict, out_root: Path, http=None,
                  supplements: list[str] | None = None, declarations: dict | None = None) -> Path:
    j = cand.journal
    out = out_root / slug(j.name)
    out.mkdir(parents=True, exist_ok=True)
    src = Path(ms.path)
    shutil.copy2(src, out / f"manuscript{src.suffix}")
    supp_dir = out / "supplementary"
    if supp_dir.exists():
        shutil.rmtree(supp_dir)  # the package always reflects the files given this time
    for f in supplements or []:
        supp_dir.mkdir(exist_ok=True)
        shutil.copy2(f, supp_dir / Path(f).name)

    guide: dict = {}
    if llm:
        try:
            page = ""
            if not llm.has_web:  # e.g. DeepSeek: find and read the guidelines page ourselves
                from .http import HttpClient
                from .metrics.publishers import PageFetcher, find_guidelines_text

                fetcher = PageFetcher(http or HttpClient(), remote=getattr(llm, "fetch_text", None))
                try:
                    url, page = find_guidelines_text(j, fetcher)
                finally:
                    fetcher.close()
            guide = llm.guidelines(ms, j, page)
            if page and not guide.get("guidelines_url"):
                guide["guidelines_url"] = url
        except Exception as e:
            guide = {"error": str(e)}
        letter = llm.cover_letter(ms, j, author, cand.scope_reason, guide.get("required_items") or [],
                                  declarations or {})
        _write_docx(out / "cover_letter.docx", letter)
        (out / "cover_letter.txt").write_text(letter, encoding="utf-8")

    metadata = {
        "journal": j.name, "publisher": j.publisher, "issn": j.issns,
        "title": ms.title, "abstract": ms.abstract, "keywords": ms.keywords,
        "article_type": ms.article_type, "word_count": ms.word_count,
        "figures": ms.figure_count, "tables": ms.table_count,
        "corresponding_author": author,
        "submission_system": guide.get("submission_system"),
        "submission_url": guide.get("submission_url"),
        "guidelines_url": guide.get("guidelines_url"),
        "supplementary_files": sorted(p.name for p in supp_dir.glob("*")) if supp_dir.exists() else [],
        "declarations": declarations or {},
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (out / "checklist.md").write_text(_checklist(ms, cand, guide, metadata["supplementary_files"], declarations or {}),
                                      encoding="utf-8")
    return out


def _write_docx(path: Path, text: str) -> None:
    import docx

    d = docx.Document()
    for para in text.split("\n\n"):
        d.add_paragraph(para.strip())
    d.save(str(path))


def _checklist(ms: Manuscript, cand: Candidate, guide: dict, supplements: list[str] = (),
               declarations: dict | None = None) -> str:
    j = cand.journal
    lines = [f"# Submission checklist — {j.name}", ""]
    if guide.get("error"):
        lines += [f"> Could not read the author guidelines automatically: {guide['error']}",
                  "> Check them on the journal website before submitting.", ""]
    if guide.get("guidelines_url"):
        lines.append(f"Author guidelines: {guide['guidelines_url']}")
    if guide.get("submission_url"):
        lines.append(f"Submission site ({guide.get('submission_system') or 'unknown system'}): {guide['submission_url']}")
    if guide.get("format_free_first_submission"):
        lines += ["", "Good news: this journal accepts any reasonable format and reference style for the "
                      "first submission. Formatting can wait until revision."]
    lines.append("")
    if guide.get("article_type_accepted") is False:
        lines += [f"**Warning:** the guidelines suggest this journal does not accept '{ms.article_type}'.", ""]

    issues = guide.get("issues") or []
    items = guide.get("required_items") or ["Competing interests statement", "Funding statement",
                                            "Data availability statement", "Author contributions"]
    lines.append("## Must do before submitting")
    lines += [f"- [ ] **{i.get('item')}**: yours = {i.get('manuscript')}; required = {i.get('requirement')}"
              for i in issues]
    lines += [f"- [ ] {item}" for item in items]
    lines += ["- [ ] All co-authors have approved this version and the journal choice",
              "- [ ] The manuscript is not under consideration at any other journal",
              "- [ ] Cover letter read, and [placeholders] filled in"]
    if declarations:
        from .settings import DECLARATION_FIELDS

        lines += ["", "## Your declarations (used in the cover letter and the submission forms)"]
        lines += [f"- {DECLARATION_FIELDS.get(k, k)}: {v}" for k, v in declarations.items() if k in DECLARATION_FIELDS]
        if declarations.get("confirm_standard"):
            lines.append("- Standard author confirmations: you asked the tool to tick these on the journal's site")
    else:
        lines += ["", "Tip: add your declarations (competing interests, funding, data availability...) "
                      "in Settings and they'll be filled in for you."]
    if supplements:
        lines += ["", "## Supplementary files in this package"] + [f"- {n}" for n in supplements]

    lines += ["", "## Facts"]
    for k, label in (("word_limit", "Word limit"), ("abstract_word_limit", "Abstract limit"),
                     ("keyword_limit", "Keywords"), ("reference_style", "Reference style")):
        if guide.get(k):
            lines.append(f"- {label}: {guide[k]}")
    lines.append(f"- Your manuscript: {ms.word_count} words, abstract {len(ms.abstract.split())} words, "
                 f"{len(ms.keywords)} keywords, {ms.figure_count} figures, {ms.table_count} tables")

    minor = guide.get("minor_issues") or []
    later = guide.get("can_wait") or []
    if minor or later:
        lines += ["", "## Can wait (formatting; usually only needed at revision or acceptance)"]
        lines += [f"- {i.get('item')}: yours = {i.get('manuscript')}; journal style = {i.get('requirement')}" for i in minor]
        lines += [f"- {x}" for x in later]
    return "\n".join(lines) + "\n"
