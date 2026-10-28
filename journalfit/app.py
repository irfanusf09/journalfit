"""Point-and-click app: a local web page for finding journals, preparing the
package and filling in the submission site. Runs only on this computer.

    journalfit app               # start it (opens your browser)
    journalfit app --shortcut    # put a double-click "JournalFit" icon on the Desktop
"""

from __future__ import annotations

import json
import os
import platform
import queue
import secrets
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from . import settings as settings_mod
from .config import Criteria
from .llm import PROVIDERS

ROOT = Path(__file__).resolve().parent
REPO_DIR = ROOT.parent
UPLOADS = config.DATA_DIR / "uploads"
OUTPUT_DIR = Path.home() / "Documents" / "JournalFit"
TOKEN = secrets.token_urlsafe(24)


# ------------------------------------------------------------------ settings

def load_settings() -> dict:
    return settings_mod.load()


def apply_settings(s: dict) -> None:
    if s.get("author_email"):
        config.CONTACT_EMAIL = s["author_email"]  # OpenAlex "polite pool"


# ------------------------------------------------------------------ jobs

class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.log: list[str] = []
        self.status = "running"
        self.result: dict | None = None
        self.error = ""
        self.data: dict = {}  # live objects for follow-up steps

    def say(self, msg: str) -> None:
        self.log.append(str(msg))


JOBS: dict[str, Job] = {}


def start_job(fn, *args) -> Job:
    job = Job()
    JOBS[job.id] = job

    def run():
        try:
            job.result = fn(job, *args)
            job.status = "done"
        except Exception as e:  # shown to the user in plain words
            job.error = str(e) or e.__class__.__name__
            job.say("ERROR: " + job.error)
            job.say(traceback.format_exc(limit=3))
            job.status = "error"

    threading.Thread(target=run, daemon=True).start()
    return job


def _llm_or_none(use_ai: bool, job: Job):
    if not use_ai:
        return None
    from .llm import LLMError

    try:
        llm = settings_mod.make()
    except LLMError as e:
        job.say(f"{e}. Continuing without AI.")
        return None
    job.say(f"AI: {llm.name} ({llm.model})")
    return llm


def run_find(job: Job, body: dict) -> dict:
    from . import manuscript
    from .finder import Finder
    from .http import HttpClient
    from .prepare import slug
    from .report import write_report

    path = Path(body["manuscript"])
    c = body.get("criteria", {})
    criteria = Criteria(
        publishers=[p.strip() for p in (c.get("publishers") or "").split(",") if p.strip()],
        exclude_publishers=[p.strip() for p in (c.get("exclude_publishers") or "").split(",") if p.strip()],
        no_oa=bool(c.get("no_oa")), oa_only=bool(c.get("oa_only")), scopus=bool(c.get("scopus")),
        max_first_decision_days=_num(c.get("max_first_decision")),
        max_acceptance_days=_num(c.get("max_acceptance_days")),
        min_acceptance_rate=_num(c.get("min_acceptance")),
        min_quartile=c.get("min_quartile") or None,
        max_apc_usd=_num(c.get("max_apc")),
        if_missing="skip" if c.get("skip_missing") else "keep",
    )
    claude = _llm_or_none(bool(body.get("use_ai")), job)
    job.say(f"Reading {path.name}...")
    ms = manuscript.parse(path)
    if claude:
        job.say("AI is reading the manuscript...")
        try:
            claude.profile_manuscript(ms)
        except Exception as e:  # the parsed title/abstract/keywords are enough to continue
            job.say(f"AI couldn't read the manuscript ({e}); continuing with what was read from the file.")
    if not (ms.title or ms.abstract):
        raise RuntimeError("Could not find a title or abstract in this file.")
    job.say(f"Title: {ms.title}")
    finder = Finder(HttpClient(), llm=claude, log=job.say)
    res = finder.find(ms, criteria, top=int(body.get("top") or 10), web_lookup=claude is not None)
    out = OUTPUT_DIR / (slug(path.stem) or "manuscript")
    report = write_report(res, out)
    job.data.update(result=res, manuscript=ms, claude=claude, out=out)
    job.say(f"Done. Report saved to {report}")
    if claude:
        job.say(claude.cost.summary())
    return {
        "title": ms.title, "criteria": criteria.describe(), "notes": res.notes, "report": str(report),
        "folder": str(out),
        "ranked": [_cand_json(c) for c in res.ranked],
        "excluded": [{"name": c.journal.name, "publisher": c.journal.publisher, "reason": c.excluded_reason}
                     for c in sorted(res.excluded, key=lambda c: -c.text_fit)[:25]],
    }


def _cand_json(c) -> dict:
    from .metrics.publishers import candidate_urls

    j, m = c.journal, c.journal.metrics
    return {
        "name": j.name, "publisher": j.publisher, "issn": ", ".join(j.issns), "homepage": j.homepage,
        "score": c.score, "text_fit": round(c.text_fit, 2), "scope_fit": c.scope_fit,
        "scope_reason": c.scope_reason, "matched": c.matched_works, "refs": c.reference_hits,
        "warnings": c.warnings, "examples": c.example_titles[:4], "metrics": asdict(m),
        "open_access": j.is_oa or j.in_doaj,
        "metrics_page": next(iter(candidate_urls(j)), j.homepage),
        "issn_key": j.issns[0] if j.issns else "",
    }


