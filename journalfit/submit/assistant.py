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
        if (!sib.matches('input, textarea, select, style, script, link, meta, head, noscript') && txt(sib)) {
          add(txt(sib).slice(0, 160)); break;
        }
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
  const groups = new Map();  // radio buttons are one question per name
  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && (itype === 'checkbox' || itype === 'radio')) {
      if (el.disabled || !visible(el.closest('label') || el.parentElement || el)) continue;
      // A tick box is described by its own label and the nearest sentence before it, never by neighbours.
      const own = el.closest('label') ? txt(el.closest('label')) :
        (el.id && rootOf(el).querySelector && rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`)
          ? txt(rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`)) : (el.getAttribute('aria-label') || ''));
      let near = '', node = el.closest('label') || el;
      for (let depth = 0; depth < 4 && node && !near; depth++) {
        let sib = node.previousElementSibling, hops = 0;
        while (sib && hops < 2 && !near) {
          if (!sib.querySelector('input') && !sib.matches('input, style, script') && txt(sib)) near = txt(sib).slice(0, 200);
          sib = sib.previousElementSibling; hops++;
        }
        node = node.parentElement;
      }
      const bits = [own, near].filter(Boolean).map(t => t.slice(0, 200));
      if (itype === 'checkbox') {
        el.setAttribute('data-jf-idx', String(idx));
        out.push({idx: idx++, type: 'checkbox', tag, bits, label: bits.join(' | '), value: el.checked ? 'checked' : '',
                  required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*|required/i.test(bits.slice(0, 2).join(' ')),
                  visible: true, options: [], maxlength: 0, multiple: false});
      } else {
        const name = el.name || ('anon' + idx);
        let g = groups.get(name);
        if (!g) {
          // The question is the text before the group, not each button's own label.
          const box = el.closest('fieldset, tr, li, .form-group, .question, div') || el.parentElement;
          g = {idx: idx++, type: 'radio', tag, bits: [], label: '', value: '', required: false, visible: true,
               options: [], maxlength: 0, multiple: false, _box: box};
          groups.set(name, g);
          out.push(g);
        }
        el.setAttribute('data-jf-idx', String(g.idx));
        el.setAttribute('data-jf-opt', String(g.options.length));
        g.options.push(bits[0] || el.value || '');
        if (el.checked) g.value = bits[0] || el.value || 'chosen';
        g.required = g.required || !!el.required;
        if (!g.bits.length) {
          let q = [], node = el;
          for (let depth = 0; depth < 6 && node && q.length < 2; depth++) {
            let sib = node.previousElementSibling, hops = 0;
            while (sib && hops < 3) {
              if (!sib.querySelector('input') && txt(sib)) { q.push(txt(sib).slice(0, 200)); break; }
              sib = sib.previousElementSibling; hops++;
            }
            node = node.parentElement;
          }
          const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) q.unshift(txt(fs.querySelector('legend')));
          g.bits = q.length ? q : [el.name || 'question'];
          g.label = g.bits.join(' | ');
          g.required = g.required || /\*|required/i.test(g.label);
        }
      }
      continue;
    }
    const rich = el.isContentEditable && tag !== 'input' && tag !== 'textarea';
    if (rich && el.parentElement && el.parentElement.closest('[contenteditable="true"]')) continue;
    const type = rich ? 'richtext' : tag === 'select' ? 'select' : (el.getAttribute('type') || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    const shown = visible(el);
    // Lists are kept even when hidden behind a custom-styled drop-down (the site reads the real list).
    if (type !== 'file' && type !== 'select' && !shown) continue;
    el.setAttribute('data-jf-idx', String(idx));
    let value = el.value || '';
    if (rich) value = txt(el);
    if (tag === 'select') value = el.selectedIndex > 0 ? txt(el.options[el.selectedIndex]) : '';
    const bits = labelsFor(el);
    const required = !!el.required || el.getAttribute('aria-required') === 'true' ||
      /\*|required/i.test(bits.slice(0, 2).join(' '));
    out.push({idx: idx++, type, tag, bits, label: bits.join(' | '), value, multiple: !!el.multiple,
              maxlength: el.maxLength > 0 ? el.maxLength : 0, required, visible: shown,
              options: tag === 'select' ? [...el.options].map(o => txt(o)) : []});
  }
  out.forEach(f => { delete f._box; });
  return out;
}
"""

# Order matters: more specific labels first.
FIELD_RULES: list[tuple[str, re.Pattern]] = [
    ("abstract_words", re.compile(r"abstract\s+(word\s*count|length\s+in\s+words)|words?\s+in\s+(the\s+)?abstract", re.I)),
    ("word_count", re.compile(r"word[\s_-]*count|number\s+of\s+words|total\s+words|manuscript\s+length\s+in\s+words", re.I)),
    ("figure_count", re.compile(r"(number|no\.?|count)\s+of\s+(figures|illustrations|images)|figures?[\s_-]*count|"
                                r"how\s+many\s+figures", re.I)),
    ("table_count", re.compile(r"(number|no\.?|count)\s+of\s+tables|tables?[\s_-]*count|how\s+many\s+tables", re.I)),
    ("reference_count", re.compile(r"(number|no\.?|count)\s+of\s+references|references?[\s_-]*count", re.I)),
    ("highlights", re.compile(r"\bhighlights\b", re.I)),
    ("grant_number", re.compile(r"grant[\s_-]*(number|no\b|id\b)|award[\s_-]*(number|no\b|id\b)|project[\s_-]*number", re.I)),
    ("funder_name", re.compile(r"funder([\s_-]*name)?\b|funding[\s_-]*(agency|body|organi[sz]ation|source\s+name)|"
                               r"name\s+of\s+(the\s+)?funder|sponsor", re.I)),
    ("ai_disclosure", re.compile(r"artificial\s+intelligence|generative\s+ai|\bgen\s*ai\b|large\s+language\s+model|"
                                 r"\bai[\s-]+(assisted|tools?|use|technolog)", re.I)),
    ("acknowledgements", re.compile(r"acknowledge?ments?", re.I)),
    ("running_title", re.compile(r"running[\s_-]*(head|title)|short[\s_-]*title", re.I)),
    ("title", re.compile(r"\b(full[\s_-]*)?(manuscript|article|paper|submission)?[\s_-]*title\b", re.I)),
    ("abstract", re.compile(r"\babstract\b", re.I)),
    ("keywords", re.compile(r"key[\s_-]*words?|index terms", re.I)),
    ("cover_letter", re.compile(r"cover[\s_-]*letter|comments?\s+to\s+(the\s+)?editor", re.I)),
    ("competing_interests", re.compile(r"competing\s+interest|conflicts?\s+of\s+interest|declaration\s+of\s+interest", re.I)),
    ("funding_statement", re.compile(r"funding\s+(statement|information|sources?)|financial\s+support|role\s+of\s+the\s+funding", re.I)),
    ("data_availability", re.compile(r"data\s+availability|availability\s+of\s+data|data\s+(access|sharing)\s+statement", re.I)),
    ("ethics", re.compile(r"ethic(s|al)\s+(approval|statement|declaration)|informed\s+consent", re.I)),
    ("author_contributions", re.compile(r"author(s'?)?\s+contributions?|credit\s+(author\s+)?statement", re.I)),
    ("article_type", re.compile(r"(article|manuscript|submission|paper)[\s_-]*type|type\s+of\s+(article|manuscript|paper|submission)", re.I)),
    ("preprint", re.compile(r"preprint", re.I)),
    ("previous_submission", re.compile(r"previous(ly)?\s+(submitted|submission)|submitted\s+(this|the)\s+(manuscript|paper)\s+"
                                       r"(before|previously)|prior\s+submission", re.I)),
]
FILE_RULES: list[tuple[str, re.Pattern]] = [
    ("cover_letter_file", re.compile(r"cover[\s_-]*letter", re.I)),
    ("title_page_file", re.compile(r"title[\s_-]*page", re.I)),
    ("highlights_file", re.compile(r"highlights", re.I)),
    ("figure_file", re.compile(r"\bfig(ure)?s?\b", re.I)),
    ("supplementary_file", re.compile(r"supplement|supporting[\s_-]*(info|material|file)|additional[\s_-]*file|"
                                      r"appendi(x|ces)|electronic[\s_-]*supplementary|\bESM\b|\bSI\b", re.I)),
    ("manuscript_file", re.compile(r"manuscript|main[\s_-]*(document|file)|article file|upload|file", re.I)),
]


@dataclass
class Plan:
    values: dict[str, str]
    files: dict[str, str]
    supplements: list[str] = field(default_factory=list)
    last_upload: float = 0.0  # when the last file was handed to the site
    pending_uploads: bool = False  # files still waiting to go up on an item-type page
    people: dict = field(default_factory=dict)  # {"author": [...], "reviewer": [...]} from the author's records
    figures: list[str] = field(default_factory=list)  # separate figure files, when the journal wants them
    page_state: dict = field(default_factory=dict)  # what the last fill_page saw (for the autopilot and score)


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
    decl = meta.get("declarations") or {}
    values.update({
        "competing_interests": decl.get("competing_interests", ""),
        "funding_statement": decl.get("funding", ""),
        "data_availability": decl.get("data_availability", ""),
        "ethics": decl.get("ethics", ""),
        "author_contributions": decl.get("author_contributions", ""),
        # Standard confirmations, only if the author switched them on.
        "confirm": STANDARD_CONFIRMATIONS if decl.get("confirm_standard") else "",
        "preprint": decl.get("preprint", ""),
        "previous_submission": decl.get("previous_submission", ""),
        "ai_disclosure": decl.get("ai_disclosure", ""),
        "acknowledgements": decl.get("acknowledgements", ""),
    })
    # Facts about the manuscript that forms ask for as numbers.
    for k in ("word_count", "figures", "tables", "references", "abstract_words"):
        if meta.get(k) not in (None, ""):
            values[{"figures": "figure_count", "tables": "table_count",
                    "references": "reference_count"}.get(k, k)] = str(meta[k])
    funders = meta.get("funders") or []
    if funders:
        values["funder_name"] = "; ".join(f["name"] for f in funders if f.get("name"))
        values["grant_number"] = "; ".join(f["award"] for f in funders if f.get("award"))
    hl = package_dir / "highlights.txt"
    if hl.exists():
        values["highlights"] = hl.read_text(encoding="utf-8").strip()
    manuscript = next(package_dir.glob("manuscript.*"), None)
    blinded = next(package_dir.glob("anonymised_manuscript.*"), None)
    files = {
        # Double-blind journals get the anonymised copy as "the manuscript" and the title page separately.
        "manuscript_file": str(blinded or manuscript or ""),
        "cover_letter_file": str(package_dir / "cover_letter.docx") if (package_dir / "cover_letter.docx").exists() else "",
        "title_page_file": str(package_dir / "title_page.docx") if (package_dir / "title_page.docx").exists() else "",
        "highlights_file": str(package_dir / "highlights.docx") if (package_dir / "highlights.docx").exists() else "",
    }
    supplements = sorted(str(p) for p in (package_dir / "supplementary").glob("*") if p.is_file())
    figures = sorted((str(p) for p in (package_dir / "figures").glob("*") if p.is_file()), key=_natural)
    from . import people as people_mod

    persons = {"author": people_mod.load(package_dir, "authors.json"),
               # A suggested reviewer goes in only with an email: sites need it, and it is never guessed.
               "reviewer": [p for p in people_mod.load(package_dir, "reviewers.json") if p.get("email")]}
    return Plan(values, {k: v for k, v in files.items() if v}, supplements, people=persons, figures=figures), meta


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(s).name)]


# Person-detail fields (e.g. an author's "Title: Dr/Prof") must never get manuscript text.
PERSON_FIELD = re.compile(r"author(?!s?'?\s*contribution)|salutation|prefix|honorific|first[\s_-]*name|"
                          r"last[\s_-]*name|given[\s_-]*name|family[\s_-]*name|surname|job|position|degree|"
                          r"reviewer|e-?mail|institution|department|affiliation|orcid|phone", re.I)
STANDARD_CONFIRMATIONS = (
    "The work is original and not published elsewhere; it is not under consideration at another "
    "journal; all co-authors have approved the submission; the author agrees to the journal's "
    "policies, terms and ethical guidelines.")
DECLARATIONS = ("competing_interests", "funding_statement", "data_availability", "ethics", "author_contributions",
                "preprint", "ai_disclosure", "acknowledgements", "highlights")


def match_field(label: str, is_file: bool) -> str | None:
    return match_bits([label], is_file)


def words_of(text: str) -> str:
    """'fullTitleHtml' -> 'full Title Html', 'abstract_html' -> 'abstract html' (field names as words)."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_$]+", " ", text)


