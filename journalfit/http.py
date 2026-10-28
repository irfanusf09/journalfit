"""HTTP client with an on-disk cache, retries and per-host rate limiting.

Journal metadata changes slowly, so caching responses (default 7 days) keeps
repeat runs fast and keeps us polite towards the data providers.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

# Identify the tool honestly; sites that don't want automated visitors can say so.
USER_AGENT = "JournalFit/0.1 (journal-selection helper; +https://github.com/irfanusf09/journalfit)"
DEFAULT_TTL = 7 * 24 * 3600


class FetchError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, cache_path=None, ttl: int = DEFAULT_TTL, min_interval: float = 0.15):
        self.ttl = ttl
        self.min_interval = min_interval
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        retry = Retry(
            total=4, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",), respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        self.session.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        self.session.headers["User-Agent"] = USER_AGENT
        if cache_path is None:
            config.ensure_data_dir()
            cache_path = config.CACHE_PATH
        self.db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, status INT, body BLOB)"
        )

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last_hit.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def get_text(self, url: str, params: dict | None = None, ttl: int | None = None,
                 headers: dict | None = None, timeout: float = 30) -> str:
        req = requests.Request("GET", url, params=params).prepare()
        key = hashlib.sha256(req.url.encode()).hexdigest()
        ttl = self.ttl if ttl is None else ttl
        with self._lock:
            row = self.db.execute("SELECT ts, status, body FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[0] < ttl and row[1] == 200:
            return row[2].decode("utf-8", "replace")

        self._throttle(urlparse(req.url).netloc)
        try:
            resp = self.session.get(req.url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            raise FetchError(f"{req.url}: {e}") from e
        if resp.status_code != 200:
            raise FetchError(f"{req.url}: HTTP {resp.status_code}")
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                (key, time.time(), resp.status_code, resp.content),
            )
            self.db.commit()
        return resp.text

    def get_json(self, url: str, params: dict | None = None, ttl: int | None = None):
        import json

        return json.loads(self.get_text(url, params=params, ttl=ttl,
                                        headers={"Accept": "application/json"}))

    def download(self, url: str, dest, timeout: float = 120) -> None:
        resp = self.session.get(url, timeout=timeout, stream=True)
        if resp.status_code != 200:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
