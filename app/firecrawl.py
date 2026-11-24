"""Minimal Firecrawl v2 client (search + scrape to markdown). Docs: https://docs.firecrawl.dev/api-reference/endpoint/search"""
from __future__ import annotations

import os

import httpx

BASE = "https://api.firecrawl.dev/v2"


def enabled() -> bool:
    return bool(os.getenv("FIRECRAWL_API_KEY"))


async def search(client: httpx.AsyncClient, query: str, *, country: str = "US", limit: int = 5,
                 timeout_ms: int = 45000) -> list[dict]:
    """Returns data.web[] items: {url, title, description, markdown, ...}. Empty list on failure."""
    r = await client.post(
        f"{BASE}/search",
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
        json={"query": query, "limit": limit, "country": country, "timeout": timeout_ms,
              "sources": [{"type": "web"}],
              "scrapeOptions": {"formats": [{"type": "markdown"}], "onlyMainContent": True}},
    )
    if r.status_code != 200:
        return []
    body = r.json()
    if not body.get("success"):
        return []
    return body.get("data", {}).get("web", []) or []