def run_prepare(job: Job, find_job: Job, index: int, supplements: list[str]) -> dict:
    from .prepare import build_package

    res = find_job.data["result"]
    cand = res.ranked[index]
    ms = find_job.data["manuscript"]
    claude = find_job.data["claude"]
    job.say(f"Preparing the package for {cand.journal.name}...")
    if claude:
        job.say("AI is reading the author guidelines and writing the cover letter (1-3 minutes)...")
        before = claude.cost.usd
    pkg = build_package(ms, cand, claude, settings_mod.author(), find_job.data["out"], supplements=supplements)
    meta = json.loads((pkg / "metadata.json").read_text())
    cover = pkg / "cover_letter.txt"
    job.say("Package ready.")
    if claude:
        job.say(f"Estimated AI cost of this step: ${claude.cost.usd - before:.2f}")
    return {
        "journal": cand.journal.name, "package": str(pkg),
        "checklist": (pkg / "checklist.md").read_text(),
        "cover_letter": cover.read_text() if cover.exists() else "",
        "submission_url": meta.get("submission_url") or "",
        "files": sorted(p.name for p in pkg.iterdir()),
        "supplementary": meta.get("supplementary_files", []),
    }


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ browser assistant
# Playwright objects must stay on one thread, so a single worker owns the browser.

class Assistant:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.log: list[str] = []
        self.active = False
        self.thread: threading.Thread | None = None

    def start(self, package: str, url: str) -> None:
        if self.active:
            self.q.put(("goto", url))
            return
        self.active = True
        self.log = []
        self.thread = threading.Thread(target=self._run, args=(Path(package), url), daemon=True)
        self.thread.start()

    def _run(self, package: Path, url: str) -> None:
        try:
            from playwright.sync_api import sync_playwright

            from .submit.assistant import fill_page, load_plan

            plan, _ = load_plan(package)
            config.ensure_data_dir()
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url)
                self.log.append("Browser opened. Log in, start a new submission, then press 'Fill this page'.")
                while True:
                    cmd, arg = self.q.get()
                    if cmd == "stop":
                        break
                    try:
                        if cmd == "goto":
                            ctx.pages[-1].goto(arg)
                        elif cmd == "fill":
                            n = fill_page(ctx.pages[-1], plan, log=self.log.append)
                            self.log.append(f"{n} field(s) filled on this page." if n else
                                            "Nothing to fill on this page.")
                    except Exception as e:
                        self.log.append(f"Problem: {e}")
                ctx.close()
        except Exception as e:
            self.log.append(f"Could not run the browser: {e}")
        finally:
            self.active = False
            self.log.append("Browser closed. Nothing is submitted unless you clicked Submit yourself.")


ASSISTANT = Assistant()


# ------------------------------------------------------------------ helpers

def data_status() -> dict:
    from .sources.indexing import IndexingData

    idx = IndexingData.load()
    return {"scimago": len(idx.scimago), "scopus": len(idx.scopus)}


def open_path(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def run_update() -> str:
    if not (REPO_DIR / ".git").exists():
        return ("This copy was installed from a ZIP, so it can't update itself. Download the new ZIP from "
                "GitHub and replace the folder (your settings and data are kept).")
    out = subprocess.run(["git", "pull", "--ff-only"], cwd=REPO_DIR, capture_output=True, text=True)
    if out.returncode != 0:
        return "Update failed:\n" + out.stderr
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(REPO_DIR)],
                   capture_output=True, text=True)
    return out.stdout.strip() + "\nRestart JournalFit to use the new version."


