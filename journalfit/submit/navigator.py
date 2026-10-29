"""Autopilot: move through a submission site's steps the way an author would.

Only two kinds of button are ever pressed:
* "next step" buttons (Next, Continue, Save and continue, Proceed, ...), and
* "add a person" buttons (Add author, Add co-author, Add reviewer) when there are more
  people to enter than the page shows.

Anything that could send, approve, pay, delete, withdraw or agree is never pressed, whatever
else its wording says (see FORBIDDEN). The autopilot stops on the review/approval page, so
the final Submit is always the author's own click.
"""

from __future__ import annotations

import re

# Checked first and always wins: a button whose text, value, title or aria-label matches
# any of these is never clicked.
FORBIDDEN = re.compile(
    r"submit|approv|build\s*(the\s*)?pdf|\bpdf\b|finish|complete|finali[sz]e|\bsend\b|\bpay|payment|checkout|"
    r"purchase|order|delete|remove|withdraw|cancel|discard|reset|clear|log\s*-?\s*(out|off)|sign\s*-?\s*(out|off)|"
    r"\bagree|\baccept|\bconsent|\bsign\b|transfer|decline|reject|\bclose\b|\bexit\b|\bquit\b|unsubscribe|"
    r"upload|browse|attach|choose\s+file|select\s+file|print|download|preview|back|previous|\bprev\b|"
    r"search|find|lookup|look\s+up|verify|validate|confirm|\bok\b|\byes\b|\bno\b|edit|change|start\s+new|"
    r"new\s+submission|create|register|save\s+(as\s+)?draft|save\s+and\s+(close|exit)|help|contact|"
    r"feedback|faq|instructions|copy|duplicate|revise|resubmit|proof",
    re.I)

NEXT = re.compile(
    r"^\s*(save\s*(and|&|\+)\s*)?(continue|next|proceed)(\s+(step|page|to\s+next\s+step))?\s*[>»›→]*\s*$|"
    r"^\s*next\s*[>»›→]+\s*$|^\s*(go\s+to\s+)?next\s+(step|page|section)\s*[>»›→]*\s*$|"
    r"^\s*save\s*(and|&|\+)\s*(go\s+to\s+)?next(\s+(step|page|section))?\s*[>»›→]*\s*$",
    re.I)

ADD_PERSON = {
    "author": re.compile(r"^\s*\+?\s*add\s+(an?\s+|another\s+|new\s+|this\s+)?(co-?\s*)?(contributor|authors?)\s*$|"
                         r"^\s*\+?\s*add\s+to\s+(my\s+)?(co-?\s*)?authors?(\s+list)?\s*$", re.I),
    "reviewer": re.compile(r"^\s*\+?\s*add\s+(an?\s+|another\s+|new\s+)?(suggested\s+|preferred\s+)?reviewers?\s*$",
                           re.I),
}

# Pages where the author must act: the autopilot stops here and says why.
REVIEW_PAGE = re.compile(
    r"build\s+pdf|approve\s+submission|view\s+(the\s+)?(pdf|proof)|review\s*(&|and)\s*submit|"
    r"submission\s+summary|confirm\s+(your\s+)?submission|ready\s+to\s+submit", re.I)
LOGIN_PAGE = re.compile(r"\b(log\s*in|sign\s*in)\b", re.I)
ERROR_SELECTORS = ('[role="alert"]', '.error', '.errors', '.errorMessage', '.error-message', '.alert-danger',
                   '.validation-summary-errors', '.field-validation-error', '.invalid-feedback', '.msgError',
                   '.pageError', '.text-danger')

