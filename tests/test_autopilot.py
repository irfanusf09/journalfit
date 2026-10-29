"""The autopilot on a multi-step practice submission site (tests/portal): it fills every
step, adds co-authors, answers the Yes/No questions from the author's statements, uploads the
files with their item types, and stops at the review page without pressing Submit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from journalfit.submit import assistant, navigator, people

playwright = pytest.importorskip("playwright.sync_api")

PORTAL = Path(__file__).parent / "portal"
AUTHORS = [
    {"given": "Irfan", "family": "Yousuf", "email": "irfan@uni.example", "institution": "University of Kashmir",
     "country": "India", "corresponding": True},
    {"given": "Asha", "family": "Rao", "email": "asha.rao@uni.example", "institution": "IIT Delhi",
     "country": "India", "orcid": "0000-0002-1825-0097"},
    {"given": "John", "family": "Smith", "email": "j.smith@lse.example", "institution": "LSE", "country": "UK"},
]
REVIEWERS = [{"given": "Maria", "family": "Garcia", "email": "m.garcia@uv.example", "institution": "Univ. of Valencia",
              "reason": "Works on household saving behaviour."}]


def make_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manuscript.docx").write_bytes(b"x")
    (pkg / "cover_letter.docx").write_bytes(b"x")
    (pkg / "cover_letter.txt").write_text("Dear Editor, please consider our paper.")
    (pkg / "title_page.docx").write_bytes(b"x")
    (pkg / "figures").mkdir()
    for n in (2, 1, 10):
        (pkg / "figures" / f"Figure_{n}.png").write_bytes(b"x")
    (pkg / "metadata.json").write_text(json.dumps({
        "journal": "J", "title": "Five Dimensions of Everyday Financial Behaviour in Kashmir",
        "abstract": "We study saving.", "keywords": ["saving", "credit"], "article_type": "original research",
        "word_count": 7200, "figures": 3, "tables": 2,
        "declarations": {
            "competing_interests": "The authors declare no competing interests.",
            "funding": "This research did not receive any specific grant.",
            "data_availability": "Data are available on request.",
            "previous_submission": "This manuscript has not been submitted to this journal before.",
        }}))
    people.save(pkg, "authors.json", AUTHORS)
    people.save(pkg, "reviewers.json", REVIEWERS)
    return pkg


def run_portal(pkg: Path, monkeypatch, max_ticks: int = 120):
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(assistant, "UPLOAD_GAP", 0)
    logs: list[str] = []
    plan, _ = assistant.load_plan(pkg)
    driver = assistant.Driver(pkg, plan, log=logs.append)
    driver.pilot.AFTER_CLICK_WAIT = 1.5
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.goto((PORTAL / "step1.html").as_uri())
        page.evaluate("localStorage.clear()")
        page.reload()
        for _ in range(max_ticks):
            status = driver.tick(page)
            if status == "paused" and "review" in page.url:
                break
            page.wait_for_timeout(250)
        store = page.evaluate("Object.fromEntries(Object.entries(localStorage))")
        final_url = page.url
        b.close()
    return store, final_url, logs


def test_autopilot_reaches_review_page_and_never_submits(tmp_path, monkeypatch):
    pkg = make_package(tmp_path)
    store, url, logs = run_portal(pkg, monkeypatch)
    text = "\n".join(logs)
    assert url.endswith("review.html"), text
    # Never pressed: Submit, Approve, Build PDF, Cancel, Delete, Browse.
    for flag in ("submitted", "pdf", "cancelled", "deleted", "browse"):
        assert flag not in store, (flag, text)
    assert "AUTOPILOT PAUSED: this is the review / approval page" in text
    # Step 1
    assert store["v:type"] == "Original Article"
    assert store["v:title"].startswith("Five Dimensions")
    assert store["v:keywords"] == "saving; credit"
    # Step 2: the corresponding author was already listed; the two co-authors were added.
    assert (store["v:given1"], store["v:family1"], store["v:email1"]) == ("Asha", "Rao", "asha.rao@uni.example")
    assert store["v:orcid1"] == "0000-0002-1825-0097" and store["v:country1"] == "India"
    assert (store["v:given2"], store["v:family2"], store["v:country2"]) == ("John", "Smith", "United Kingdom")
    # Step 3: suggested reviewer filled, opposed reviewer boxes left alone.
    assert (store["v:r1given"], store["v:r1email"]) == ("Maria", "m.garcia@uv.example")
    assert store["v:r1reason"].startswith("Works on")
    assert store["v:opgiven"] == "" and store["v:opemail"] == ""
    # Step 4: numbers, Yes/No questions from the author's statements, statements, no newsletter.
    assert (store["v:words"], store["v:figs"], store["v:tabs"]) == ("7200", "3", "2")
    assert store["v:prev"] == "n" and store["v:fund"] == "n"
    assert store["v:coi"].startswith("The authors declare no")
    assert store["v:newsletter"] == ""
    # Step 5: every file with its own item type, figures in number order.
    assert store["files"].split("|") == [
        "Manuscript:manuscript.docx", "Title Page:title_page.docx", "Cover Letter:cover_letter.docx",
        "Figure:Figure_1.png", "Figure:Figure_2.png", "Figure:Figure_10.png"]
    score = assistant.automation_score(pkg)
    assert score["percent"] is not None and score["percent"] >= 90, score


def test_autopilot_pauses_when_a_required_box_cannot_be_filled(tmp_path, monkeypatch):
    pkg = make_package(tmp_path)
    meta = json.loads((pkg / "metadata.json").read_text())
    meta["declarations"].pop("previous_submission")  # the author hasn't said
    (pkg / "metadata.json").write_text(json.dumps(meta))
    store, url, logs = run_portal(pkg, monkeypatch, max_ticks=80)
    assert url.endswith("step4.html")
    assert any("AUTOPILOT PAUSED: fill these required boxes" in line and "submitted previously" in line
               for line in logs), logs
    assert "submitted" not in store


def test_forbidden_buttons_are_never_next():
    def b(text, **kw):
        return dict({"tag": "button", "type": "", "text": text, "words": [text], "href": "", "target": ""}, **kw)

    for t in ("Submit", "Build PDF for Approval", "Approve Submission", "I agree and continue", "Continue to payment",
              "Submit and Continue", "Finish", "Delete", "Cancel", "Back", "Save and Close", "Upload", ">",
              "Confirm", "Withdraw", "Transfer to another journal", "Next page of results"):
        assert navigator.classify(b(t)) is None, t
    for t in ("Next", "Save & Continue", "Continue", "Proceed", "Next Step", "Save and Next >"):
        assert navigator.classify(b(t)) == "next", t
    assert navigator.classify(b("Next", words=["Next", "Submit manuscript"])) is None  # hidden wording counts
    assert navigator.classify(b("Next", tag="a", href="https://elsewhere.example/")) is None
    assert navigator.classify(b("Add Co-Author")) == "add_author"
    assert navigator.classify(b("Add to My Authors")) == "add_author"
    assert navigator.classify(b("Add Reviewer")) == "add_reviewer"


def test_people_records():
    assert people.orcid_ok("0000-0002-1825-0097") and not people.orcid_ok("0000-0002-1825-0098")
    p = people.normalise({"name": "Dr. Irfan Yousuf", "orcid": "https://orcid.org/0000-0002-1825-0097",
                          "email": "not an email"})
    assert (p["given"], p["family"], p["orcid"], p["email"]) == ("Irfan", "Yousuf", "0000-0002-1825-0097", "")
    assert people.split_name("Yousuf, Irfan") == ("Irfan", "Yousuf")
    assert people.pick_option(["Select", "India", "United States"], "country", "USA") == "United States"
    assert people.pick_option(["Select", "Dr.", "Prof."], "salutation", "Professor") == "Prof."
    assert people.attribute(["Funding institution"]) == "institution"
    assert not people.is_person_field(["Funding institution"])  # funders are not people
    assert people.role(["First Name", "Opposed Reviewers"]) is None


def test_merge_authors_fills_gaps_from_settings_and_address_book():
    found = [{"name": "Irfan Yousuf"}, {"name": "Asha Rao", "email": "asha@x.example"}]
    me = {"name": "Irfan Yousuf", "email": "irfan@x.example", "institution": "Univ"}
    book = {"asha@x.example": people.normalise({"name": "Asha Rao", "orcid": "0000-0002-1825-0097"})}
    out = people.merge_authors(found, me, book)
    assert [people.full_name(p) for p in out] == ["Irfan Yousuf", "Asha Rao"]
    assert out[0]["corresponding"] and out[0]["email"] == "irfan@x.example"
    assert out[1]["orcid"] == "0000-0002-1825-0097"
