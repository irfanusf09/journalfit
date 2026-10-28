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
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

COLLECT_JS = r"""
(startIdx) => {
  const out = [];
  let idx = startIdx;
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const rootOf = (el) => el.getRootNode();
  const byId = (el, id) => {
    const r = rootOf(el);
    return (r.getElementById && r.getElementById(id)) || document.getElementById(id);
  };
  // Label texts, nearest first: explicit labels and attributes, then text just before the
  // field, walking up through the page structure (modern forms put labels in nearby blocks).
  const labelsFor = (el) => {
    const bits = [];
    const add = (t) => { t = (t || '').replace(/\s+/g, ' ').trim(); if (t) bits.push(t.slice(0, 200)); };
    if (el.id) {
      const r = rootOf(el);
      const l = r.querySelector && r.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) add(txt(l));
    }
    const wrap = el.closest('label'); if (wrap) add(txt(wrap));
    for (const a of ['aria-label', 'placeholder', 'data-placeholder', 'title', 'name', 'id', 'data-testid']) add(el.getAttribute(a));
    for (const a of ['aria-labelledby', 'aria-describedby']) {
      const v = el.getAttribute(a);
      if (v) v.split(/\s+/).forEach(id => { const n = byId(el, id); if (n) add(txt(n)); });
    }
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) add(txt(lg)); }
    let node = el;
    for (let depth = 0; depth < 6 && node; depth++) {
      let sib = node.previousElementSibling, hops = 0;
      while (sib && hops < 3) {
        if (!sib.matches('input, textarea, select') && txt(sib)) { add(txt(sib).slice(0, 160)); break; }
        sib = sib.previousElementSibling; hops++;
      }
      if (node.tagName === 'TD') { const row = node.closest('tr'); if (row) add(txt(row)); }
      node = node.parentElement || (rootOf(node) && rootOf(node).host) || null;
    }
    return bits;
  };
  const all = [];
  const walk = (r) => {
    r.querySelectorAll('input, textarea, select, [contenteditable="true"], [contenteditable=""]').forEach(e => all.push(e));
    r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    const rich = el.isContentEditable && tag !== 'input' && tag !== 'textarea';
    if (rich && el.parentElement && el.parentElement.closest('[contenteditable="true"]')) continue;
    const type = rich ? 'richtext' : tag === 'select' ? 'select' : (el.getAttribute('type') || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    if (type !== 'file' && !visible(el)) continue;
    el.setAttribute('data-jf-idx', String(idx));
    let value = el.value || '';
    if (rich) value = txt(el);
    if (tag === 'select') value = el.selectedIndex > 0 ? txt(el.options[el.selectedIndex]) : '';
    const bits = labelsFor(el);
    out.push({idx: idx++, type, tag, bits, label: bits.join(' | '), value, multiple: !!el.multiple,
              options: tag === 'select' ? [...el.options].map(o => txt(o)) : []});
  }
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
    ("article_type", re.compile(r"(article|manuscript|submission|paper)[\s_-]*type|type\s+of\s+(article|manuscript|paper|submission)", re.I)),
]
FILE_RULES: list[tuple[str, re.Pattern]] = [
    ("cover_letter_file", re.compile(r"cover[\s_-]*letter", re.I)),
    ("supplementary_file", re.compile(r"supplement|supporting[\s_-]*(info|material|file)|additional[\s_-]*file|"
                                      r"appendi(x|ces)|electronic[\s_-]*supplementary|\bESM\b|\bSI\b", re.I)),
    ("manuscript_file", re.compile(r"manuscript|main[\s_-]*(document|file)|article file|upload|file", re.I)),
]


@dataclass
class Plan:
    values: dict[str, str]
    files: dict[str, str]
    supplements: list[str] = field(default_factory=list)


def running_title(title: str) -> str:
    """Short title: the part before a colon or question mark if that reads well, else the first 8 words."""
    if len(title) <= 60:
        return title
    m = re.match(r"(.{15,70}?[?:])\s", title)
    if m:
        return m.group(1).rstrip(":").strip()
    return " ".join(title.split()[:8])


def load_plan(package_dir: Path) -> tuple[Plan, dict]:
    meta = json.loads((package_dir / "metadata.json").read_text(encoding="utf-8"))
    cover_txt = package_dir / "cover_letter.txt"
    values = {
        "title": meta["title"],
        "running_title": running_title(meta["title"]),
        "abstract": meta["abstract"],
        "keywords": "; ".join(meta["keywords"]),
        "cover_letter": cover_txt.read_text(encoding="utf-8") if cover_txt.exists() else "",
        "article_type": meta.get("article_type") or "original research",
    }
    manuscript = next(package_dir.glob("manuscript.*"), None)
    files = {
        "manuscript_file": str(manuscript) if manuscript else "",
        "cover_letter_file": str(package_dir / "cover_letter.docx") if (package_dir / "cover_letter.docx").exists() else "",
    }
    supplements = sorted(str(p) for p in (package_dir / "supplementary").glob("*") if p.is_file())
    return Plan(values, {k: v for k, v in files.items() if v}, supplements), meta


# Person-detail fields (e.g. an author's "Title: Dr/Prof") must never get manuscript text.
PERSON_FIELD = re.compile(r"author|salutation|prefix|honorific|first[\s_-]*name|last[\s_-]*name|"
                          r"job|position|degree|reviewer|e-?mail|institution|department", re.I)


def match_field(label: str, is_file: bool) -> str | None:
    return match_bits([label], is_file)


def match_bits(bits: list[str], is_file: bool) -> str | None:
    """The nearest label text decides: a field's own label beats text further away on the page."""
    for bit in bits:
        if not is_file and PERSON_FIELD.search(bit):
            return None
        for key, rx in (FILE_RULES if is_file else FIELD_RULES):
            if rx.search(bit):
                return key
    return None


