"""Firecrawl v2 client. Works against Firecrawl Local (`firecrawl-local serve`, no key) or Firecrawl Cloud (key).

FIRECRAWL_BASE_URL  default http://127.0.0.1:3002 (local). Set https://api.firecrawl.dev for cloud.
FIRECRAWL_API_KEY   only needed for cloud.

Firecrawl Local serves /v2/scrape, /v2/map, /v2/crawl and /v2/batch/scrape (no /v2/search, no LLM /v2/extract).
"""
from __future__ import annotations

import os
import time

import httpx

from .netsafe import is_public_url

DEFAULT_LOCAL = "http://127.0.0.1:3002"
PAGE_CACHE_TTL_S = 6 * 3600
_page_cache: dict[str, tuple[float, dict]] = {}


def base_url() -> str:
    return os.getenv("FIRECRAWL_BASE_URL", DEFAULT_LOCAL).rstrip("/")


def _headers() -> dict:
    key = os.getenv("FIRECRAWL_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def is_local() -> bool:
    return base_url().startswith(("http://127.", "http://localhost", "http://0.0.0.0"))


async def healthy(client: httpx.AsyncClient) -> bool:
    """True if a scrape endpoint is reachable (local server up, or cloud with a key)."""
    if not is_local():
        return bool(os.getenv("FIRECRAWL_API_KEY"))
    try:
        r = await client.get(base_url() + "/", timeout=2)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def cached(url: str) -> dict | None:
    hit = _page_cache.get(url)
    return dict(hit[1]) if hit and time.time() - hit[0] < PAGE_CACHE_TTL_S else None


def remember(url: str, page: dict) -> None:
    if len(_page_cache) > 3000:                              # bounded: drop the oldest third
        for k in sorted(_page_cache, key=lambda k: _page_cache[k][0])[:1000]:
            _page_cache.pop(k, None)
    _page_cache[url] = (time.time(), dict(page))


async def scrape(client: httpx.AsyncClient, url: str, timeout_ms: int = 20000, wait_ms: int = 0) -> dict | None:
    """Return {"url","title","markdown","links"} or None. Full page (not main-content only): footers carry dealer
    phone/address. `links` lets the research loop open product pages found on a listing/category page."""
    if (hit := cached(url)) is not None:
        return hit
    if not await is_public_url(url):
        return None
    body = {"url": url, "formats": ["markdown", "links"], "onlyMainContent": False, "timeout": timeout_ms}
    if wait_ms:
        body["waitFor"] = wait_ms
    try:
        r = await client.post(f"{base_url()}/v2/scrape", headers=_headers(), timeout=timeout_ms / 1000 + 10, json=body)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    data = payload.get("data") or {}
    md = data.get("markdown")
    if not payload.get("success", True) or not md:
        return None
    meta = data.get("metadata") or {}
    page = {"url": url, "title": meta.get("title") or "", "markdown": md,
            "links": [x for x in (data.get("links") or []) if isinstance(x, str)][:400]}
    remember(url, page)
    return page


async def search(client: httpx.AsyncClient, query: str, *, country: str = "US", limit: int = 5,
                 timeout_ms: int = 45000) -> list[dict]:
    """Firecrawl /v2/search (cloud only). Returns data.web[] items. Empty list on failure."""
    try:
        r = await client.post(
            f"{base_url()}/v2/search", headers=_headers(),
            json={"query": query, "limit": limit, "country": country, "timeout": timeout_ms,
                  "sources": [{"type": "web"}],
                  "scrapeOptions": {"formats": [{"type": "markdown"}], "onlyMainContent": True}},
        )
        if r.status_code != 200 or not r.json().get("success"):
            return []
        return r.json().get("data", {}).get("web", []) or []
    except (httpx.HTTPError, ValueError):
        return []