def match_bits(bits: list[str], is_file: bool) -> str | None:
    """The nearest label text decides: a field's own label beats text further away on the page.

    Any person-detail wording close to the field (its own label, name or the text just before it)
    rules it out, so an author's "Title (Dr/Prof)" box never gets the manuscript title.
    """
    words = list(map(words_of, bits))
    if not is_file and any(PERSON_FIELD.search(w) for w in words[:4]):
        return None
    for bit in words:
        for key, rx in (FILE_RULES if is_file else FIELD_RULES):
            if rx.search(bit):
                return key
    return None


# Yes/No questions settled by the author's own statements: (statement key, question pattern).
QUESTIONS: list[tuple[str, re.Pattern]] = [
    ("previous_submission", re.compile(r"previous(ly)?\s+(submitted|submission|considered|rejected)|submitted\s+"
                                       r"(this|the)\s+(manuscript|paper|article|work)\s+(before|previously)|"
                                       r"prior\s+submission|been\s+submitted\s+(to|before|previously|elsewhere)|"
                                       r"submitted\s+(previously|before|elsewhere)", re.I)),
    ("preprint", re.compile(r"preprint", re.I)),
    ("competing_interests", re.compile(r"competing\s+interest|conflicts?\s+of\s+interest|declaration\s+of\s+interest", re.I)),
    ("funding_statement", re.compile(r"\bfund(ing|ed|er)\b|\bgrants?\b|financial\s+support", re.I)),
    ("ethics", re.compile(r"ethic|human\s+(participants|subjects)|\banimals?\b|informed\s+consent", re.I)),
    ("ai_disclosure", re.compile(r"artificial\s+intelligence|generative\s+ai|\bai\b|large\s+language\s+model|chatgpt", re.I)),
]
NEGATIVE = re.compile(r"\b(no|not|none|nil|never|without|did\s+not|does\s+not|have\s+not|has\s+not|"
                      r"do\s+not|were\s+not|was\s+not)\b|n/a|not\s+applicable", re.I)


