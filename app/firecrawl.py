"""Firecrawl v2 client. Works against Firecrawl Local (`firecrawl-local serve`, no key) or Firecrawl Cloud (key).

FIRECRAWL_BASE_URL  default http://127.0.0.1:3002 (local). Set https://api.firecrawl.dev for cloud.
FIRECRAWL_API_KEY   only needed for cloud.
"""
from __future__ import annotations

import os

import httpx

DEFAULT_LOCAL = "http://127.0.0.1:3002"


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


async def scrape(client: httpx.AsyncClient, url: str, timeout_ms: int = 20000) -> dict | None:
    """Return {"url","title","markdown"} or None. Full page (not main-content only): footers carry dealer phone/address."""
    try:
        r = await client.post(f"{base_url()}/v2/scrape", headers=_headers(), timeout=timeout_ms / 1000 + 10,
                              json={"url": url, "formats": ["markdown"], "onlyMainContent": False, "timeout": timeout_ms})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    data = body.get("data") or {}
    md = data.get("markdown")
    if not body.get("success", True) or not md:
        return None
    meta = data.get("metadata") or {}
    return {"url": url, "title": meta.get("title") or "", "markdown": md}


async def search(client: httpx.AsyncClient, query: str, *, country: str = "US", limit: int = 5,
                 timeout_ms: int = 45000) -> list[dict]:
    """Firecrawl /v2/search (cloud). Returns data.web[] items. Empty list on failure."""
    r = await client.post(
        f"{base_url()}/v2/search", headers=_headers(),
        json={"query": query, "limit": limit, "country": country, "timeout": timeout_ms,
              "sources": [{"type": "web"}],
              "scrapeOptions": {"formats": [{"type": "markdown"}], "onlyMainContent": True}},
    )
    if r.status_code != 200 or not r.json().get("success"):
        return []
    return r.json().get("data", {}).get("web", []) or []
