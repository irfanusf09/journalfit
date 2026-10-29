"""Let the AI read a submission-form page the way a person would.

The AI only decides *which prepared item* belongs in each box (or which listed option to
choose); it never writes text itself, so nothing can be made up. The caller still applies
the hard safety rules (no person fields, no overwriting, no buttons).
"""

from __future__ import annotations

import json
import re

ITEM_HELP = {
    "title": "the full manuscript title",
    "running_title": "a short/running title",
    "abstract": "the abstract",
    "keywords": "the keywords, separated by semicolons",
    "cover_letter": "the cover letter text (only for a box that asks for a cover letter or comments to the editor)",
    "article_type": "the article type (choose the matching option from the list)",
    "competing_interests": "the competing-interests / conflict-of-interest statement",
    "funding_statement": "the funding statement (a text box, not a funder search box)",
    "data_availability": "the data availability statement",
    "ethics": "the ethics approval / consent statement",
    "author_contributions": "the author contributions statement",
    "confirm": "standard confirmations the author has agreed to (tick boxes stating exactly these only)",
}
SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "item": {"type": "string"},
                    "option": {"type": ["string", "null"]},
                },
                "required": ["id", "item", "option"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}
SYSTEM = ("You help an author fill in a journal's online submission form. You only decide where the "
          "author's prepared items go. You never invent information and never answer questions the "
          "items don't settle.")


def make_mapper(llm, log=None):
    """Returns mapper(fields, values) -> {field id: (item, option or None)}; results are cached per page."""
    cache: dict[str, dict] = {}

    def mapper(fields: list[dict], values: dict[str, str]) -> dict[int, tuple[str, str | None]]:
        available = {k: v for k, v in values.items() if v and k in ITEM_HELP}
        if not fields or not available:
            return {}
        listing = []
        for f in fields:
            entry = {"id": f["idx"], "type": f["type"], "label": " | ".join(f.get("bits") or [])[:300]}
            if f.get("options"):
                entry["options"] = f["options"][:40]
            listing.append(entry)
        key = json.dumps(listing, sort_keys=True)
        if key in cache:
            return cache[key]
        items = []
        for k, v in available.items():
            shown = v if k in ("article_type", "competing_interests", "funding_statement", "data_availability",
                               "ethics", "author_contributions", "confirm") else (v[:80] + "..." if len(v) > 80 else v)
            items.append(f"- {k}: {ITEM_HELP[k]}. Value: {shown}")
        prompt = (
            "Items the author has prepared:\n" + "\n".join(items) + "\n\n"
            "Boxes on the current page (id, type, nearby label text, options for lists):\n"
            + json.dumps(listing, ensure_ascii=False, indent=0) + "\n\n"
            "For every box give the item that belongs in it, or \"none\". Rules:\n"
            "- Use \"none\" for anything about people (names, titles like Dr, emails, affiliations, ORCID, "
            "reviewers), search boxes, page navigation, filters, 'results per page', and anything the items "
            "don't cover.\n"
            "- For a list (select), set \"option\" to the exact option text to choose, only if the item's value "
            "clearly settles it (e.g. the article type, or a yes/no question answered by a declaration). "
            "Otherwise use \"none\".\n"
            "- For a radio question, set \"option\" to the exact option text, only if a declaration clearly "
            "answers it (e.g. 'Do you have competing interests?' -> 'No' when the declaration says none).\n"
            "- For a tick box (checkbox), use item \"confirm\" only if its text is one of the standard "
            "confirmations listed; anything else (e.g. opting into services, transfers, newsletters) is \"none\".\n"
            "- For text boxes, \"option\" is null.\n"
            "- Each item normally goes in one box only."
        )
        data = llm.structured(SYSTEM, prompt, SCHEMA, effort="low")
        valid_ids = {f["idx"]: f for f in fields}
        out: dict[int, tuple[str, str | None]] = {}
        for row in data.get("fields") or []:
            fid, item, option = row.get("id"), str(row.get("item") or "none"), row.get("option")
            f = valid_ids.get(fid)
            if f is None or item not in available:
                continue
            if f["type"] in ("select", "radio"):
                match = next((o for o in f.get("options") or []
                              if option and _norm(o) == _norm(option)), None)
                if not match:
                    continue  # the AI may only choose an option that really exists
                out[fid] = (item, match)
            elif f["type"] == "checkbox":
                if item == "confirm":
                    out[fid] = (item, None)
            else:
                out[fid] = (item, None)
        cache[key] = out
        return out

    return mapper


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()
