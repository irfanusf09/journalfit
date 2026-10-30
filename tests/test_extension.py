"""The Chrome extension on the practice submission site (tests/portal), with the real app.

Chrome loads the unpacked extension; the "author" switches it on for the tab and presses
Next / Add Co-Author / Save & Continue, as they would on a journal's site. Every page must be
filled by the extension and app together, and nothing may be submitted."""

from __future__ import annotations

import functools
import http.server
import re
import threading
import time
from pathlib import Path

import pytest

from journalfit.submit import assistant, remote

from .test_autopilot import make_package

playwright = pytest.importorskip("playwright.sync_api")
PORTAL = Path(__file__).parent / "portal"


def test_generated_js_matches_the_page_reader():
    assert (remote.EXTENSION_DIR / "generated.js").read_text(encoding="utf-8") == remote.generated_js(), \
        "run: python -c 'from journalfit.submit.remote import write_generated_js; write_generated_js()'"


def test_remote_page_records_steps_instead_of_touching_a_browser(tmp_path):
    pkg = make_package(tmp_path)
    s = remote.Session(pkg)
    fields = [
        {"idx": 0, "type": "text", "tag": "input", "bits": ["Full Title"], "label": "Full Title", "value": "",
         "required": True, "visible": True, "options": [], "maxlength": 0, "multiple": False},
        {"idx": 1, "type": "file", "tag": "input", "bits": ["Cover letter"], "label": "Cover letter", "value": "",
         "required": False, "visible": True, "options": [], "maxlength": 0, "multiple": False},
    ]
    out = s.fill("https://journal.example/submit", [{"frameId": 0, "start": 0, "fields": fields, "text": "", "sig": "a"}])
    ops = out["frames"][0]["ops"]
    assert ops[0] == {"op": "fill", "sel": '[data-jf-idx="0"]', "value": "Five Dimensions of Everyday Financial "
                                                                        "Behaviour in Kashmir"}
    assert ops[1]["op"] == "files" and ops[1]["files"][0]["name"] == "cover_letter.docx"
    # Numbered differently from what the app expects: that frame is not filled at all.
    bad = s.fill("https://journal.example/submit", [{"frameId": 0, "start": 5, "fields": fields, "text": "", "sig": "a"}])
    assert bad["frames"] == [] and bad["filled"] == 0
    # A file the browser could not attach is offered again next time.
    s.failed([ops[1]["paths"][0]], ["files: test"])
    again = s.fill("https://journal.example/submit", [{"frameId": 0, "start": 0, "fields": fields, "text": "", "sig": "a"}])
    assert any(op["op"] == "files" for op in again["frames"][0]["ops"])


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve(handler, port=0):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_extension_fills_every_page_and_never_submits(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    from journalfit import app

    monkeypatch.setattr(assistant, "UPLOAD_GAP", 0)
    out_dir = tmp_path / "JournalFit"
    (out_dir / "paper").mkdir(parents=True)
    pkg = make_package(out_dir / "paper")
    monkeypatch.setattr(app, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app.EXTENSION, "sessions", {})
    app_srv = _serve(app.Handler)
    site = _serve(functools.partial(_Quiet, directory=str(PORTAL)))
    ext = str(remote.EXTENSION_DIR)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            sw.evaluate(f"PORTS.splice(0, PORTS.length, {app_srv.server_address[1]}); app = null;")
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{site.server_address[1]}/step1.html")
            page.evaluate("localStorage.clear()")
            page.reload()

            def state():
                return sw.evaluate("(async () => Object.values(await tabs())[0] || {})()")

            def wait_filled(after: float):
                for _ in range(80):
                    st = state()
                    if st.get("filledAt", 0) > after and not st.get("pending"):
                        return st
                    page.wait_for_timeout(250)
                raise AssertionError(f"page not filled: {st}")

            t = time.time() * 1000
            sw.evaluate("""async (pkg) => {
                const [tab] = await chrome.tabs.query({url: 'http://127.0.0.1/*'});
                await saveTab(tab.id, {package: pkg, journal: 'J', log: []});
                fill(tab.id, true);
            }""", str(pkg))
            for step in range(1, 6):
                st = wait_filled(t)
                assert f"step{step}" in page.url, (page.url, st)
                if step == 2:
                    # The extension says who is missing; the author presses Add Co-Author for each.
                    while any("Add author" in line for line in st.get("log", [])):
                        t = time.time() * 1000
                        page.get_by_role("button", name="Add Co-Author").click()
                        st = wait_filled(t)
                t = time.time() * 1000
                page.get_by_role("button", name=re.compile(r"^(Save & Continue|Next)$")).click()
                page.wait_for_url(re.compile(rf"(step{step + 1}|review)\.html"))
            assert "review.html" in page.url
            store = page.evaluate("Object.fromEntries(Object.entries(localStorage))")
        finally:
            ctx.close()
            app_srv.shutdown()
            site.shutdown()
    for flag in ("submitted", "pdf", "cancelled", "deleted", "browse"):
        assert flag not in store, flag
    assert store["v:type"] == "Original Article" and store["v:title"].startswith("Five Dimensions")
    assert store["v:keywords"] == "saving; credit"
    assert (store["v:given1"], store["v:family1"], store["v:email1"]) == ("Asha", "Rao", "asha.rao@uni.example")
    assert (store["v:given2"], store["v:family2"], store["v:country2"]) == ("John", "Smith", "United Kingdom")
    assert (store["v:r1given"], store["v:r1email"]) == ("Maria", "m.garcia@uv.example")
    assert store["v:opgiven"] == ""
    assert (store["v:words"], store["v:prev"], store["v:fund"]) == ("7200", "n", "n")
    assert store["v:coi"].startswith("The authors declare no") and store["v:newsletter"] == ""
    assert store["files"].split("|") == [
        "Manuscript:manuscript.docx", "Title Page:title_page.docx", "Cover Letter:cover_letter.docx",
        "Figure:Figure_1.png", "Figure:Figure_2.png", "Figure:Figure_10.png"]
    score = assistant.automation_score(pkg)
    assert score["percent"] >= 90, score


def test_only_the_extension_can_pair_with_the_app():
    import json
    import urllib.error
    import urllib.request

    from journalfit import app

    srv = _serve(app.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}/api/ext/hello"

    def hello(headers):
        try:
            with urllib.request.urlopen(urllib.request.Request(base, headers=headers)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}
    try:
        assert hello({"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})[0] == 403
        assert hello({"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert hello({"Origin": "https://evil.example", "Sec-Fetch-Site": "none"})[0] == 403
        status, body = hello({"Sec-Fetch-Site": "none"})
        assert status == 200 and body["token"] == app.TOKEN
        # Without the pairing code nothing else answers.
        assert hello({})[0] == 403
    finally:
        srv.shutdown()


def test_extension_on_a_material_style_portal(tmp_path, monkeypatch):
    """Taylor & Francis's portal: a lone unlabelled upload box, radio buttons that each have their
    own name, keyword chips made with Enter, and 'are you an author or submitting for someone'."""
    import json

    from playwright.sync_api import sync_playwright

    from journalfit import app

    out_dir = tmp_path / "JournalFit"
    (out_dir / "paper").mkdir(parents=True)
    pkg = make_package(out_dir / "paper")
    meta = json.loads((pkg / "metadata.json").read_text())
    meta["declarations"]["ethics"] = "Not applicable: the study uses public country-level data."
    (pkg / "metadata.json").write_text(json.dumps(meta))
    monkeypatch.setattr(app, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app.EXTENSION, "sessions", {})
    app_srv = _serve(app.Handler)
    site = _serve(functools.partial(_Quiet, directory=str(PORTAL)))
    ext = str(remote.EXTENSION_DIR)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            sw.evaluate(f"PORTS.splice(0, PORTS.length, {app_srv.server_address[1]}); app = null;")
            page = ctx.new_page()
            base = f"http://127.0.0.1:{site.server_address[1]}"

            def run(name):
                t = time.time() * 1000
                page.goto(f"{base}/{name}")
                sw.evaluate("""async (pkg) => {
                    const [tab] = await chrome.tabs.query({url: 'http://127.0.0.1/*'});
                    await saveTab(tab.id, {package: pkg, journal: 'J', log: []});
                    fill(tab.id, true);
                }""", str(pkg))
                for _ in range(60):
                    st = sw.evaluate("(async () => Object.values(await tabs())[0] || {})()")
                    if st.get("filledAt", 0) > t:
                        return st, page.evaluate("result()")
                    page.wait_for_timeout(250)
                raise AssertionError(st)

            st, res = run("tf_upload.html")
            assert res["upload"] == ["manuscript.docx"], (res, st)
            st, res = run("tf.html")
        finally:
            ctx.close()
            app_srv.shutdown()
            site.shutdown()
    assert res["title"].startswith("Five Dimensions")
    assert res["special"] == "No" and res["human"] == "No"
    assert res["chips"] == ["saving", "credit"]
    assert res["author"] == "I am an author on this manuscript"
    assert res["terms"] is False  # agreeing to the publisher's terms is the author's own act
    assert any("consent to Taylor" in y for y in st["yours"]), st["yours"]
