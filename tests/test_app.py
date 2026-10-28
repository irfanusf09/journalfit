"""Click through the point-and-click app in a real (headless) browser, with the
journal data sources stubbed out."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")

from journalfit import app, config  # noqa: E402

from .test_core import SCIMAGO, FakeHttp, make_docx  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SCIMAGO_PATH", data / "scimago.csv")
    monkeypatch.setattr(config, "SCOPUS_PATH", data / "scopus.xlsx")
    monkeypatch.setattr(config, "OVERRIDES_PATH", data / "none.csv")
    monkeypatch.setattr(app, "UPLOADS", data / "uploads")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path / "out")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("journalfit.http.HttpClient", lambda *a, **k: FakeHttp())
    monkeypatch.setattr("journalfit.metrics.publishers.PageFetcher._browser_get", lambda self, url: None)
    (tmp_path / "out").mkdir()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", tmp_path
    srv.shutdown()


def test_click_through(server):
    from playwright.sync_api import sync_playwright

    url, tmp = server
    csv = tmp / "scimagojr 2025.csv"
    csv.write_text(SCIMAGO + '4;4;Slow Journal;journal;"12345679";1;Q1;50;Springer;X\n')
    paper = tmp / "my paper.docx"
    make_docx(paper)
    alerts = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("dialog", lambda d: (alerts.append(d.message), d.accept()))
        page.goto(url)

        # One-time setup card shows until the journal list is loaded.
        assert page.is_visible("#setupCard")
        page.set_input_files("#scimagoInput", str(csv))
        page.wait_for_selector("#setupCard", state="hidden")
        assert "Journal list loaded" in alerts[-1]
        assert page.is_visible("#keyCard")  # no API key yet
        assert page.is_disabled("#use_ai")

        page.set_input_files("#msInput", str(paper))
        page.wait_for_selector("text=✓ my paper.docx")
        page.fill("#min_acceptance", "")  # Carbon's publisher page lists 25%, others don't publish
        page.click("#findBtn")
        page.wait_for_selector("#resultsCard:not(.hidden)", timeout=20000)
        first = page.inner_text("#results .journal >> nth=0")
        assert "1. Carbon" in first and "28 days" in first
        assert "Batteries" in page.text_content("#excluded")  # open access excluded

        page.click("#results .journal >> nth=0 >> text=Prepare submission for this journal")
        page.wait_for_selector("#pkgBody:not(.hidden)", timeout=20000)
        assert "Submission checklist" in page.inner_text("#checklist")
        assert page.is_visible("#submitCard")
        page.screenshot(path=str(tmp / "app.png"), full_page=True)

        # Settings: the key is stored but never sent back to the page.
        page.click("#settingsBtn")
        page.select_option("#s_provider", "deepseek")
        assert page.inner_text("#keyLabel") == "DeepSeek API key"
        page.fill("#s_key", "sk-deepseek-test")
        page.fill("#s_name", "Dr Test")
        page.click("#saveSettings")
        page.wait_for_selector("#keyCard", state="hidden")
        assert "DeepSeek" in page.inner_text("#aiName")
        state = page.evaluate("fetch('/api/state', {headers: {'X-Token': TOKEN}}).then(r => r.json())")
        assert state["has_api_key"] and state["keys_set"] == ["DEEPSEEK_API_KEY"]
        assert "keys" not in state["settings"] and "sk-deepseek" not in str(state)

        # Switching provider keeps the saved DeepSeek key but needs a key of its own.
        page.click("#settingsBtn")
        page.select_option("#s_provider", "openai")
        assert "not set" in page.inner_text("#keyState")
        page.click("#saveSettings")
        page.wait_for_selector("#keyCard", state="visible")
        browser.close()

    pkgs = list((tmp / "out").glob("*/carbon"))
    assert pkgs and (pkgs[0] / "checklist.md").exists() and (pkgs[0] / "manuscript.docx").exists()


def test_api_requires_token(server):
    import json
    import urllib.error
    import urllib.request

    url, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "api/state")
    assert e.value.code == 403
    req = urllib.request.Request(url + "api/state", headers={"X-Token": app.TOKEN})
    assert "data" in json.loads(urllib.request.urlopen(req).read())
