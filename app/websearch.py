"""Free web search via a self-hosted SearXNG instance (JSON API), plus a dependency-free page fetcher fallback."""
from __future__ import annotations

import html
import os
import re

import httpx

LANG = {"PK": "en-PK", "AE": "en-AE", "SA": "ar-SA", "OM": "en", "QA": "en", "JP": "ja-JP", "KR": "ko-KR",
        "CN": "zh-CN", "TH": "th-TH", "MY": "en-MY", "SG": "en-SG", "DE": "de-DE", "GB": "en-GB", "IT": "it-IT",
        "TR": "tr-TR", "US": "en-US", "CA": "en-CA"}

# Sites that rarely hold part listings with seller details, or block scrapers.
SKIP_HOSTS = ("facebook.com", "instagram.com", "youtube.com", "tiktok.com", "pinterest.", "reddit.com",
              "wikipedia.org", "x.com", "twitter.com", "linkedin.com")


def searxng_url() -> str | None:
    return (os.getenv("SEARXNG_URL") or "").rstrip("/") or None


async def searxng(client: httpx.AsyncClient, query: str, country: str, pages: int = 1) -> list[dict]:
    """Returns [{url, title, snippet, engine}]; empty on failure."""
    base = searxng_url()
    if not base:
        return []
    out: list[dict] = []
    for pageno in range(1, pages + 1):
        try:
            r = await client.get(f"{base}/search", timeout=20, params={
                "q": query, "format": "json", "language": LANG.get(country, "en"), "pageno": pageno, "safesearch": 0})
        except httpx.HTTPError:
            break
        if r.status_code != 200:
            break
        for x in r.json().get("results", []):
            url = x.get("url") or ""
            if not url.startswith("http") or any(h in url for h in SKIP_HOSTS):
                continue
            out.append({"url": url, "title": x.get("title") or "", "snippet": x.get("content") or "",
                        "engine": x.get("engine") or ""})
    return out


_TAG = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)


def html_to_text(raw: str) -> str:
    raw = _TAG.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", raw).strip()


async def plain_fetch(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fallback when Firecrawl is down: plain GET + tag stripping. No JS rendering."""
    try:
        r = await client.get(url, timeout=15, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; PartsBridgeBot/0.1)"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    return {"url": url, "title": html.unescape(m.group(1).strip()) if m else "", "markdown": html_to_text(r.text)}