BAD_TYPES = re.compile(r"review|letter|editorial|comment|short|brief|erratum|correction|book|perspective|case", re.I)


def pick_article_type(options: list[str], article_type: str) -> str | None:
    """Choose the drop-down option matching the manuscript's article type, or None if unclear."""
    want_review = bool(re.search(r"review", article_type, re.I))
    choices = [o for o in options if o and not re.match(r"^(please\s+)?(select|choose|--)", o, re.I)]
    if want_review:
        hits = [o for o in choices if re.search(r"review", o, re.I) and not re.search(r"book", o, re.I)]
    else:
        hits = [o for o in choices if re.search(r"original|research", o, re.I) and not BAD_TYPES.search(o)]
        hits = hits or [o for o in choices if re.fullmatch(r"\s*(full[\s-]*length\s+)?article\s*", o, re.I)]
    return hits[0] if len(hits) >= 1 else None


def fill_page(page, plan: Plan, log=print, uploaded: set | None = None, report: list | None = None) -> int:
    """Fill every recognised field on the current page. Never clicks buttons.

    `uploaded` remembers files already uploaded in this session (so automatic filling doesn't
    upload them again); `report` receives one line per field seen, for troubleshooting.
    """
    filled = 0
    used_files: set[str] = uploaded if uploaded is not None else set()
    saw_file_input = False
    supplements_done = False
    idx = 0
    for frame in page.frames:
        try:
            fields = frame.evaluate(COLLECT_JS, idx)
        except Exception:
            continue  # cross-origin or detached frame
        idx += len(fields) + 1
        for f in fields:
            is_file = f["type"] == "file"
            saw_file_input |= is_file
            key = match_bits(f.get("bits") or [f["label"]], is_file)
            action = "not recognised"
            loc = frame.locator(f'[data-jf-idx="{f["idx"]}"]')
            try:
                if not key:
                    pass
                elif is_file and key == "supplementary_file":
                    left = [p for p in plan.supplements if p not in used_files]
                    if left:
                        batch = left if f.get("multiple") else left[:1]
                        loc.set_input_files(batch)
                        used_files.update(batch)
                        supplements_done = True
                        action = "uploaded " + ", ".join(Path(p).name for p in batch)
                    else:
                        action = "supplementary already uploaded"
                elif is_file:
                    path = plan.files.get(key)
                    if path and path not in used_files:
                        loc.set_input_files(path)
                        used_files.add(path)
                        action = f"uploaded {Path(path).name}"
                    else:
                        action = "file already uploaded" if path else "no file for it"
                elif f["value"].strip():
                    action = "already has a value (left unchanged)"
                elif key == "article_type":
                    if f["type"] != "select":
                        action = "article type is not a list (left for you)"
                    else:
                        choice = pick_article_type(f.get("options") or [], plan.values.get("article_type", ""))
                        if choice:
                            loc.select_option(label=choice)
                            action = f"selected '{choice}'"
                        else:
                            action = "article type unclear (left for you)"
                elif f["type"] == "select":
                    action = "list (left for you)"
                else:
                    value = plan.values.get(key)
                    if not value:
                        action = "nothing to put here"
                    elif key == "abstract" and f["type"] not in ("textarea", "richtext"):
                        action = "abstract box is a one-line field (left for you)"
                    elif f["type"] == "richtext":
                        loc.click()
                        frame.page.keyboard.insert_text(value)
                        action = f"filled {key}"
                    else:
                        loc.fill(value)
                        action = f"filled {key}"
                if action.startswith(("filled", "uploaded", "selected")):
                    filled += 1
                    log(f"  {action}  ->  {f['label'][:70]}")
            except Exception as e:
                action = f"failed: {e}"
                log(f"  could not fill {key}: {e}")
            if report is not None:
                report.append(f"  [{action}] {f['type']} | {f['label'][:220]}")
    if saw_file_input and plan.supplements and not supplements_done and \
            not all(p in used_files for p in plan.supplements):
        folder = Path(plan.supplements[0]).parent
        log(f"  Supplementary files were not uploaded: this page has no field labelled 'supplementary'. "
            f"If the site uses one upload box with a 'file type' list, upload them there yourself "
            f"from {folder} and choose 'Supplementary material'.")
    return filled


PAGE_SIGNATURE_JS = """() => {
  let n = 0;
  const walk = (r) => { n += r.querySelectorAll('input, textarea, select, [contenteditable]').length;
                        r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  return location.href.split('#')[0] + ' ' + n;
}"""


def page_signature(page) -> str:
    """Changes when the site shows a different form page (new address or different set of fields)."""
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.evaluate(PAGE_SIGNATURE_JS))
        except Exception:
            continue
    return "|".join(parts)


def write_report(package_dir: Path, url: str, lines: list[str]) -> None:
    import datetime as dt

    with open(package_dir / "form_report.txt", "a", encoding="utf-8") as f:
        f.write(f"\n== {dt.datetime.now():%H:%M:%S} {url}\n" + "\n".join(lines) + "\n")


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
