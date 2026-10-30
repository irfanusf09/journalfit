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
  <label for="c">Cover letter</label><input id="c" type="file">
  <label for="s1">Supplementary material</label><input id="s1" type="file">
  <label for="s2">Supporting information (multiple)</label><input id="s2" type="file" multiple>'></iframe>
</body></html>
"""


def test_supplement_hint_when_no_supplement_field(tmp_path):
    from journalfit.submit.assistant import Plan

    plan = Plan({}, {"manuscript_file": str(tmp_path / "m.docx")}, [str(tmp_path / "s" / "a.csv")])
    (tmp_path / "m.docx").write_bytes(b"x")
    from playwright.sync_api import sync_playwright

    logs = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content('<label for="f">Upload file</label><input id="f" type="file">')
        fill_page(page, plan, log=logs.append)
        b.close()
    assert any("Supplementary files were not uploaded" in line for line in logs)


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
    (pkg / "supplementary").mkdir()
    for n in ("a.csv", "b.pdf", "c.mp4"):
        (pkg / "supplementary" / n).write_bytes(b"z")
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
        uploads = frame.evaluate("[...document.querySelectorAll('input[type=file]')].map(e => [...e.files].map(f => f.name).join(','))")
        browser.close()

    assert vals["MANUSCRIPT_TITLE"].startswith("A study of graphene")
    assert vals["RUNNING_HEAD"] == "A study of graphene interlayers in lithium sulfur"
    assert vals["ABSTRACT"] == "We show things."
    assert vals["kw"] == "graphene; Li-S"
    assert vals["author_title"] == ""  # person-detail field untouched
    assert vals["other_title"] == "keep me"  # never overwrites
    assert uploads == ["manuscript.docx", "cover_letter.docx", "a.csv", "b.pdf,c.mp4"]
    assert n == 8


MODERN_FORM = """
<html><body>
<div class="step"><h2>Manuscript details</h2>
  <div class="row"><div class="lbl">Article type</div>
    <div><select><option>Please select</option><option>Review Article</option>
      <option>Original Research Article</option><option>Letter</option></select></div></div>
  <div class="row"><div class="lbl">Full title</div><div><div class="wrap"><input type="text"></div></div></div>
  <div class="row"><div class="lbl">Abstract</div>
    <div><div class="toolbar">B I U</div><div class="editor" contenteditable="true"></div></div></div>
  <div class="row"><span>Keywords (separate with semicolons)</span><input type="text"></div>
  <div class="row"><div class="lbl">Corresponding author email</div><input type="text"></div>
  <div class="row"><div class="lbl">Main document</div>
    <div class="dropzone">Drag files here<input type="file" style="display:none"></div></div>
  <x-field></x-field>
