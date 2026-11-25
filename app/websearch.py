"""Free web search via a self-hosted SearXNG instance (JSON API), plus a dependency-free page fetcher fallback."""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urljoin

import httpx

from .netsafe import is_public_url

LANG = {"PK": "en-PK", "AE": "en-AE", "SA": "ar-SA", "OM": "en", "QA": "en", "JP": "ja-JP", "KR": "ko-KR",
        "CN": "zh-CN", "TH": "th-TH", "MY": "en-MY", "SG": "en-SG", "DE": "de-DE", "GB": "en-GB", "IT": "it-IT",
        "TR": "tr-TR", "US": "en-US", "CA": "en-CA"}

# Sites that rarely hold part listings with seller details, or block scrapers.
SKIP_HOSTS = ("facebook.com", "instagram.com", "youtube.com", "tiktok.com", "pinterest.", "reddit.com",
              "wikipedia.org", "x.com", "twitter.com", "linkedin.com", "quora.com", "medium.com")
# Social shops: many Pakistani parts sellers only sell through Facebook / Instagram pages and groups.
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "tiktok.com")


# Only individual posts / listings carry a seller + price; feeds, hashtag, discover and category pages do not.
SOCIAL_POST = re.compile(
    r"facebook\.com/(marketplace/item/\d+|[^/?#]+/(posts|videos|photos)/|groups/[^/?#]+/(posts|permalink)/|permalink\.php|"
    r"story\.php|share/[pv]/|watch/?\?v=|reel/)"
    r"|instagram\.com/(p|reel|tv)/[\w-]+"
    r"|tiktok\.com/@[^/?#]+/(video|photo)/\d+", re.I)


def is_social_post(url: str) -> bool:
    return bool(SOCIAL_POST.search(url))


def is_social(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)
MAX_FETCH_BYTES = 3_000_000


def searxng_url() -> str | None:
    return (os.getenv("SEARXNG_URL") or "").rstrip("/") or None


def _skip(url: str, allow_social: bool = False) -> bool:
    if allow_social and is_social(url):
        return not is_social_post(url)
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return any(host == h or host.endswith("." + h) or (h.endswith(".") and h in host) for h in SKIP_HOSTS)


async def searxng(client: httpx.AsyncClient, query: str, country: str, pages: int = 1,
                  allow_social: bool = False) -> list[dict]:
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
            if not url.startswith("http") or _skip(url, allow_social):
                continue
            out.append({"url": url, "title": x.get("title") or "", "snippet": x.get("content") or "",
                        "engine": x.get("engine") or ""})
    return out


_TAG = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_HREF = re.compile(r"""<a\s[^>]*href=["']([^"'#]+)["']""", re.I)


def html_to_text(raw: str) -> str:
    raw = _TAG.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", raw).strip()


async def plain_fetch(client: httpx.AsyncClient, url: str, max_redirects: int = 4) -> dict | None:
    """Fallback when Firecrawl is down: plain GET + tag stripping. No JS rendering.
    Redirects are followed by hand so every hop passes the SSRF check; the body is size-capped."""
    current = url
    for _ in range(max_redirects + 1):
        if not await is_public_url(current):
            return None
        try:
            async with client.stream("GET", current, timeout=15, follow_redirects=False,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; PartsBridgeBot/0.2)"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                    return None
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_FETCH_BYTES:
                        break
                text = body.decode(r.encoding or "utf-8", errors="replace")
        except httpx.HTTPError:
            return None
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        links = list(dict.fromkeys(urljoin(current, h) for h in _HREF.findall(text)))[:400]
        return {"url": url, "title": html.unescape(m.group(1).strip()) if m else "", "markdown": html_to_text(text),
                "links": links}
    return None
