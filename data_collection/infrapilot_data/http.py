"""Polite HTTP client: robots.txt, per-host rate limits, retries, and conditional GETs.

Responses are cached on disk with their ETag/Last-Modified so re-runs send conditional
requests and unchanged pages cost one 304 instead of a full download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import SETTINGS, USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    content_type: str
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


class PoliteClient:
    def __init__(self, cache_dir: Path | None = None, settings=SETTINGS, use_cache: bool = True):
        self.settings = settings
        self.cache_dir = cache_dir or settings.paths.http_cache
        self.use_cache = use_cache
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=settings.timeout,
            follow_redirects=True,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    # ---- politeness -------------------------------------------------------
    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait_turn(self, host: str) -> None:
        delay = (
            self.settings.fast_host_delay
            if host in self.settings.fast_hosts
            else self.settings.host_delay
        )
        with self._host_lock(host):
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_hit[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.netloc in ("api.github.com", "raw.githubusercontent.com"):
            return True  # API endpoints, governed by API rate limits instead of robots.txt
        base = f"{parts.scheme}://{parts.netloc}"
        with self._host_lock(parts.netloc + "#robots"):
            if base not in self._robots:
                rp = RobotFileParser()
                try:
                    r = self._client.get(f"{base}/robots.txt")
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                    self._robots[base] = rp
                except httpx.HTTPError:
                    self._robots[base] = None  # unreachable robots.txt: allow
            rp = self._robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    # ---- cache ------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.json"

    def _load_cache(self, url: str) -> dict | None:
        p = self._cache_path(url)
        if self.use_cache and p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def _save_cache(self, url: str, resp: httpx.Response) -> None:
        etag, modified = resp.headers.get("etag"), resp.headers.get("last-modified")
        if not (etag or modified):
            return
        self._cache_path(url).write_text(
            json.dumps(
                {
                    "url": url,
                    "etag": etag,
                    "last_modified": modified,
                    "content_type": resp.headers.get("content-type", ""),
                    "text": resp.text,
                }
            )
        )

    # ---- requests ---------------------------------------------------------
    def get(self, url: str, *, headers: dict | None = None, check_robots: bool = True,
            retries: int = 3) -> Response:
        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(url)
        host = urlsplit(url).netloc
        req_headers = dict(headers or {})
        if host == "api.github.com" and self.settings.github_token:
            req_headers.setdefault("Authorization", f"Bearer {self.settings.github_token}")
        cached = self._load_cache(url)
        if cached:
            if cached.get("etag"):
                req_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                req_headers["If-Modified-Since"] = cached["last_modified"]

        for attempt in range(retries + 1):
            self._wait_turn(host)
            try:
                resp = self._client.get(url, headers=req_headers)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise
                log.warning("transport error %s (%s), retrying", url, exc)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 304 and cached:
                return Response(url, 200, cached["text"], cached["content_type"], from_cache=True)
            if resp.status_code in RETRY_STATUS and attempt < retries:
                wait = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                log.warning("HTTP %s for %s, retrying in %.0fs", resp.status_code, url, wait)
                time.sleep(min(wait, 60))
                continue
            if resp.status_code == 200:
                self._save_cache(url, resp)
            return Response(
                str(resp.url), resp.status_code, resp.text, resp.headers.get("content-type", "")
            )
        raise RuntimeError(f"unreachable: {url}")
