"""Timestamped JSON cache and a polite per-host rate limiter for news fetches."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import ROOT

CACHE_DIR = ROOT / "data" / "news_cache"
USER_AGENT = "fantasy-ai/0.1 (personal fantasy football helper; low volume)"
MIN_INTERVAL = 2.0   # seconds between requests to the same host


class NewsCache:
    def __init__(self, directory: Path | str = CACHE_DIR):
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")

    def get(self, key: str, ttl: float) -> object | None:
        p = self._path(key)
        if not p.exists():
            return None
        rec = json.loads(p.read_text())
        if time.time() - rec["fetched_at"] > ttl:
            return None
        return rec["data"]

    def put(self, key: str, data: object) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps({"key": key, "fetched_at": time.time(), "data": data}))


class PoliteClient:
    """GET with cache, per-host spacing, and a real User-Agent. `transport` lets tests mock it."""

    def __init__(self, cache: NewsCache | None = None, transport: httpx.BaseTransport | None = None,
                 min_interval: float = MIN_INTERVAL, sleep=time.sleep):
        self.cache = cache or NewsCache()
        self.transport = transport
        self.min_interval = min_interval
        self.sleep = sleep
        self._last: dict[str, float] = {}

    def get(self, url: str, ttl: float = 1800, params: dict | None = None, as_json: bool = True):
        key = url + "?" + json.dumps(params or {}, sort_keys=True)
        hit = self.cache.get(key, ttl)
        if hit is not None:
            return hit
        host = urlparse(url).netloc
        wait = self.min_interval - (time.time() - self._last.get(host, 0))
        if wait > 0:
            self.sleep(wait)
        with httpx.Client(transport=self.transport, timeout=15, headers={"User-Agent": USER_AGENT},
                          follow_redirects=True) as c:
            r = c.get(url, params=params)
        self._last[host] = time.time()
        r.raise_for_status()
        data = r.json() if as_json else r.text
        self.cache.put(key, data)
        return data