BUTTONS_JS = r"""
(startIdx) => {
  const out = [];
  let idx = startIdx;
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const all = [];
  const walk = (r) => {
    r.querySelectorAll('button, input[type="submit"], input[type="button"], input[type="image"], a, [role="button"]')
      .forEach(e => all.push(e));
    r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  for (const el of all) {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || !visible(el)) continue;
    const words = [txt(el), el.value || '', el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
                   el.getAttribute('alt') || ''].filter(Boolean);
    // Links that leave the site or open another document are never navigation buttons.
    const href = el.tagName === 'A' ? (el.getAttribute('href') || '') : '';
    el.setAttribute('data-jf-btn', String(idx));
    out.push({idx: idx++, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
              text: txt(el).slice(0, 120), words: words.map(w => w.slice(0, 160)), href: href.slice(0, 300),
              target: el.getAttribute('target') || ''});
  }
  return out;
}
"""


def is_forbidden(b: dict) -> bool:
    return any(FORBIDDEN.search(w) for w in b.get("words") or [b.get("text", "")])


def _label(b: dict) -> str:
    return next((w for w in b.get("words") or [] if w.strip()), "")


def _safe_link(b: dict) -> bool:
    href = b.get("href") or ""
    if b["tag"] != "a":
        return True
    # A same-page script link or form step; never a link to another site, a file or a new window.
    return b.get("target", "") in ("", "_self") and (href in ("", "#") or href.startswith(("#", "javascript:")))


def classify(b: dict) -> str | None:
    """'next', 'add_author', 'add_reviewer' or None. FORBIDDEN always wins."""
    if is_forbidden(b) or not _safe_link(b):
        return None
    label = _label(b)
    if NEXT.search(label):
        return "next"
    for kind, rx in ADD_PERSON.items():
        if rx.search(label):
            return f"add_{kind}"
    return None


def buttons(page) -> list[tuple[object, dict]]:
    """Every visible, enabled button on the page (all frames), with its frame."""
    out, idx = [], 0
    for frame in page.frames:
        try:
            found = frame.evaluate(BUTTONS_JS, idx)
        except Exception:
            continue
        idx += len(found) + 1
        out += [(frame, b) for b in found]
    return out


def find(page, kind: str):
    """The one button of this kind, or None if there is none or it is ambiguous."""
    hits = [(fr, b) for fr, b in buttons(page) if classify(b) == kind]
    if not hits:
        return None
    if len(hits) > 1 and kind == "next":
        # "Next" and "Save & Continue" both shown: the one that saves. Sites often repeat the same
        # button at the top and bottom of the form; two different "next" buttons (e.g. a results
        # list's own Next) are ambiguous, so the author decides.
        saving = [(fr, b) for fr, b in hits if re.search(r"save", _label(b), re.I)]
        hits = saving or hits
        if len({_label(b).strip().lower() for _, b in hits}) > 1 or len(hits) > 2:
            return None
    return hits[0]


def click(frame, b: dict) -> str:
    """Press one allowed button. Re-checks the rules on the live element first."""
    loc = frame.locator(f'[data-jf-btn="{b["idx"]}"]')
    live = loc.evaluate("(el) => [ (el.innerText || el.textContent || ''), el.value || '', "
                        "el.getAttribute('title') || '', el.getAttribute('aria-label') || '', "
                        "el.getAttribute('alt') || '' ]")
    again = dict(b, words=[re.sub(r"\s+", " ", w).strip() for w in live if w and w.strip()])
    if classify(again) != classify(b) or classify(again) is None:
        raise RuntimeError(f"button '{_label(b)}' changed; not pressed")
    loc.click()
    return _label(again)


def page_text(page) -> str:
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.evaluate("document.body ? document.body.innerText : ''")[:30000])
        except Exception:
            continue
    return "\n".join(parts)


def errors_on(page) -> list[str]:
    """Visible error messages the site shows (e.g. after a Next that didn't go through)."""
    js = """(sels) => {
      const out = [];
      for (const s of sels) document.querySelectorAll(s).forEach(e => {
        const r = e.getBoundingClientRect(), t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
        if (r.width > 0 && r.height > 0 && t && t.length < 400) out.push(t);
      });
      return out;
    }"""
    found = []
    for frame in page.frames:
        try:
            found += frame.evaluate(js, list(ERROR_SELECTORS))
        except Exception:
            continue
    return list(dict.fromkeys(found))[:8]


