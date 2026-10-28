"""Browser assistant for the submission portal (ScholarOne, Editorial Manager,
Elsevier, Snapp, MDPI SuSy, OJS, ...).

Design choices:
* A visible browser with a persistent profile: you log in yourself once (handles
  2FA/CAPTCHA/SSO, no passwords stored by this tool) and stay logged in.
* Page-by-page autofill driven from the terminal. Fields are matched by their
  labels, so the same code works across submission systems and their frames.
* The tool never clicks buttons. Moving between steps and the final "Submit"
  are always done by you.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .. import config

COLLECT_JS = r"""
(startIdx) => {
  const out = [];
  let idx = startIdx;
  const labelFor = (el) => {
    const bits = [];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) bits.push(l.innerText);
    }
    const wrap = el.closest('label'); if (wrap) bits.push(wrap.innerText);
    for (const a of ['aria-label', 'placeholder', 'name', 'id', 'title']) {
      if (el.getAttribute(a)) bits.push(el.getAttribute(a));
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\s+/).forEach(id => { const n = document.getElementById(id); if (n) bits.push(n.innerText); });
    // Table-based forms (ScholarOne, Editorial Manager): text of the row / previous cell.
    const cell = el.closest('td, div.form-group, div.field, li');
    if (cell) {
      const prev = cell.previousElementSibling; if (prev) bits.push(prev.innerText.slice(0, 200));
      const row = cell.closest('tr'); if (row) bits.push(row.innerText.slice(0, 200));
    }
    return bits.join(' | ').replace(/\s+/g, ' ').trim();
  };
  document.querySelectorAll('input, textarea').forEach(el => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'password'].includes(type)) return;
    if (el.disabled || el.readOnly) return;
    const r = el.getBoundingClientRect();
    if (type !== 'file' && (r.width === 0 || r.height === 0)) return;
    el.setAttribute('data-jf-idx', String(idx));
    out.push({idx: idx++, type, tag: el.tagName.toLowerCase(), label: labelFor(el), value: el.value || ''});
  });
  return out;
}
"""

# Order matters: more specific labels first.
FIELD_RULES: list[tuple[str, re.Pattern]] = [
    ("running_title", re.compile(r"running[\s_-]*(head|title)|short[\s_-]*title", re.I)),
    ("title", re.compile(r"\b(full[\s_-]*)?(manuscript|article|paper|submission)?[\s_-]*title\b", re.I)),
    ("abstract", re.compile(r"\babstract\b", re.I)),
    ("keywords", re.compile(r"key[\s_-]*words?|index terms", re.I)),
    ("cover_letter", re.compile(r"cover[\s_-]*letter|comments?\s+to\s+(the\s+)?editor", re.I)),
]
FILE_RULES: list[tuple[str, re.Pattern]] = [
    ("cover_letter_file", re.compile(r"cover[\s_-]*letter", re.I)),
    ("manuscript_file", re.compile(r"manuscript|main[\s_-]*(document|file)|article file|upload|file", re.I)),
]


@dataclass
class Plan:
    values: dict[str, str]
    files: dict[str, str]


def load_plan(package_dir: Path) -> tuple[Plan, dict]:
    meta = json.loads((package_dir / "metadata.json").read_text(encoding="utf-8"))
    cover_txt = package_dir / "cover_letter.txt"
    words = meta["title"].split()
    values = {
        "title": meta["title"],
        "running_title": " ".join(words[:8]) if len(meta["title"]) > 60 else meta["title"],
        "abstract": meta["abstract"],
        "keywords": "; ".join(meta["keywords"]),
        "cover_letter": cover_txt.read_text(encoding="utf-8") if cover_txt.exists() else "",
    }
    manuscript = next(package_dir.glob("manuscript.*"), None)
    files = {
        "manuscript_file": str(manuscript) if manuscript else "",
        "cover_letter_file": str(package_dir / "cover_letter.docx") if (package_dir / "cover_letter.docx").exists() else "",
    }
    return Plan(values, {k: v for k, v in files.items() if v}), meta


# Person-detail fields (e.g. an author's "Title: Dr/Prof") must never get manuscript text.
PERSON_FIELD = re.compile(r"author|salutation|prefix|honorific|first[\s_-]*name|last[\s_-]*name|"
                          r"job|position|degree|reviewer|e-?mail|institution|department", re.I)


def match_field(label: str, is_file: bool) -> str | None:
    if not is_file and PERSON_FIELD.search(label):
        return None
    for key, rx in (FILE_RULES if is_file else FIELD_RULES):
        if rx.search(label):
            return key
    return None


def fill_page(page, plan: Plan, log=print) -> int:
    filled = 0
    used_files: set[str] = set()
    idx = 0
    for frame in page.frames:
        try:
            fields = frame.evaluate(COLLECT_JS, idx)
        except Exception:
            continue  # cross-origin or detached frame
        idx += len(fields) + 1
        for f in fields:
            is_file = f["type"] == "file"
            key = match_field(f["label"], is_file)
            if not key:
                continue
            loc = frame.locator(f'[data-jf-idx="{f["idx"]}"]')
            try:
                if is_file:
                    path = plan.files.get(key)
                    if not path or path in used_files:
                        continue
                    loc.set_input_files(path)
                    used_files.add(path)
                    log(f"  uploaded {Path(path).name}  ->  {f['label'][:70]}")
                else:
                    value = plan.values.get(key)
                    if not value or f["value"].strip():
                        continue  # never overwrite what is already there
                    if key == "abstract" and f["tag"] != "textarea":
                        continue
                    loc.fill(value)
                    log(f"  filled {key:<13} ->  {f['label'][:70]}")
                filled += 1
            except Exception as e:
                log(f"  could not fill {key}: {e}")
    return filled


def run(package_dir: Path, url: str | None = None) -> None:
    from playwright.sync_api import sync_playwright

    plan, meta = load_plan(package_dir)
    url = url or meta.get("submission_url")
    if not url:
        raise SystemExit("No submission URL known for this journal. Pass --url (from the journal's 'Submit' link).")
    config.ensure_data_dir()
    print(f"\nJournal: {meta['journal']}\nPortal:  {url}")
    print("A browser window opens. Log in if asked and start a new submission.\n"
          "On each form page come back here and press Enter to autofill it.\n"
          "This tool never clicks buttons. You check every page and click Submit yourself.\n")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=False,
                                                    accept_downloads=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        while True:
            cmd = input("[Enter] autofill current page   [q] quit > ").strip().lower()
            if cmd == "q":
                break
            page = ctx.pages[-1]  # portals sometimes open the form in a new tab
            n = fill_page(page, plan)
            print(f"  {n} field(s) filled on this page." if n else "  Nothing to fill on this page.")
        ctx.close()
    print("Browser closed. If you did not click Submit, nothing was submitted.")