</div>
<script>
customElements.define('x-field', class extends HTMLElement {
  constructor() { super(); const r = this.attachShadow({mode: 'open'});
    r.innerHTML = '<label>Running title <input type="text" id="rt"></label>'; }
});
</script>
</body></html>
"""


def _pkg(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manuscript.docx").write_bytes(b"x")
    (pkg / "metadata.json").write_text(json.dumps({
        "journal": "J", "title": "Does connectivity flatten tourism's carbon slope? A tourism-dependence test",
        "abstract": "We study 185 countries.", "keywords": ["tourism", "CO2", "internet"],
        "article_type": "original research"}))
    return pkg


def test_modern_form_is_filled(tmp_path):
    from playwright.sync_api import sync_playwright

    from journalfit.submit.assistant import page_signature

    plan, _ = load_plan(_pkg(tmp_path))
    report = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(MODERN_FORM)
        page.wait_for_timeout(200)
        sig_before = page_signature(page)
        n = fill_page(page, plan, log=lambda *_: None, report=report)
        title = page.input_value(".row:nth-of-type(2) input")
        abstract = page.inner_text(".editor")
        keywords = page.locator("span + input").input_value()
        email = page.locator(".row:nth-of-type(5) input").input_value()
        article = page.eval_on_selector("select", "s => s.options[s.selectedIndex].text")
        running = page.evaluate("document.querySelector('x-field').shadowRoot.querySelector('#rt').value")
        upload = page.evaluate("document.querySelector('input[type=file]').files[0]?.name")
        page.evaluate("document.querySelector('.step').insertAdjacentHTML('beforeend', '<textarea></textarea>')")
        sig_after = page_signature(page)
        browser.close()

    assert title.startswith("Does connectivity flatten")
    assert abstract == "We study 185 countries."
    assert keywords == "tourism; CO2; internet"
    assert email == ""  # person field untouched
    assert article == "Original Research Article"
    assert running == "Does connectivity flatten tourism's carbon slope?"
    assert upload == "manuscript.docx"
    assert n == 6
    assert any("[filled abstract] richtext" in line for line in report)
    assert sig_before != sig_after  # a changed page is noticed for automatic filling


def test_uploaded_files_are_not_repeated_in_auto_mode(tmp_path):
    from playwright.sync_api import sync_playwright

    plan, _ = load_plan(_pkg(tmp_path))
    uploaded = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content('<div>Main document</div><input type="file">')
        assert fill_page(page, plan, log=lambda *_: None, uploaded=uploaded) == 1
        page.set_content('<div>Main document</div><input type="file">')
        assert fill_page(page, plan, log=lambda *_: None, uploaded=uploaded) == 0
        browser.close()


def test_pick_article_type():
    from journalfit.submit.assistant import pick_article_type

    opts = ["-- Select --", "Book Review", "Review Article", "Original Paper", "Short Communication"]
    assert pick_article_type(opts, "original research") == "Original Paper"
    assert pick_article_type(opts, "systematic review") == "Review Article"
    assert pick_article_type(["Select", "Article", "Letter"], "original research") == "Article"
    assert pick_article_type(["Select", "Letter", "Editorial"], "original research") is None


CKE = ("<html><head><style>html{cursor:text}.cke_editable{cursor:text}</style></head>"
       "<body contenteditable=\"true\" class=\"cke_editable\" title=\"Rich Text Editor, {name}\"></body></html>")

EDITORIAL_MANAGER_UPLOAD = """
<html><body><form>
<div>Select Item Type
  <select id="ddSubmissionItemType" name="ddSubmissionItemType">
    <option>Select Item Type</option><option>Cover Letter</option><option>Manuscript</option>
    <option>Manuscript with Tracked Changes</option><option>Figure</option><option>Table</option>
    <option>Supplementary Material</option></select>
  <input type="file" id="fileInput" name="upload_file" style="display:none">