def has_password_box(page) -> bool:
    for frame in page.frames:
        try:
            if frame.evaluate("""() => [...document.querySelectorAll('input[type=password]')]
                                  .some(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })"""):
                return True
        except Exception:
            continue
    return False


def stop_reason(page) -> str | None:
    """Why the autopilot must hand over on this page, or None to carry on."""
    if has_password_box(page):
        return "login"
    labels = " | ".join(_label(b) for _, b in buttons(page))
    if REVIEW_PAGE.search(labels) or REVIEW_PAGE.search(page_text(page)[:4000]):
        if find(page, "next") is None:
            return "review"
    return None


# ------------------------------------------------------------------ the autopilot

class Autopilot:
    """Decides, after each page is filled, whether to press Next / Add author or hand over.

    It hands over (and says exactly why) when: the site wants a login, a required box is
    still empty, the site showed an error after Next, it can't tell which button is Next, or
    the review/approval page is reached. It carries on by itself once the page changes.
    """

    AFTER_CLICK_WAIT = 6.0  # seconds to wait for the site to move on before reading its errors

    def __init__(self, log=print):
        self.log = log
        self.paused_sig = ""  # page the author has to deal with; resume when it changes
        self.clicked: tuple[str, float, str] | None = None  # (page signature, time, button) of the last press
        self.adds: dict[str, int] = {}
        self.steps = 0
        self.max_steps = 60

    def _pause(self, sig: str, why: str) -> str:
        if self.paused_sig != sig:
            self.log("AUTOPILOT PAUSED: " + why)
        self.paused_sig = sig
        return "paused"

    def after_fill(self, page, plan, sig: str) -> str:
        import time

        if sig == self.paused_sig:
            return "paused"
        why = stop_reason(page)
        if why == "login":
            return self._pause(sig, "log in to the site (the tool never types passwords). It carries on after that.")
        if why == "review":
            return self._pause(sig, "this is the review / approval page. Check the PDF and every detail, "
                                    "then click Submit yourself. The tool never submits.")
        if plan.pending_uploads:
            return "uploading"
        st = plan.page_state or {}
        base = sig.split(" ")[0]
        for r in ("author", "reviewer"):
            left = (st.get("missing") or {}).get(r) or []
            if not left:
                continue
            btn = find(page, f"add_{r}")
            if btn and self.adds.get(base + r, 0) <= len(plan.people.get(r) or []) + 1:
                self.adds[base + r] = self.adds.get(base + r, 0) + 1
                label = click(*btn)
                self.log(f"Pressed '{label}' to enter {left[0]}.")
                self.clicked = (sig, time.time(), label)
                return "added"
            if (st.get("person_boxes") or {}).get(r):
                return self._pause(sig, f"add these {r}s yourself (no 'Add {r}' button found): "
                                        + ", ".join(left))
        if st.get("needed"):
            return self._pause(sig, "fill these required boxes, then press Next yourself: "
                                    + "; ".join(st["needed"][:8]))
        btn = find(page, "next")
        if not btn:
            return self._pause(sig, "no clear Next/Continue button on this page; move on yourself.")
        self.steps += 1
        if self.steps > self.max_steps:
            return self._pause(sig, "stopped after many steps; please check where the site is.")
        label = click(*btn)
        self.log(f"Pressed '{label}'.")
        self.clicked = (sig, time.time(), label)
        return "next"

    def check_stuck(self, page, sig: str) -> str | None:
        """Called while the page looks unchanged: did our last press fail?"""
        import time

        if not self.clicked or self.clicked[0] != sig or sig == self.paused_sig:
            return None
        if time.time() - self.clicked[1] < self.AFTER_CLICK_WAIT:
            return None
        label = self.clicked[2]
        self.clicked = None
        errs = errors_on(page)
        return self._pause(sig, f"the site did not move on after '{label}'"
                           + (": " + " / ".join(errs) if errs else ". Check the page for a message."))