def answer_question(bits: list[str], options: list[str], values: dict[str, str]) -> str | None:
    """'Has this manuscript been submitted before?' -> 'No', when the author's statement says so.

    Only for plain Yes/No (or Not applicable) choices, and only when exactly one statement
    matches the question; anything else is left to the AI mapper or the author.
    """
    question = " | ".join(words_of(b) for b in bits[:2])
    keys = [k for k, rx in QUESTIONS if rx.search(question) and values.get(k)]
    if len(keys) != 1:
        return None
    statement = values[keys[0]]
    yes = [o for o in options if re.match(r"^\s*yes\b", o, re.I)]
    no = [o for o in options if re.match(r"^\s*no\b", o, re.I)]
    na = [o for o in options if re.search(r"not\s+applicable|^\s*n/?a\s*$", o, re.I)]
    if len(yes) != 1 or len(no) != 1:
        return None
    if keys[0] == "ethics" and re.search(r"not\s+applicable", statement, re.I) and len(na) == 1:
        return na[0]
    return no[0] if NEGATIVE.search(statement) else yes[0]


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


ITEM_TYPES = [  # (file key, option pattern) in upload order
    ("manuscript_file", re.compile(r"^\s*(main\s+)?(blinded\s+|anonymi[sz]ed\s+)?manuscript(\s+file)?"
                                   r"(\s*\((blinded|anonymi[sz]ed|without\s+author\s+details)\))?\s*$|"
                                   r"^\s*(main\s+)?(article|document)\s*$|^\s*(blinded|anonymi[sz]ed)\s+manuscript\b", re.I)),
    ("title_page_file", re.compile(r"title\s*page", re.I)),
    ("cover_letter_file", re.compile(r"cover\s*letter", re.I)),
    ("highlights_file", re.compile(r"highlight", re.I)),
    ("figure_file", re.compile(r"^\s*figures?\s*$|^\s*figure\b(?!.*caption)", re.I)),
    ("supplementary_file", re.compile(r"supplement|supporting|electronic\s+supplementary", re.I)),
]
# Extra files are typed by their name: Figure1.tif -> "Figure", Table2.docx -> "Table", ...
FILE_KINDS = [
    (re.compile(r"title[\s_-]*page", re.I), re.compile(r"title\s*page", re.I)),
    (re.compile(r"(^|[\s_-])(fig|figure)s?[\s_-]*\d*", re.I), re.compile(r"^\s*figures?\s*$|^\s*figure\b(?!.*caption)", re.I)),
    (re.compile(r"(^|[\s_-])tab(le)?s?[\s_-]*\d*", re.I), re.compile(r"^\s*tables?\s*$", re.I)),
    (re.compile(r"highlight", re.I), re.compile(r"highlight", re.I)),
    (re.compile(r"graphical[\s_-]*abstract", re.I), re.compile(r"graphical\s+abstract", re.I)),
]