</div>
<div>Description <input type="text" id="meta_Description"></div>
<table><tr><td>File Name:</td><td><input type="file" id="fileInput2"></td></tr>
<tr><td>Delivery Method</td><td>Online Web System</td></tr></table>
</form></body></html>
"""


def test_editorial_manager_rich_text_title_and_abstract(tmp_path):
    from playwright.sync_api import sync_playwright

    plan, _ = load_plan(_pkg(tmp_path))
    page_html = ("<div>Enter Title</div><iframe title=\"Rich Text Editor, fullTitleHtml\" srcdoc='"
                 + CKE.replace("{name}", "fullTitleHtml") + "'></iframe>"
                 "<div>Enter Abstract</div><iframe title=\"Rich Text Editor, abstractHtml\" srcdoc='"
                 + CKE.replace("{name}", "abstractHtml") + "'></iframe>")
    report = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(page_html)
        page.wait_for_timeout(300)
        n = fill_page(page, plan, log=lambda *_: None, report=report)
        title = page.frames[1].inner_text("body")
        abstract = page.frames[2].inner_text("body")
        b.close()
    assert n == 2, report
    assert title.startswith("Does connectivity flatten") and abstract == "We study 185 countries."
    assert not any("cursor" in line for line in report)  # styling code is no longer read as a label


def test_editorial_manager_uploads_one_file_at_a_time_with_item_type(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr("journalfit.submit.assistant.UPLOAD_GAP", 0)

    pkg = _pkg(tmp_path)
    (pkg / "cover_letter.docx").write_bytes(b"c")
    (pkg / "supplementary").mkdir()
    (pkg / "supplementary" / "data.xlsx").write_bytes(b"d")
    plan, _ = load_plan(pkg)
    uploaded = set()
    steps = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        for _ in range(4):  # the site refreshes the page after each upload
            page.set_content(EDITORIAL_MANAGER_UPLOAD)
            fill_page(page, plan, log=lambda *_: None, uploaded=uploaded)
            steps.append((page.eval_on_selector("#ddSubmissionItemType", "s => s.options[s.selectedIndex].text"),
                          page.evaluate("document.querySelector('#fileInput').files[0]?.name || ''"),
                          page.evaluate("document.querySelector('#fileInput2').files[0]?.name || ''")))
        b.close()
    assert steps == [
        ("Manuscript", "manuscript.docx", ""),
        ("Cover Letter", "cover_letter.docx", ""),
        ("Supplementary Material", "data.xlsx", ""),
        ("Select Item Type", "", ""),  # everything uploaded: nothing more is touched
    ]


def test_field_names_written_as_identifiers_are_understood():
    assert match_field("Rich Text Editor, fullTitleHtml", False) == "title"
    assert match_field("Rich Text Editor, abstractHtml", False) == "abstract"
    assert match_field("txt_Keywords", False) == "keywords"


def _run(html, plan, uploaded=None, report=None, logs=None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(html)
        page.wait_for_timeout(200)
        n = fill_page(page, plan, log=(logs.append if logs is not None else (lambda *_: None)),
                      uploaded=uploaded, report=report)
        values = page.evaluate("[...document.querySelectorAll('input:not([type=file]), textarea')].map(e => e.value)")
        files = page.evaluate("[...document.querySelectorAll('input[type=file]')].map(e => [...e.files].map(f => f.name).join(','))")
        selects = page.evaluate("[...document.querySelectorAll('select')].map(s => s.options[s.selectedIndex].text)")
        b.close()
    return n, values, files, selects


def test_next_upload_waits_for_the_previous_one(tmp_path):
    pkg = _pkg(tmp_path)
    (pkg / "cover_letter.docx").write_bytes(b"c")
    plan, _ = load_plan(pkg)
    uploaded = set()
    logs = []
    _run(EDITORIAL_MANAGER_UPLOAD, plan, uploaded)
    _, _, files, _ = _run(EDITORIAL_MANAGER_UPLOAD, plan, uploaded, logs=logs)  # immediately after
    assert files == ["", ""] and plan.pending_uploads
    plan.last_upload = 0
    busy = EDITORIAL_MANAGER_UPLOAD.replace("<form>", "<form><p>Uploading manuscript.docx... please wait</p>")
    _, _, files, _ = _run(busy, plan, uploaded, logs=logs)
    assert files == ["", ""]  # the site still shows an upload in progress


def test_scholarone_rows_are_filled_all_at_once(tmp_path):
    pkg = _pkg(tmp_path)
    (pkg / "cover_letter.docx").write_bytes(b"c")
    (pkg / "supplementary").mkdir()
    (pkg / "supplementary" / "data.xlsx").write_bytes(b"d")
    plan, _ = load_plan(pkg)
    row = ('<tr><td><select><option>Select File Designation</option><option>Main Document</option>'
           '<option>Title Page</option><option>Cover Letter</option><option>Figure</option>'
           '<option>Supplementary Material for Review</option></select></td><td><input type="file"></td></tr>')
    html = "<h3>File Upload</h3><table>" + row * 5 + "</table>"
    _, _, files, selects = _run(html, plan, set())
    assert files == ["manuscript.docx", "cover_letter.docx", "data.xlsx", "", ""]
    assert selects[:3] == ["Main Document", "Cover Letter", "Supplementary Material for Review"]


def test_author_title_box_is_never_given_the_manuscript_title(tmp_path):
    plan, _ = load_plan(_pkg(tmp_path))
    html = ('<h3>Add Author</h3><div class="author-form">'
            '<div><label>First Name</label><input type="text"></div>'
            '<div><span>Title</span><input type="text" name="authorTitle"></div>'
            '<div><span>Title</span><input type="text" name="ctl00$AuthorBlock$txtTitle"></div></div>')
    _, values, _, _ = _run(html, plan, set())
    assert values == ["", "", ""]


def test_text_fits_box_limits(tmp_path):
    from journalfit.submit.assistant import fit

    assert fit("tourism; CO2 emissions; internet; panel data", "keywords", 30) == "tourism; CO2 emissions"
    assert fit("Does connectivity flatten tourism carbon slope", "running_title", 30) == "Does connectivity flatten"
    plan, _ = load_plan(_pkg(tmp_path))
    _, values, _, _ = _run('<div>Short title</div><input type="text" maxlength="25">', plan, set())
    assert len(values[0]) <= 25 and not values[0].endswith(" ")


def test_declarations_go_only_into_multiline_boxes_and_missing_required_fields_are_listed(tmp_path):
    pkg = _pkg(tmp_path)
    meta = json.loads((pkg / "metadata.json").read_text())
    meta["declarations"] = {"competing_interests": "The authors declare no competing interests.",
                            "funding": "No funding was received."}
    (pkg / "metadata.json").write_text(json.dumps(meta))
    plan, _ = load_plan(pkg)
    html = ('<div>Competing Interests Statement *</div><textarea></textarea>'
            '<div>Funding information - search the funder registry</div><input type="text" name="funderSearch">'
            '<div>Funding statement</div><textarea></textarea>'
            '<div>Data availability statement *</div><textarea required></textarea>'
            '<div>Is this a special issue submission? *</div><select required><option>Please select</option>'
            '<option>No</option><option>Yes</option></select>')
    logs, report = [], []
    n, values, _, _ = _run(html, plan, set(), report=report, logs=logs)
    assert values == ["The authors declare no competing interests.", "", "No funding was received.", ""]
    needed = next(l for l in logs if "Still needed from you" in l)
    assert "Data availability statement" in needed
    # A JournalFit submission is a regular one: "special issue?" is answered No, not left for the author.
    assert "special issue" not in needed and any("selected 'No'" in r for r in report)
    assert any("add this declaration in Settings" in r for r in report)


QUESTIONS_PAGE = """
<h3>Additional Information</h3>
<fieldset><legend>Do you have any competing interests to declare? *</legend>
  <label><input type="radio" name="ci" value="y"> Yes</label>
  <label><input type="radio" name="ci" value="n"> No</label></fieldset>
