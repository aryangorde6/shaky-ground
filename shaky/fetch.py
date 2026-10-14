"""HTTP for the data APIs: a disk cache, a polite rate limit, and retries with backoff.

API keys are added only when a request is sent. They never reach the cache files,
the cache keys, or any error message.
"""
import hashlib
import json
import logging
import random
import re
import time

import httpx

from . import config

CACHE_DIR = config.DATA_DIR / "cache"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6

# httpx logs full URLs (keys included) at INFO level; keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Source:
    """One API. `secret_params` (e.g. the API key) are sent but never cached or printed."""

    def __init__(self, name: str, base_url: str, min_interval: float,
                 secret_params: dict | None = None, headers: dict | None = None):
        self.name = name
        self.min_interval = min_interval
        self.secret_params = {k: v for k, v in (secret_params or {}).items() if v}
        self.client = httpx.Client(base_url=base_url, headers=headers or {},
                                   timeout=60, follow_redirects=True)
        self.fetched = 0   # requests sent to the API
        self.cached = 0    # answered from disk
        self._last = 0.0

    def get(self, path: str, params: dict | None = None,
            refresh: bool = False, cache: bool = True) -> dict:
        params = dict(params or {})
        file = self._cache_file(path, params)
        if cache and not refresh and file.exists():
            self.cached += 1
            return json.loads(file.read_text())
        data = self._request(path, params)
        self.fetched += 1
        if cache:
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(file)   # never leaves a half-written cache file
        return data

    def _cache_file(self, path: str, params: dict):
        digest = hashlib.sha1(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:60]
        return CACHE_DIR / self.name / f"{slug}-{digest[:12]}.json"

    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _request(self, path: str, params: dict) -> dict:
        problem = None
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            retry_after = None
            try:
                r = self.client.get(path, params={**params, **self.secret_params})
            except httpx.TransportError as e:
                problem = type(e).__name__   # not str(e): it can contain the URL
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in RETRY_STATUS:
                    raise FetchError(f"{self.name} {path}: HTTP {r.status_code}", r.status_code)
                problem, retry_after = f"HTTP {r.status_code}", r.headers.get("Retry-After")
            delay = (float(retry_after) if retry_after and retry_after.isdigit()
                     else min(60, 2 ** attempt) + random.random())
            print(f"  {self.name} {path}: {problem}, retrying in {delay:.0f}s")
            time.sleep(delay)
        raise FetchError(f"{self.name} {path}: gave up after {MAX_ATTEMPTS} attempts ({problem})")