def _supplement_pattern(path: str, options: list[str]):
    """Item type for an extra file: by its name when the list has that type, else supplementary material."""
    name = Path(path).stem
    for name_rx, option_rx in FILE_KINDS:
        if name_rx.search(name) and any(option_rx.search(o) for o in options):
            return option_rx
    return dict(ITEM_TYPES)["supplementary_file"]


def _is_item_type_list(f: dict) -> bool:
    opts = " | ".join(f.get("options") or [])
    return f["type"] == "select" and bool(re.search(r"manuscript|main\s+document", opts, re.I)) and \
        bool(re.search(r"cover\s*letter|figure|supplement|title\s+page", opts, re.I))


UPLOAD_BUSY = re.compile(r"uploading|upload in progress|please wait|processing your file", re.I)
UPLOAD_GAP = 6  # seconds between files, so the site finishes one upload before the next starts


def upload_with_item_type(frame, fields: list[dict], plan: Plan, used: set[str]) -> str | None:
    """Sites like Editorial Manager: pick the file's Item Type, then attach that one file.

    Uploads one file per call (these sites refresh after each upload); returns what was done,
    or None if this page isn't that kind of upload page.
    """
    lists = [f for f in fields if _is_item_type_list(f)]
    inputs = [f for f in fields if f["type"] == "file"]
    if not lists or not inputs:
        return None
    pending = []
    options = lists[0].get("options") or []
    for key, pattern in ITEM_TYPES:
        if key == "supplementary_file":
            pending += [(p, _supplement_pattern(p, options)) for p in plan.supplements if p not in used]
        elif key == "figure_file":
            # Separate figures only where the site has a Figure type (otherwise they stay in the manuscript).
            if any(pattern.search(o) for o in options):
                pending += [(p, pattern) for p in plan.figures if p not in used]
        elif key in ("title_page_file", "highlights_file"):
            p = plan.files.get(key)
            if p and p not in used and any(pattern.search(o) for o in options):
                pending.append((p, pattern))
        else:
            p = plan.files.get(key)
            if p and p not in used:
                pending.append((p, pattern))
    plan.pending_uploads = bool(pending)
    if not pending:
        return "all files already uploaded"
    import time

    if time.time() - plan.last_upload < UPLOAD_GAP:
        return "waiting for the previous upload to finish"
    try:
        if UPLOAD_BUSY.search(frame.evaluate("document.body ? document.body.innerText : ''")[:20000]):
            return "waiting for the previous upload to finish"
    except Exception:
        pass

    def choose(lst: dict, pattern) -> str | None:
        choice = next((o for o in lst.get("options") or [] if pattern.search(o)), None)
        if choice:
            frame.locator(f'[data-jf-idx="{lst["idx"]}"]').select_option(label=choice, force=True)
        return choice

    done = []
    if len(lists) >= 2 and len(inputs) >= 2:
        # Several upload rows on one page (ScholarOne): one file per row, all at once.
        for (path, pattern), lst, box in zip(pending, lists, inputs):
            choice = choose(lst, pattern)
            if not choice:
                continue
            frame.locator(f'[data-jf-idx="{box["idx"]}"]').set_input_files(path)
            used.add(path)
            done.append(f"'{choice}': {Path(path).name}")
        if not done:
            return "no matching file types in the list (left for you)"
        plan.last_upload = time.time()
        left = len(pending) - len(done)
        plan.pending_uploads = left > 0
        return ("chose item type and attached " + "; ".join(done)
                + ". Click the site's upload button if it has one"
                + (f"; {left} more file(s) need another row" if left else ""))

    path, pattern = pending[0]
    choice = choose(lists[0], pattern)
    if not choice:
        return f"no item type for {Path(path).name} (left for you)"
    frame.wait_for_timeout(800)  # the site may enable its file box after the type is chosen
    # The file box next to the Item Type list, else the first one.
    target = next((f for f in inputs if any(re.search(r"item\s*type", words_of(b), re.I) for b in f["bits"])), inputs[0])
    frame.locator(f'[data-jf-idx="{target["idx"]}"]').set_input_files(path)
    used.add(path)
    plan.last_upload = time.time()
    plan.pending_uploads = len(pending) > 1
    left = len(pending) - 1
    return (f"chose item type '{choice}' and uploaded {Path(path).name}"
            + (f"; {left} more file(s) will follow after the site finishes this upload" if left else ""))


