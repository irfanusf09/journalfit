"""Read the author list from a manuscript's title block.

Most manuscripts write it the same way:

    Irfan Yousf^{1,2*}, Mushtaq Ahmad Rather^{2}, Sabina Akhtar^{3}
    ^{1} Lovely Professional University, Phagwara, Punjab, India
    *Corresponding author: Irfan Yousf, irfan@example.edu

Superscripts come from the Word file itself (marked ^{...}); in plain text, digits and
symbols right after a name are used. Only what is written is taken; nothing is guessed.
The AI is used only when this finds no authors (see llm.extract_authors).
"""

from __future__ import annotations

import re
from pathlib import Path

from .submit import people

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
ORCID = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")
MARK = r"(?:\^\{([^}]*)\}|((?:\d{1,2}|[*†‡§#])(?:\s*,\s*(?:\d{1,2}|[*†‡§#]))*\*?))"
L = r"(?:[^\W\d_]|['’.\-])"  # a letter (any language) or ' . -
NAME = rf"([A-Z]{L}*(?:\s+(?:[A-Z]{L}*|van|von|de|da|del|der|al|el|bin|binti|ul|ud))*)"
AFF_LINE = re.compile(r"^\s*(?:\^\{([^}]+)\}|(\d{1,2}|[a-z]|[*†‡§])[\s.)\]]+)\s*(.+)$")
CORR = re.compile(r"correspond", re.I)
DEPT = re.compile(r"^(department|dept\.?|school|faculty|division|centre|center|institute\s+of|"
                  r"laboratory|lab\b|college\s+of|unit)\b", re.I)
INST = re.compile(r"universit|college|institut|school|academy|hospital|centre|center|council|ministry|"
                  r"department|directorate|bank|laborator|polytechnic|foundation|agency|corporation|ltd|inc\b", re.I)


def docx_front_matter(path: str, stop_at: str = "abstract") -> str:
    """Title block of a Word file with superscripts kept as ^{...}."""
    import docx

    out = []
    for para in docx.Document(path).paragraphs[1:40]:
        if re.match(rf"^\s*{stop_at}\b", para.text, re.I):
            break
        bits = []
        for run in para.runs:
            t = run.text
            if run.font.superscript and t.strip():
                bits.append("^{" + t.strip() + "}")
            else:
                bits.append(t)
        line = re.sub(r"\}\s*\^\{", ",", "".join(bits)).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def _marks(s: str) -> list[str]:
    """'1,2*' -> ['1', '2', '*']; 'a,b' -> ['a', 'b']."""
    return re.findall(r"\d{1,2}|[a-z]|[*†‡§#]", s or "")


def parse(front: str) -> list[dict]:
    """Authors in order: name, affiliation details, email, ORCID, corresponding."""
    lines = [l.strip() for l in front.splitlines() if l.strip()]
    if not lines:
        return []
    affs: dict[str, str] = {}
    author_lines: list[str] = []
    other: list[str] = []
    for line in lines:
        m = AFF_LINE.match(line)
        if m and not CORR.search(line[:30]) and not EMAIL.search(line) and INST.search(line):
            affs[(m.group(1) or m.group(2)).strip()] = m.group(3).strip()
        elif not affs and not EMAIL.search(line) and not CORR.search(line) and len(line) < 400:
            author_lines.append(line)
        else:
            other.append(line)
    found: list[dict] = []
    for line in author_lines:
        for m in re.finditer(NAME + r"\s*" + MARK + r"?", line):
            name, marks = m.group(1).strip(), _marks(m.group(2) or m.group(3) or "")
            if len(name.split()) < 2 or len(name) > 60 or INST.search(name):
                continue
            found.append({"name": name, "marks": marks})
    if not found:
        return []
    single_aff = next(iter(affs.values())) if len(affs) == 1 else ""
    out = []
    rest = "\n".join(other)
    for a in found:
        given, family = people.split_name(a["name"])
        p = {"given": given, "family": family}
        aff = next((affs[k] for k in a["marks"] if k in affs), single_aff)
        p.update(split_affiliation(aff))
        p["corresponding"] = any(k in ("*", "†") for k in a["marks"])
        out.append(p)
    # Emails and ORCIDs: next to the person's name, or the corresponding author's line.
    for line in other:
        emails = [e.group(0).rstrip(".") for e in EMAIL.finditer(line)]
        orcids = ORCID.findall(line)
        for value, key in [(e, "email") for e in emails] + [(o, "orcid") for o in orcids]:
            who = _owner(out, line, value)
            if who is None and CORR.search(line):
                who = next((p for p in out if p["corresponding"]), None)
            if who is not None and not who.get(key):
                who[key] = value
    if not any(p["corresponding"] for p in out) and CORR.search(rest):
        hit = next((p for p in out if p.get("email")), None)
        if hit:
            hit["corresponding"] = True
    return [people.normalise(p) for p in out]


def _owner(authors: list[dict], line: str, value: str) -> dict | None:
    """The author an email/ORCID belongs to: their name next to it, or their name in the email."""
    low = line.lower()
    at = low.find(value.lower())
    best, dist = None, 10 ** 6
    for p in authors:
        for key in (p["family"].lower(), people.full_name(p).lower()):
            pos = low.find(key)
            if key and pos >= 0 and abs(pos - at) < dist:
                best, dist = p, abs(pos - at)
    if best is not None:
        return best
    local = value.split("@")[0].lower()
    hits = [p for p in authors if len(p["family"]) > 2 and p["family"].lower() in local]
    return hits[0] if len(hits) == 1 else None


def split_affiliation(aff: str) -> dict:
    """'Department of X, University Y, Srinagar, Jammu and Kashmir, India' -> parts."""
    if not aff:
        return {}
    parts = [p.strip() for p in re.split(r",|;", aff) if p.strip()]
    out = {"address": aff}
    if len(parts) >= 2:
        out["country"] = parts[-1]
    if parts and DEPT.match(parts[0]) and len(parts) > 1:
        out["department"] = parts[0]
        parts = parts[1:]
    inst = next((p for p in parts if INST.search(p)), parts[0] if parts else "")
    out["institution"] = inst
    tail = parts[parts.index(inst) + 1:-1] if inst in parts and len(parts) >= 2 else []
    if tail:
        out["city"] = tail[0]
        if len(tail) > 1:
            out["state"] = tail[1]
    return out


def from_manuscript(path: str, front_text: str) -> list[dict]:
    front = front_text
    if Path(path).suffix.lower() == ".docx":
        try:
            front = docx_front_matter(path) or front_text
        except Exception:
            pass
    return parse(front)
