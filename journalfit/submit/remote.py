"""Filling submission forms inside the author's own Chrome, through the JournalFit extension.

Publishers' sign-in pages and some submission systems (Wiley, ScholarOne, Taylor & Francis)
refuse browsers driven by a program. The extension works in the author's normal Chrome
instead: the author signs in and moves through the steps as usual, and on each form page the
extension sends the page's boxes (collected with the same COLLECT_JS as the Playwright
assistant) to this app, which decides what goes where with the same rules (fill_page) and
returns the steps to apply.

RemotePage stands in for a Playwright page: reading returns what the extension collected, and
every fill/select/tick/upload is recorded as an operation for the extension to carry out. The
extension never presses buttons: Next, Add author and Submit stay with the author.
"""

from __future__ import annotations

import base64
import mimetypes
import threading
from pathlib import Path

from .assistant import (COLLECT_JS, PAGE_SIGNATURE_JS, automation_score, fill_page, load_plan, write_report,
                        write_score)

MAX_FILE_BYTES = 40 * 1024 * 1024  # larger files are left for the author to upload


class RemoteLocator:
    def __init__(self, frame: "RemoteFrame", selector: str):
        self.frame, self.selector = frame, selector

    def _add(self, op: str, **kw) -> None:
        self.frame.ops.append({"op": op, "sel": self.selector, **kw})

    def fill(self, value: str) -> None:
        self._add("fill", value=value)

    def select_option(self, label: str, force: bool = False) -> None:
        self._add("select", label=label)

    def check(self, force: bool = False) -> None:
        self._add("check")

    def press(self, key: str) -> None:
        self._add("press", key=key)

    def click(self) -> None:
        self.frame.page.focused = self.frame
        self._add("click")

    def set_input_files(self, paths) -> None:
        paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
        files = []
        for p in map(Path, paths):
            if p.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"{p.name} is too large to attach automatically; upload it yourself")
            files.append({"name": p.name, "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                          "b64": base64.b64encode(p.read_bytes()).decode()})
        self._add("files", files=files, paths=[str(p) for p in paths])


class RemoteKeyboard:
    def __init__(self, page: "RemotePage"):
        self.page = page

    def insert_text(self, value: str) -> None:
        # Typed into the box clicked just before (rich-text editors).
        frame = self.page.focused or self.page.frames[0]
        frame.ops.append({"op": "insert", "value": value})


class RemoteFrame:
    def __init__(self, page: "RemotePage", data: dict):
        self.page = page
        self.frame_id = data.get("frameId", 0)
        self.start = int(data.get("start") or 0)
        self.fields = data.get("fields") or []
        self.text = data.get("text") or ""
        self.sig = data.get("sig") or ""
        self.ops: list[dict] = []

    def evaluate(self, script: str, arg=None):
        if script == COLLECT_JS:
            if arg != self.start:
                raise RuntimeError("field numbering differs from the extension's")  # never fill the wrong box
            return self.fields
        if script == PAGE_SIGNATURE_JS:
            return self.sig
        if "innerText" in script:
            return self.text
        raise RuntimeError("not available through the extension")

    def locator(self, selector: str) -> RemoteLocator:
        return RemoteLocator(self, selector)

    def wait_for_timeout(self, ms: float) -> None:
        self.ops.append({"op": "wait", "ms": ms})


class RemotePage:
    def __init__(self, url: str, frames: list[dict]):
        self.url = url
        self.focused: RemoteFrame | None = None
        self.frames = [RemoteFrame(self, f) for f in frames]
        self.keyboard = RemoteKeyboard(self)


class Session:
    """One submission being filled through the extension (files are uploaded once per session)."""

    def __init__(self, package_dir: Path, mapper=None):
        self.package_dir = package_dir
        self.plan, self.meta = load_plan(package_dir)
        self.mapper = mapper
        self.uploaded: set[str] = set()
        self.lock = threading.Lock()

    def fill(self, url: str, frames: list[dict]) -> dict:
        with self.lock:
            page = RemotePage(url, frames)
            log: list[str] = []
            report: list[str] = []
            n = fill_page(page, self.plan, log=log.append, uploaded=self.uploaded, report=report, mapper=self.mapper)
            write_report(self.package_dir, url, report)
            write_score(self.package_dir, url, self.plan.page_state)
            state = self.plan.page_state or {}
            missing = {r: v for r, v in (state.get("missing") or {}).items() if v}
            if missing.get("author"):
                log.append("Press the site's 'Add author' button for: " + ", ".join(missing["author"])
                           + ". Their details are then filled in.")
            if missing.get("reviewer"):
                log.append("Press the site's 'Add reviewer' button for: " + ", ".join(missing["reviewer"]) + ".")
            log.insert(0, f"{n} box(es) filled on this page." if n else "Nothing to fill on this page.")
            return {"filled": n, "log": log, "pending_uploads": bool(self.plan.pending_uploads),
                    "needed": state.get("needed") or [], "yours": state.get("yours") or [], "score": automation_score(self.package_dir),
                    "frames": [{"frameId": f.frame_id, "ops": f.ops} for f in page.frames if f.ops]}

    def failed(self, paths: list[str], errors: list[str]) -> None:
        """The extension couldn't apply some steps: those files count as not uploaded."""
        with self.lock:
            self.uploaded.difference_update(paths)
            if errors:
                write_report(self.package_dir, "(extension)", [f"  [failed in the browser] {e}" for e in errors])


EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"


def generated_js() -> str:
    """extension/generated.js: the page-reading code shared with the Playwright assistant."""
    return ("// Generated from journalfit/submit/assistant.py by journalfit.submit.remote.write_generated_js().\n"
            "// Do not edit: change COLLECT_JS / PAGE_SIGNATURE_JS there and regenerate.\n"
            f"self.JF_COLLECT = {COLLECT_JS.strip()};\n"
            f"self.JF_SIGNATURE = {PAGE_SIGNATURE_JS.strip()};\n")


def write_generated_js() -> Path:
    path = EXTENSION_DIR / "generated.js"
    path.write_text(generated_js(), encoding="utf-8")
    return path