def fill_page(page, plan: Plan, log=print, uploaded: set | None = None, report: list | None = None,
              mapper=None) -> int:
    """Fill every recognised field on the current page. Never clicks buttons.

    `uploaded` remembers files already uploaded in this session (so automatic filling doesn't
    upload them again); `report` receives one line per field seen, for troubleshooting.
    `mapper` (optional, see ai_mapper) lets the AI decide which prepared item goes in which box;
    the built-in label rules are used where it gives no answer.
    """
    from . import navigator
    from . import people as people_mod

    filled = 0
    used_files: set[str] = uploaded if uploaded is not None else set()
    saw_file_input = False
    supplements_done = False
    still_needed: list[str] = []
    left_for_you = 0  # boxes the author still has to fill on this page
    seen = 0
    placed: dict[str, set[int]] = {"author": set(), "reviewer": set()}
    person_boxes = {"author": 0, "reviewer": 0}
    text = navigator.page_text(page) if any(plan.people.values()) else ""
    idx = 0
    for frame in page.frames:
        try:
            fields = frame.evaluate(COLLECT_JS, idx)
        except Exception:
            continue  # cross-origin or detached frame
        idx += len(fields) + 1
        # Person boxes (authors, suggested reviewers) are filled only from the author's records.
        person_fields = []
        for f in fields:
            bits = f.get("bits") or [f["label"]]
            if f["type"] in ("file", "checkbox", "radio") or not people_mod.is_person_field(bits):
                continue
            f["_person"] = True
            r = people_mod.role(bits)
            if r:
                f["_role"], f["_attr"] = r, people_mod.attribute(bits)
                person_fields.append(f)
                person_boxes[r] += 1
        assigned: dict = {}
        if person_fields and any(plan.people.values()):
            assigned, used = people_mod.assign(person_fields, plan.people, text)
            for r in placed:
                placed[r] |= used.get(r, set())
        try:
            done = upload_with_item_type(frame, fields, plan, used_files)
        except Exception as e:
            done = f"upload failed: {e}"
        if done is not None:
            saw_file_input = supplements_done = True  # this page's files are handled one by one
            if not done.startswith("waiting"):
                log(f"  {done}")
            if done.startswith("chose item type"):
                filled += 1
            if report is not None:
                report.append(f"  [{done}]")
        ai: dict = {}
        if mapper is not None:
            askable = [f for f in fields if f["type"] != "file" and not _is_item_type_list(f)
                       and (f.get("visible", True) or f["type"] == "select")]
            try:
                ai = mapper(askable, plan.values) or {}
            except Exception as e:
                log(f"  AI couldn't read this page ({e}); using the built-in rules")
        for f in fields:
            if done is not None and (f["type"] == "file" or _is_item_type_list(f)):
                continue
            if f["type"] == "select" and not f.get("visible", True) and f["idx"] not in ai:
                continue  # hidden lists are only used for file types or when the AI picks an option
            is_file = f["type"] == "file"
            saw_file_input |= is_file
            key = match_bits(f.get("bits") or [f["label"]], is_file)
            ai_option = None
            if f["idx"] in ai and not is_file:
                ai_key, ai_option = ai[f["idx"]]
                person = any(PERSON_FIELD.search(w) for w in map(words_of, (f.get("bits") or [])[:4]))
                key = None if person else ai_key  # hard rule: person fields are never filled
            if not key and f["type"] in ("radio", "select") and \
                    answer_question(f.get("bits") or [], f.get("options") or [], plan.values):
                key = "question"  # a Yes/No question the author's statements settle
            action = "not recognised"
            loc = frame.locator(f'[data-jf-idx="{f["idx"]}"]')
            seen += 1
            try:
                if f.get("_person"):
                    action = _fill_person(loc, f, assigned.get(f["idx"]))
                elif not key:
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
                elif is_file and key == "figure_file":
                    left = [p for p in plan.figures if p not in used_files]
                    if left:
                        batch = left if f.get("multiple") else left[:1]
                        loc.set_input_files(batch)
                        used_files.update(batch)
                        action = "uploaded " + ", ".join(Path(p).name for p in batch)
                    else:
                        action = "figures already uploaded" if plan.figures else "no separate figure files"
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
                elif f["type"] == "checkbox":
                    if f["value"]:
                        action = "already ticked (left unchanged)"
                    elif key == "confirm" and plan.values.get("confirm"):
                        loc.check(force=True)
                        action = "ticked (standard confirmation)"
                    else:
                        action = "tick box (left for you)"
                elif f["type"] == "radio":
                    if f["value"]:
                        action = "already answered (left unchanged)"
                    elif (ai_option := ai_option or answer_question(f.get("bits") or [], f.get("options") or [],
                                                                    plan.values)) in (f.get("options") or []):
                        k = f["options"].index(ai_option)
                        frame.locator(f'[data-jf-idx="{f["idx"]}"][data-jf-opt="{k}"]').check(force=True)
                        action = f"answered '{ai_option}'"
                    else:
                        action = "question (left for you)"
                elif f["type"] == "select" and (ai_option := ai_option or answer_question(
                        f.get("bits") or [], f.get("options") or [], plan.values)):
                    loc.select_option(label=ai_option, force=True)
                    action = f"selected '{ai_option}'"
                elif key == "article_type":
                    if f["type"] != "select":
                        action = "article type is not a list (left for you)"
                    else:
                        choice = pick_article_type(f.get("options") or [], plan.values.get("article_type", ""))
                        if choice:
                            loc.select_option(label=choice, force=True)
                            action = f"selected '{choice}'"
                        else:
                            action = "article type unclear (left for you)"
                elif f["type"] == "select":
                    action = "list (left for you)"
                else:
                    value = fit(plan.values.get(key) or "", key, f.get("maxlength") or 0)
                    if not value:
                        action = "nothing to put here" if key not in DECLARATIONS else \
                            "add this declaration in Settings to have it filled"
                    elif key in DECLARATIONS and f["type"] not in ("textarea", "richtext"):
                        action = "declaration box is a one-line or search field (left for you)"
                    elif key == "abstract" and f["type"] not in ("textarea", "richtext"):
                        action = "abstract box is a one-line field (left for you)"
                    elif f["type"] == "richtext":
                        loc.click()
                        frame.page.keyboard.insert_text(value)
                        action = f"filled {key}"
                    else:
                        loc.fill(value)
                        action = f"filled {key}"
                if action.startswith(("filled", "uploaded", "selected", "ticked", "answered")):
                    filled += 1
                    log(f"  {action}  ->  {f['label'][:70]}")
            except Exception as e:
                action = f"failed: {e}"
                log(f"  could not fill {key}: {e}")
            done_ok = action.startswith(("filled", "uploaded", "selected", "ticked", "answered"))
            if f.get("required") and not f["value"].strip() and f["type"] != "file" and not done_ok:
                still_needed.append(_short_label(f))
            if not done_ok and not f["value"].strip() and f["type"] != "file" and \
                    (f.get("required") or "(left for you)" in action):
                left_for_you += 1
            if report is not None:
                via = " (AI)" if f["idx"] in ai else ""
                report.append(f"  [{action}{via}] {f['type']} | {f['label'][:220]}")
    if saw_file_input and plan.supplements and not supplements_done and \
            not all(p in used_files for p in plan.supplements):
        folder = Path(plan.supplements[0]).parent
        log(f"  Supplementary files were not uploaded: this page has no field labelled 'supplementary'. "
            f"If the site uses one upload box with a 'file type' list, upload them there yourself "
            f"from {folder} and choose 'Supplementary material'.")
    if still_needed:
        log("  Still needed from you on this page: " + "; ".join(dict.fromkeys(still_needed)))
        if report is not None:
            report.append("  STILL NEEDED: " + "; ".join(dict.fromkeys(still_needed)))
    plan.page_state = {
        "filled": filled, "seen": seen, "left_for_you": left_for_you,
        "needed": list(dict.fromkeys(still_needed)),
        # People the page neither lists nor has boxes for yet: the autopilot may press "Add author".
        "missing": {r: [people_mod.full_name(p) for p in people_mod.missing(plan.people, text, r, placed[r])]
                    for r in ("author", "reviewer")},
        "person_boxes": person_boxes,
    }
    if report is not None:
        report.append(f"  SCORE: filled {filled}, left for you {left_for_you}")
    return filled