def import_upload(kind: str, name: str, data: bytes) -> dict:
    config.ensure_data_dir()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = Path(name).name or "upload"
    if kind == "manuscript":
        if Path(safe).suffix.lower() not in (".docx", ".pdf", ".tex", ".txt", ".md"):
            raise ValueError("Please use a .docx, .pdf, .tex, .txt or .md file.")
        dest = UPLOADS / safe
        dest.write_bytes(data)
        return {"path": str(dest), "name": safe}
    if kind == "supplement":
        folder = UPLOADS / "supplementary"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / safe
        dest.write_bytes(data)
        return {"path": str(dest), "name": safe, "size": len(data)}
    if kind == "remove-supplement":
        (UPLOADS / "supplementary" / safe).unlink(missing_ok=True)
        return {"ok": True}
    if kind == "scimago":
        head = data[:500].decode("utf-8", "replace").lstrip("﻿")
        if not (head.startswith("Rank;") and "Issn" in head):
            raise ValueError("That isn't the SCImago file. Use the .csv from the 'Download data' button.")
        config.SCIMAGO_PATH.write_bytes(data)
        return data_status()
    if kind == "scopus":
        config.SCOPUS_PATH.write_bytes(data)
        return data_status()
    raise ValueError("unknown upload type")


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode())

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        q = parse_qs(u.query)
        if u.path == "/api/download" and secrets.compare_digest(q.get("t", [""])[0], TOKEN):
            p = Path(unquote(q.get("path", [""])[0])).resolve()
            if not (p.is_file() and OUTPUT_DIR.resolve() in p.parents):
                return self._json({"error": "not found"}, 404)
            return self._send(200, p.read_bytes(), "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{p.name}"'})
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        if u.path == "/api/state":
            s = load_settings()
            keys = settings_mod.keys(s)
            provider = s.get("provider")
            return self._json({
                "data": data_status(),
                "settings": {k: v for k, v in s.items() if k != "keys"},
                "keys_set": sorted(keys),  # which keys exist, never the keys themselves
                "providers": [{"id": k, "label": v[0], "model": v[1], "key": v[2]} for k, v in PROVIDERS.items()],
                "has_api_key": PROVIDERS[provider][2] in keys if provider in PROVIDERS else False,
                "output_dir": str(OUTPUT_DIR),
                "can_update": (REPO_DIR / ".git").exists(),
            })
        if u.path.startswith("/api/job/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            return self._json({"status": job.status, "log": job.log, "result": job.result, "error": job.error})
        if u.path == "/api/assist/log":
            return self._json({"active": ASSISTANT.active, "log": ASSISTANT.log})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                kind = parse_qs(u.query).get("kind", [""])[0]
                name = unquote(self.headers.get("X-Filename", "upload"))
                return self._json(import_upload(kind, name, self._body()))
            body = json.loads(self._body() or b"{}")
            if u.path == "/api/settings":
                s = load_settings()
                for name, v in (body.pop("keys", None) or {}).items():
                    if name in settings_mod.KEY_NAMES and v.strip():  # empty box = keep the saved key
                        s["keys"][name] = v.strip()
                if body.get("provider") and body["provider"] not in PROVIDERS:
                    raise ValueError("unknown provider")
                for k in ("provider", "author_name", "author_affiliation", "author_email"):
                    if k in body:
                        s[k] = str(body[k]).strip()
                if "model" in body:
                    s.setdefault("models", {})[s["provider"]] = str(body["model"]).strip()
                settings_mod.save(s)
                apply_settings(s)
                return self._json({"ok": True})
            if u.path == "/api/find":
                return self._json({"job": start_job(run_find, body).id})
            if u.path == "/api/prepare":
                find_job = JOBS.get(body.get("find_job", ""))
                if not find_job or find_job.status != "done":
                    raise ValueError("Run 'Find journals' first.")
                supp_root = (UPLOADS / "supplementary").resolve()
                supplements = [str(Path(p).resolve()) for p in body.get("supplements") or []]
                if any(supp_root not in Path(p).parents or not Path(p).is_file() for p in supplements):
                    raise ValueError("Supplementary files must be added with the upload box.")
                return self._json({"job": start_job(run_prepare, find_job, int(body["index"]), supplements).id})
            if u.path == "/api/open-folder":
                p = Path(body["path"]).resolve()
                if OUTPUT_DIR.resolve() not in [p, *p.parents]:
                    raise ValueError("Can only open JournalFit output folders.")
                open_path(p)
                return self._json({"ok": True})
            if u.path == "/api/assist/start":
                if not body.get("url", "").startswith(("http://", "https://")):
                    raise ValueError("Enter the journal's submission website address (starting with https://).")
                ASSISTANT.start(body["package"], body["url"])
                return self._json({"ok": True})
            if u.path == "/api/assist/fill":
                if not ASSISTANT.active:
                    raise ValueError("Open the submission website first.")
                ASSISTANT.q.put(("fill", None))
                return self._json({"ok": True})
            if u.path == "/api/assist/stop":
                ASSISTANT.q.put(("stop", None))
                return self._json({"ok": True})
            if u.path == "/api/override":
                from .metrics.publishers import save_override

                save_override(body.get("issn", ""), body.get("values") or {}, body.get("note", ""))
                return self._json({"ok": True})
            if u.path == "/api/update":
                return self._json({"message": run_update()})
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)


def make_shortcut() -> Path:
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    py = sys.executable
    if platform.system() == "Windows":
        path = desktop / "JournalFit.bat"
        path.write_text(f'@echo off\r\n"{py}" -m journalfit.app\r\n')
    else:
        path = desktop / ("JournalFit.command" if platform.system() == "Darwin" else "journalfit.sh")
        path.write_text(f'#!/bin/bash\ncd "{REPO_DIR}"\n"{py}" -m journalfit.app\n')
        path.chmod(0o755)
    return path


def serve(port: int = 8765, open_browser: bool = True) -> None:
    apply_settings(load_settings())
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("No free port found.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"JournalFit is running at {url}\nKeep this window open while you use it. Close it to quit.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="journalfit app")
    p.add_argument("--shortcut", action="store_true", help="create a double-click icon on the Desktop")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    if a.shortcut:
        print(f"Created {make_shortcut()}. Double-click it to start JournalFit.")
        return
    serve(a.port, not a.no_browser)


if __name__ == "__main__":
    main()