<div class="q"><p>I confirm that this manuscript is original and is not under consideration elsewhere *</p>
  <label><input type="checkbox" id="orig"> I confirm</label></div>
<div class="q"><p>Would you like to receive marketing newsletters?</p>
  <label><input type="checkbox" id="news"> Yes please</label></div>
<div class="q"><p>Is this submission for a special issue? *</p>
  <label><input type="radio" name="si" value="y"> Yes</label><label><input type="radio" name="si" value="n"> No</label></div>
<div class="author"><div>Author title</div><input type="text" id="atitle"></div>
"""


class FakeMapper:
    """Stands in for the AI: claims every box, including ones the hard rules must refuse."""

    def __init__(self):
        self.seen = []

    def __call__(self, fields, values):
        self.seen = fields
        out = {}
        for f in fields:
            label = f["label"].lower()
            if f["type"] == "radio" and "competing" in label:
                out[f["idx"]] = ("competing_interests", "No")
            elif f["type"] == "radio" and "special issue" in label:
                out[f["idx"]] = ("competing_interests", "Maybe")  # not a real option: must be ignored
            elif f["type"] == "checkbox":
                out[f["idx"]] = ("confirm", None) if "original" in label else ("title", None)
            elif f["type"] == "text":
                out[f["idx"]] = ("title", None)  # an author field: the person rule must refuse
        return out


def _questions_plan(tmp_path, confirm=True):
    pkg = _pkg(tmp_path)
    meta = json.loads((pkg / "metadata.json").read_text())
    meta["declarations"] = {"competing_interests": "The authors declare no competing interests.",
                            "confirm_standard": confirm}
    (pkg / "metadata.json").write_text(json.dumps(meta))
    return load_plan(pkg)[0]


def test_ai_answers_questions_and_ticks_only_standard_confirmations(tmp_path):
    from playwright.sync_api import sync_playwright

    plan = _questions_plan(tmp_path)
    mapper, logs = FakeMapper(), []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(QUESTIONS_PAGE)
        fill_page(page, plan, log=logs.append, uploaded=set(), mapper=mapper)
        state = page.evaluate("""() => ({
            ci: [...document.querySelectorAll('input[name=ci]')].map(r => r.checked),
            si: [...document.querySelectorAll('input[name=si]')].map(r => r.checked),
            orig: document.querySelector('#orig').checked, news: document.querySelector('#news').checked,
            atitle: document.querySelector('#atitle').value})""")
        b.close()
    assert state["ci"] == [False, True]          # "No", settled by the declaration
    assert state["si"] == [False, False]         # invented option ignored
    assert state["orig"] is True and state["news"] is False
    assert state["atitle"] == ""                 # person field refused even though the AI claimed it
    radios = [f for f in mapper.seen if f["type"] == "radio"]
    assert [r["options"] for r in radios] == [["Yes", "No"], ["Yes", "No"]]
    assert "competing interests" in radios[0]["label"].lower()
    needed = next(l for l in logs if "Still needed" in l)
    assert "special issue" in needed


def test_confirmations_are_not_ticked_unless_switched_on(tmp_path):
    from playwright.sync_api import sync_playwright

    plan = _questions_plan(tmp_path, confirm=False)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(QUESTIONS_PAGE)
        fill_page(page, plan, log=lambda *_: None, uploaded=set(), mapper=FakeMapper())
        orig = page.evaluate("document.querySelector('#orig').checked")
        b.close()
    assert orig is False


def test_extra_files_get_their_own_item_type(tmp_path, monkeypatch):
    monkeypatch.setattr("journalfit.submit.assistant.UPLOAD_GAP", 0)
    pkg = _pkg(tmp_path)
    (pkg / "supplementary").mkdir()
    for n in ("Figure1.tif", "Table 2.docx", "appendix.pdf"):
        (pkg / "supplementary" / n).write_bytes(b"z")
    plan, _ = load_plan(pkg)
    uploaded, chosen = set(), []
    for _ in range(4):
        _, _, files, selects = _run(EDITORIAL_MANAGER_UPLOAD, plan, uploaded)
        chosen.append((selects[0], files[0]))
    # Extra files go in name order (capitals first), each with its own type.
    assert chosen == [("Manuscript", "manuscript.docx"), ("Figure", "Figure1.tif"),
                      ("Table", "Table 2.docx"), ("Supplementary Material", "appendix.pdf")]
    assert plan.pending_uploads is False


def test_pending_uploads_flag_keeps_the_assistant_coming_back(tmp_path):
    pkg = _pkg(tmp_path)
    (pkg / "cover_letter.docx").write_bytes(b"c")
    plan, _ = load_plan(pkg)
    _run(EDITORIAL_MANAGER_UPLOAD, plan, set())
    assert plan.pending_uploads is True  # the cover letter still has to go up


def test_ai_mapper_validates_its_answers():
    from journalfit.submit.ai_mapper import make_mapper

    class LLM:
        calls = 0

        def structured(self, system, prompt, schema, effort="medium"):
            LLM.calls += 1
            return {"fields": [
                {"id": 1, "item": "abstract", "option": None},
                {"id": 2, "item": "article_type", "option": "research article"},
                {"id": 3, "item": "article_type", "option": "Made-up type"},
                {"id": 4, "item": "nonsense", "option": None},
                {"id": 5, "item": "title", "option": None},   # a tick box may only be "confirm"
                {"id": 99, "item": "title", "option": None},  # no such box
            ]}

    fields = [{"idx": 1, "type": "richtext", "bits": ["Abstract"], "options": []},
              {"idx": 2, "type": "select", "bits": ["Article type"], "options": ["Select", "Research Article"]},
              {"idx": 3, "type": "select", "bits": ["Section"], "options": ["A", "B"]},
              {"idx": 4, "type": "text", "bits": ["x"], "options": []},
              {"idx": 5, "type": "checkbox", "bits": ["Agree"], "options": []}]
    values = {"title": "T", "abstract": "A", "article_type": "original research"}
    mapper = make_mapper(LLM())
    assert mapper(fields, values) == {1: ("abstract", None), 2: ("article_type", "Research Article")}
    mapper(fields, values)
    assert LLM.calls == 1  # same page again: answered from memory, no second AI call


def test_hidden_item_type_list_is_still_used(tmp_path):
    # Editorial Manager hides the real list behind a custom-styled drop-down: the manuscript must
    # still go up as "Manuscript", never through the plain upload box without a type.
    plan, _ = load_plan(_pkg(tmp_path))
    html = EDITORIAL_MANAGER_UPLOAD.replace('<select id="ddSubmissionItemType"',
                                            '<div class="fancy-dropdown">Select Item Type ▾</div>'
                                            '<select style="display:none" id="ddSubmissionItemType"')
    _, _, files, selects = _run(html, plan, set())
    assert selects[0] == "Manuscript" and files == ["manuscript.docx", ""]