def _fill_person(loc, f: dict, hit) -> str:
    """One person box, from the author's records only. Never overwrites what is there."""
    from . import people as people_mod

    if f["value"].strip():
        return "already has a value (left unchanged)"
    if not hit:
        return "person detail (left for you)"
    who, attr = hit
    name = people_mod.full_name(who) or "this person"
    value = people_mod.value_for(who, attr)
    if not value:
        return f"no {attr} in the record of {name} (left for you)"
    if f["type"] == "select":
        opt = people_mod.pick_option(f.get("options") or [], attr, value)
        if not opt:
            return f"'{value}' is not in this list (left for you)"
        loc.select_option(label=opt, force=True)
        return f"selected '{opt}' ({attr} of {name})"
    if f["type"] == "richtext":
        return "person detail in a text editor (left for you)"
    loc.fill(fit(value, attr, f.get("maxlength") or 0))
    return f"filled {attr} of {name}"


def _short_label(f: dict) -> str:
    """A readable name for a field: its first label text that looks like words, not a code name."""
    for b in f.get("bits") or []:
        b = b.strip(" *:")
        if b and " " in b and len(b) < 90:
            return b
    return words_of((f.get("bits") or ["a required field"])[0])[:60]


def fit(value: str, key: str, maxlength: int) -> str:
    """Shorten text to a box's character limit sensibly instead of letting the site cut it."""
    if not maxlength or len(value) <= maxlength:
        return value
    if key == "keywords":
        parts = [p.strip() for p in value.split(";")]
        while parts and len("; ".join(parts)) > maxlength:
            parts.pop()
        return "; ".join(parts)
    cut = value[:maxlength]
    return cut[:cut.rfind(" ")].rstrip(" ,;:-") if " " in cut else cut


