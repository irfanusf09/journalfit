"""Browser autofill against local forms shaped like real submission systems."""

from __future__ import annotations

import json

import pytest

from journalfit.submit.assistant import fill_page, load_plan, match_field

playwright = pytest.importorskip("playwright.sync_api")

SCHOLARONE_LIKE = """
<html><body><form>
<table>
 <tr><td>Title</td><td><input type="text" name="MANUSCRIPT_TITLE"></td></tr>
 <tr><td>Running Head</td><td><input type="text" name="RUNNING_HEAD"></td></tr>
 <tr><td>Abstract</td><td><textarea name="ABSTRACT"></textarea></td></tr>
 <tr><td>Keywords</td><td><input type="text" name="kw"></td></tr>
 <tr><td>Author Title</td><td><input type="text" name="author_title"></td></tr>
 <tr><td>Already filled title</td><td><input type="text" name="other_title" value="keep me"></td></tr>
</table>
<input type="submit" value="Submit">
</form>
<iframe srcdoc='<label for="f">Upload main manuscript file</label><input id="f" type="file">
  <label for="c">Cover letter</label><input id="c" type="file">'></iframe>
</body></html>
"""


def test_match_field_guards():
    assert match_field("Running Head | RUNNING_HEAD", False) == "running_title"
    assert match_field("Title | MANUSCRIPT_TITLE", False) == "title"
    assert match_field("Author Title | author_title", False) is None
    assert match_field("Cover letter", True) == "cover_letter_file"
    assert match_field("Upload main manuscript file", True) == "manuscript_file"


def test_fill_page(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manuscript.docx").write_bytes(b"x")
    (pkg / "cover_letter.docx").write_bytes(b"y")
    (pkg / "cover_letter.txt").write_text("Dear Editor")
    (pkg / "metadata.json").write_text(json.dumps({
        "journal": "J", "title": "A study of graphene interlayers in lithium sulfur batteries for long life",
        "abstract": "We show things.", "keywords": ["graphene", "Li-S"], "submission_url": "http://x"}))
    plan, _ = load_plan(pkg)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(SCHOLARONE_LIKE)
        page.wait_for_timeout(200)
        n = fill_page(page, plan, log=lambda *_: None)
        vals = {k: page.input_value(f'[name="{k}"]') for k in
                ("MANUSCRIPT_TITLE", "RUNNING_HEAD", "ABSTRACT", "kw", "author_title", "other_title")}
        frame = page.frames[1]
        uploads = frame.evaluate("[...document.querySelectorAll('input[type=file]')].map(e => e.files[0]?.name)")
        browser.close()

    assert vals["MANUSCRIPT_TITLE"].startswith("A study of graphene")
    assert vals["RUNNING_HEAD"] == "A study of graphene interlayers in lithium sulfur"
    assert vals["ABSTRACT"] == "We show things."
    assert vals["kw"] == "graphene; Li-S"
    assert vals["author_title"] == ""  # person-detail field untouched
    assert vals["other_title"] == "keep me"  # never overwrites
    assert uploads == ["manuscript.docx", "cover_letter.docx"]
    assert n == 6
