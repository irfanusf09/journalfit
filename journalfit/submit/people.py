"""Authors and suggested reviewers on submission forms.

Person boxes are filled only from the author's own records (authors.json / reviewers.json in
the package, which the author sees and can edit in the app). Nothing about a person is ever
guessed: a detail that isn't in the record is left for the author. Manuscript text never goes
into a person box, and person details never go anywhere else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Person detail -> label pattern. Order matters: more specific first.
ATTRS: list[tuple[str, re.Pattern]] = [
    ("orcid", re.compile(r"orcid", re.I)),
    ("email", re.compile(r"e-?\s*mail", re.I)),
    ("given", re.compile(r"first[\s_-]*name|given[\s_-]*name|forename|fore[\s_-]*name|\bfname\b", re.I)),
    ("middle", re.compile(r"middle[\s_-]*(name|initials?)", re.I)),
    ("family", re.compile(r"last[\s_-]*name|family[\s_-]*name|surname|\blname\b", re.I)),
    ("salutation", re.compile(r"salutation|prefix|honorific|^\s*(author\s+)?title\s*[*:]?\s*$|\bacademic\s+title", re.I)),
    ("degree", re.compile(r"\bdegrees?\b|qualifications?", re.I)),
    ("position", re.compile(r"position|job[\s_-]*title|\brole\b", re.I)),
    ("department", re.compile(r"department|\bdept\b|division|faculty|\bschool\b", re.I)),
    ("institution", re.compile(r"institution|organi[sz]ation|affiliation|university|employer|\bcompany\b", re.I)),
    ("city", re.compile(r"\bcity\b|\btown\b", re.I)),
    ("state", re.compile(r"\bstate\b|province|county", re.I)),
    ("postcode", re.compile(r"zip|postal|post[\s_-]*code", re.I)),
    ("country", re.compile(r"country|\bnation\b", re.I)),
    ("address", re.compile(r"address|street", re.I)),
    ("phone", re.compile(r"phone|telephone|mobile|\bfax\b", re.I)),
    ("reason", re.compile(r"reason|why\s+(this|the)\s+reviewer|expertise|justification", re.I)),
    ("name", re.compile(r"^\s*(full\s+)?name\s*[*:]?\s*$|\bfull[\s_-]*name\b", re.I)),
]
PERSON_CONTEXT = re.compile(r"author|reviewer|referee|contributor|corresponding|person|people|"
                            r"first[\s_-]*name|last[\s_-]*name|given[\s_-]*name|family[\s_-]*name|surname|"
                            r"e-?mail|orcid|affiliation|institution", re.I)
REVIEWER = re.compile(r"reviewer|referee", re.I)
OPPOSED = re.compile(r"oppos|exclud|non[\s-]*preferred|not\s+(to\s+)?(be\s+)?(review|contact)|do\s+not|avoid", re.I)
CORRESPONDING = re.compile(r"corresponding", re.I)

COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "united states of america": "united states",
    "uk": "united kingdom", "great britain": "united kingdom", "england": "united kingdom",
    "south korea": "korea, republic of", "korea": "korea, republic of", "republic of korea": "korea, republic of",
    "russia": "russian federation", "iran": "iran, islamic republic of", "vietnam": "viet nam",
    "uae": "united arab emirates", "turkey": "türkiye", "czech republic": "czechia",
    "kashmir": "india", "jammu and kashmir": "india",
}
SALUTATIONS = {"dr": "dr.", "doctor": "dr.", "prof": "prof.", "professor": "prof.", "mr": "mr.", "ms": "ms.",
               "mrs": "mrs.", "miss": "miss", "mx": "mx."}


def words_of(text: str) -> str:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_$]+", " ", text)


def attribute(bits: list[str]) -> str | None:
    """Which person detail a box asks for, from its nearest label texts."""
    for bit in map(words_of, bits[:3]):
        for key, rx in ATTRS:
            if rx.search(bit):
                return key
    return None


def role(bits: list[str]) -> str | None:
    """'author', 'reviewer', or None for boxes the tool must never fill (opposed reviewers)."""
    near = " | ".join(map(words_of, bits[:6]))
    if REVIEWER.search(near):
        return None if OPPOSED.search(near) else "reviewer"
    return "author"


NOT_PERSON = re.compile(r"fund|grant|sponsor|award|ethic|committee|keyword|journal|manuscript\s+title|"
                        r"article\s+title|data\s+(availability|repository)", re.I)


def is_person_field(bits: list[str]) -> bool:
    near = list(map(words_of, bits[:2]))
    if any(NOT_PERSON.search(w) for w in near):
        return False
    return any(PERSON_CONTEXT.search(w) for w in map(words_of, bits[:4])) and attribute(bits) is not None


# ------------------------------------------------------------------ records

FIELDS = ("salutation", "given", "middle", "family", "email", "orcid", "institution", "department", "city",
          "state", "postcode", "country", "address", "phone", "position", "degree")


def split_name(name: str) -> tuple[str, str]:
    """'Irfan Yousuf' -> ('Irfan', 'Yousuf'); 'Yousuf, Irfan' -> ('Irfan', 'Yousuf')."""
    name = re.sub(r"\s+", " ", re.sub(r"^(dr|prof|professor|mr|ms|mrs)\.?\s+", "", name.strip(), flags=re.I))
    if "," in name:
        family, given = [p.strip() for p in name.split(",", 1)]
        return given, family
    parts = name.split(" ")
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", name)


def normalise(p: dict) -> dict:
    out = {k: str(p.get(k) or "").strip() for k in FIELDS}
    if not (out["given"] or out["family"]) and p.get("name"):
        out["given"], out["family"] = split_name(str(p["name"]))
    out["corresponding"] = bool(p.get("corresponding"))
    if p.get("reason"):
        out["reason"] = str(p["reason"]).strip()
    if out["orcid"]:
        m = re.search(r"(\d{4}-?\d{4}-?\d{4}-?\d{3}[\dX])", out["orcid"].upper())
        out["orcid"] = _format_orcid(m.group(1)) if m and orcid_ok(m.group(1)) else ""
    if out["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", out["email"]):
        out["email"] = ""
    return out


def _format_orcid(s: str) -> str:
    d = s.replace("-", "")
    return "-".join(d[i:i + 4] for i in range(0, 16, 4))


def orcid_ok(s: str) -> bool:
    """ISO 7064 11-2 check digit used by every ORCID iD."""
    d = s.replace("-", "").upper()
    if not re.fullmatch(r"\d{15}[\dX]", d):
        return False
    total = 0
    for ch in d[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return d[-1] == ("X" if check == 10 else str(check))


def full_name(p: dict) -> str:
    return " ".join(x for x in (p.get("given"), p.get("middle"), p.get("family")) if x)


def value_for(p: dict, attr: str) -> str:
    if attr == "name":
        return full_name(p)
    return str(p.get(attr) or "")


def load(package_dir: Path, name: str) -> list[dict]:
    try:
        return [normalise(p) for p in json.loads((package_dir / name).read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def save(package_dir: Path, name: str, people: list[dict]) -> None:
    (package_dir / name).write_text(json.dumps([normalise(p) for p in people], indent=2, ensure_ascii=False),
                                    encoding="utf-8")


def merge_authors(found: list[dict], me: dict, book: dict[str, dict]) -> list[dict]:
    """Authors in manuscript order. The corresponding author's own Settings details and the
    address book fill gaps; nothing already known is replaced."""
    out = []
    for p in (normalise(x) for x in found):
        if not (p["given"] or p["family"]):
            continue
        extra = book.get(p["email"].lower()) or book.get(_name_key(p)) or {}
        for k in FIELDS:
            if not p[k] and extra.get(k):
                p[k] = extra[k]
        out.append(p)
    me = normalise(me) if me else None
    if me and (me["given"] or me["family"]):
        hit = next((p for p in out if (me["email"] and p["email"].lower() == me["email"].lower())
                    or _name_key(p) == _name_key(me)), None)
        if hit:
            for k in FIELDS:
                if not hit[k] and me[k]:
                    hit[k] = me[k]
        else:
            out.insert(0, me)
            hit = me
        if not any(p["corresponding"] for p in out):
            hit["corresponding"] = True
    return out


def _name_key(p: dict) -> str:
    return re.sub(r"[^a-z]", "", (p.get("given", "")[:1] + p.get("family", "")).lower())


def address_book(path: Path) -> dict[str, dict]:
    """~/.journalfit/people.json: co-authors from earlier submissions, by email and by name."""
    try:
        people = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for p in map(normalise, people if isinstance(people, list) else []):
        if p["email"]:
            out[p["email"].lower()] = p
        out[_name_key(p)] = p
    return out


def remember(path: Path, people: list[dict]) -> None:
    """Add or update co-authors in the address book (the author's own records)."""
    try:
        book = [normalise(p) for p in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        book = []
    for p in map(normalise, people):
        if not (p["given"] or p["family"]):
            continue
        hit = next((b for b in book if (p["email"] and b["email"].lower() == p["email"].lower())
                    or _name_key(b) == _name_key(p)), None)
        if hit:
            hit.update({k: v for k, v in p.items() if v and k in FIELDS})
        else:
            book.append({k: v for k, v in p.items() if k != "corresponding"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ choosing options

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def pick_option(options: list[str], attr: str, value: str) -> str | None:
    """The list option that means the same as the value (country names, Dr/Prof), or None."""
    if not value:
        return None
    want = _norm(value)
    if attr == "country":
        want = _norm(COUNTRY_ALIASES.get(want, want))
    if attr == "salutation":
        want = _norm(SALUTATIONS.get(want.rstrip("."), value))
    exact = [o for o in options if _norm(o) == want]
    if exact:
        return exact[0]
    if attr == "country":
        alt = [o for o in options if _norm(COUNTRY_ALIASES.get(_norm(o), _norm(o))) == want]
        if len(alt) == 1:
            return alt[0]
        starts = [o for o in options if _norm(o).startswith(want + " ") or want.startswith(_norm(o) + " ")]
        return starts[0] if len(starts) == 1 else None
    return None


# ------------------------------------------------------------------ assigning boxes to people

def present_on_page(p: dict, text: str) -> bool:
    """The person is already listed on the page (e.g. in the site's author table)."""
    low = text.lower()
    if p.get("email") and p["email"].lower() in low:
        return True
    name = full_name(p).lower()
    alt = f"{p.get('family', '')}, {p.get('given', '')}".lower()
    return bool(p.get("family")) and (name in low or alt in low)


def blocks(fields: list[dict]) -> list[list[dict]]:
    """Group person boxes into one block per person: a detail seen again starts a new person."""
    out: list[list[dict]] = []
    seen: set[tuple[str, str]] = set()
    current: list[dict] = []
    for f in fields:
        key = (f["_role"], f["_attr"])
        if key in seen or (current and current[-1]["_role"] != f["_role"]):
            out.append(current)
            current, seen = [], set()
        current.append(f)
        seen.add(key)
    if current:
        out.append(current)
    return out


def assign(fields: list[dict], people: dict[str, list[dict]], page_text: str
           ) -> tuple[dict[int, tuple[dict, str]], dict[str, set[int]]]:
    """({field idx: (person, detail)}, {role: indexes of the people placed on this page}).

    A block that already has details typed in belongs to the person those details name (and
    only its empty boxes are completed). Empty blocks get the next people not yet on the page.
    """
    out: dict[int, tuple[dict, str]] = {}
    used: dict[str, set[int]] = {"author": set(), "reviewer": set()}
    groups = blocks(fields)
    # First the blocks that already name someone.
    pending = []
    for g in groups:
        r = g[0]["_role"]
        typed = {f["_attr"]: f["value"].strip() for f in g if f["value"].strip() and f["type"] != "select"}
        if typed:
            who = next((i for i, p in enumerate(people.get(r) or [])
                        if (typed.get("email") and typed["email"].lower() == p["email"].lower())
                        or (typed.get("family") and typed["family"].lower() == p["family"].lower()
                            and (not typed.get("given") or typed["given"][:1].lower() == p["given"][:1].lower()))),
                       None)
            if who is not None:
                used[r].add(who)
                out.update({f["idx"]: (people[r][who], f["_attr"]) for f in g})
            continue  # someone we have no record of: leave their block alone
        pending.append(g)
    for g in pending:
        r = g[0]["_role"]
        who = next((i for i, p in enumerate(people.get(r) or [])
                    if i not in used[r] and not present_on_page(p, page_text)), None)
        if who is None:
            continue
        used[r].add(who)
        out.update({f["idx"]: (people[r][who], f["_attr"]) for f in g})
    return out, used


def missing(people: dict[str, list[dict]], page_text: str, r: str, placed: set[int] = frozenset()) -> list[dict]:
    """This role's people that the page neither lists nor has boxes filled in for."""
    return [p for i, p in enumerate(people.get(r) or []) if i not in placed and not present_on_page(p, page_text)]