PAGE_SIGNATURE_JS = """() => {
  let n = 0, names = '';
  const walk = (r) => { r.querySelectorAll('input, textarea, select, [contenteditable]').forEach(e => {
                          n++; names += (e.name || e.id || '') + ','; });
                        r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  const heads = [...document.querySelectorAll('h1, h2, h3, legend, .step.active, [aria-current="step"]')]
    .map(h => (h.innerText || '').trim()).join('/').slice(0, 300);
  let h = 0;
  for (const c of names + heads) h = (h * 31 + c.charCodeAt(0)) | 0;
  return location.href.split('#')[0] + ' ' + n + ' ' + h;
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


def write_score(package_dir: Path, url: str, state: dict) -> None:
    """automation.json: per page, how many boxes the tool filled and how many it left for the author."""
    path = package_dir / "automation.json"
    try:
        pages = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pages = {}
    key = url.split("#")[0]
    old = pages.get(key, {})
    filled = old.get("filled", 0) + state.get("filled", 0)  # pages are often filled in several passes
    pages[key] = {"filled": filled, "left_for_you": state.get("left_for_you", 0),
                  "needed": state.get("needed", [])}
    path.write_text(json.dumps(pages, indent=2), encoding="utf-8")


def automation_score(package_dir: Path) -> dict:
    """Share of the form work the tool did on this submission so far."""
    try:
        pages = json.loads((package_dir / "automation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"filled": 0, "left_for_you": 0, "percent": None, "pages": 0}
    filled = sum(p.get("filled", 0) for p in pages.values())
    left = sum(p.get("left_for_you", 0) for p in pages.values())
    return {"filled": filled, "left_for_you": left, "pages": len(pages),
            "percent": round(100 * filled / (filled + left)) if filled + left else None}


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
        uploaded: set[str] = set()  # never upload the same file twice in one session
        while True:
            cmd = input("[Enter] autofill current page   [q] quit > ").strip().lower()
            if cmd == "q":
                break
            page = ctx.pages[-1]  # portals sometimes open the form in a new tab
            n = fill_page(page, plan, uploaded=uploaded)
            print(f"  {n} field(s) filled on this page." if n else "  Nothing to fill on this page.")
        ctx.close()
    print("Browser closed. If you did not click Submit, nothing was submitted.")


class Driver:
    """Watches the browser tab: fills each new form page once it has settled and, with the
    autopilot on, moves on to the next step. Used by the app, the command line and the tests."""

    def __init__(self, package_dir: Path, plan: Plan, log=print, mapper=None, autopilot: bool = True):
        from .navigator import Autopilot

        self.package_dir, self.plan, self.log, self.mapper = package_dir, plan, log, mapper
        self.autopilot = autopilot
        self.pilot = Autopilot(log=log)
        self.uploaded: set[str] = set()  # never upload the same file twice in one session
        self.last_sig = self.pending = ""
        self.polls = 0

    def fill(self, page, manual: bool = False) -> int:
        report: list[str] = []
        n = fill_page(page, self.plan, log=self.log, uploaded=self.uploaded, report=report, mapper=self.mapper)
        if n or manual or not self.plan.pending_uploads:
            write_report(self.package_dir, page.url, report)
            write_score(self.package_dir, page.url, self.plan.page_state)
        if n or manual:
            self.log(f"{n} field(s) filled on this page." if n else "Nothing recognised to fill on this page.")
        if manual:
            self.last_sig = "" if self.plan.pending_uploads else page_signature(page)
        return n

    def resume(self) -> None:
        self.pilot.paused_sig = self.last_sig = self.pending = ""
        self.log("Autopilot resumed.")

    def tick(self, page) -> str:
        """One look at the page. Returns what happened ('filled', 'next', 'paused', ...)."""
        sig = page_signature(page)
        if not sig:
            return "no page"
        if sig != self.last_sig:
            # Fill once the new page has stopped changing for one look.
            if sig == self.pending:
                self.polls += 1
            else:
                self.pending, self.polls = sig, 0
            if self.polls < 1:
                return "settling"
            self.fill(page)
            # Files still to upload: come back to this page, don't wait for it to change.
            self.last_sig = "" if self.plan.pending_uploads else page_signature(page)
            if not (self.autopilot and self.last_sig):
                return "filled"
            page.wait_for_timeout(1200)  # let the site register what was typed
            return self.pilot.after_fill(page, self.plan, self.last_sig)
        if self.autopilot:
            return self.pilot.check_stuck(page, sig) or "waiting"
        return "waiting"
