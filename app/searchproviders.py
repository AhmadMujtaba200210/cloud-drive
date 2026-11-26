"""Search-provider router: SearXNG (free, self-hosted) + Nimble (real Google SERP, localized) + Tavily.

Why several providers: no single source sees everything (our SearXNG cannot query Google from a cloud IP; Google
is where most Pakistani sellers rank), and when many users search at once one provider's rate limit or quota must
not stall everyone. Each paid provider has:

  * a token-bucket rate limiter shared by every search on this server (requests per minute);
  * a daily budget, so the free tiers last the month (NIMBLE_DAILY_CAP / TAVILY_DAILY_CAP);
  * a circuit breaker: 429 -> pause for Retry-After; 401/402/432/433 (bad key / out of quota) -> pause 30 min;
    timeouts / 5xx -> pause 60 s. While paused, the other providers carry the load;
  * a shared result cache in the Oracle knowledge base (3 days) + in memory, so a query another user already ran
    costs nothing.

Paid providers run only on "primary" queries (the most important ones of each search); SearXNG runs on all.
Results are merged by URL, keeping every engine's snippet as evidence and Google's ranking first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from . import websearch
from .kb import kb as KB, sha1
from .netsafe import syntactically_public

log = logging.getLogger("search")

SERP_TTL_S = 3 * 86400
_mem: dict[str, tuple[float, list[dict]]] = {}

TAVILY_COUNTRY = {"PK": "pakistan", "AE": "united arab emirates", "JP": "japan", "DE": "germany", "GB": "united kingdom",
                  "US": "united states", "CN": "china", "TH": "thailand", "MY": "malaysia", "SA": "saudi arabia",
                  "TR": "turkey", "KR": "south korea"}


@dataclass
class Bucket:
    """Token bucket (per minute) + daily cap + circuit breaker, shared by all requests in this process."""
    per_minute: int
    daily_cap: int
    tokens: float = 0.0
    updated: float = field(default_factory=time.time)
    day: str = ""
    used_today: int = 0
    paused_until: float = 0.0
    last_error: str = ""

    def __post_init__(self):
        self.tokens = float(self.per_minute)

    def available(self) -> tuple[bool, str]:
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != self.day:
            self.day, self.used_today = today, 0
        if now < self.paused_until:
            return False, f"paused ({self.last_error})"
        if self.used_today >= self.daily_cap:
            return False, "daily budget used"
        self.tokens = min(self.per_minute, self.tokens + (now - self.updated) * self.per_minute / 60)
        self.updated = now
        if self.tokens < 1:
            return False, "rate limit"
        return True, ""

    def take(self) -> None:
        self.tokens -= 1
        self.used_today += 1

    def fail(self, status: int | None, retry_after: str | None = None) -> None:
        if status == 429:
            try:
                wait = float(retry_after or 30)
            except ValueError:
                wait = 30
            self.paused_until, self.last_error = time.time() + min(max(wait, 5), 600), "rate limited"
        elif status in (401, 402, 403, 432, 433):
            self.paused_until, self.last_error = time.time() + 1800, f"quota/auth HTTP {status}"
        else:
            self.paused_until, self.last_error = time.time() + 60, f"error {status or 'timeout'}"


def _env_int(k: str, d: int) -> int:
    try:
        return int(os.getenv(k, d))
    except ValueError:
        return d


BUCKETS: dict[str, Bucket] = {}


def bucket(name: str) -> Bucket:
    if name not in BUCKETS:
        if name == "nimble":
            BUCKETS[name] = Bucket(_env_int("NIMBLE_PER_MINUTE", 60), _env_int("NIMBLE_DAILY_CAP", 160))
        else:
            BUCKETS[name] = Bucket(_env_int("TAVILY_PER_MINUTE", 60), _env_int("TAVILY_DAILY_CAP", 30))
    return BUCKETS[name]


def status() -> dict:
    """For diagnostics: which providers are configured and their budget state."""
    out = {"searxng": bool(websearch.searxng_url())}
    for name, key in (("nimble", "NIMBLE_API_KEY"), ("tavily", "TAVILY_API_KEY")):
        if os.getenv(key):
            b = bucket(name)
            b.available()
            out[name] = {"used_today": b.used_today, "daily_cap": b.daily_cap,
                         "paused": b.paused_until > time.time(), "last_error": b.last_error or None}
    return out


# ---------------- providers ----------------
async def _resolve_google(client: httpx.AsyncClient, href: str) -> str | None:
    """Google SERP links can be '/goto?url=...' redirects: one HEAD-less GET without following returns a 302 with
    the real URL in Location."""
    if href.startswith("http"):
        return href
    if not href.startswith("/goto") and not href.startswith("/url"):
        return None
    try:
        r = await client.get("https://www.google.com" + href, follow_redirects=False, timeout=6,
                             headers={"User-Agent": "Mozilla/5.0"})
        loc = r.headers.get("location")
        return loc if loc and loc.startswith("http") else None
    except httpx.HTTPError:
        return None


async def nimble(client: httpx.AsyncClient, query: str, cc: str) -> list[dict]:
    r = await client.post("https://sdk.nimbleway.com/v2/serp", timeout=40,
                          headers={"Authorization": f"Bearer {os.environ['NIMBLE_API_KEY']}"},
                          json={"search_engine": "google_search", "query": query, "country": cc, "locale": "en",
                                "num_results": 20, "no_html": True})
    if r.status_code != 200:
        raise ProviderError(r.status_code, r.headers.get("retry-after"))
    ents = ((r.json().get("data") or {}).get("parsing") or {}).get("entities") or {}
    org = [o for o in ents.get("OrganicResult") or [] if isinstance(o, dict)]
    urls = await asyncio.gather(*(_resolve_google(client, o.get("url") or "") for o in org))
    return [{"url": u, "title": o.get("title") or "", "snippet": o.get("snippet") or "", "engine": "google(nimble)"}
            for o, u in zip(org, urls) if u]


async def tavily(client: httpx.AsyncClient, query: str, cc: str) -> list[dict]:
    body = {"query": query, "search_depth": "basic", "topic": "general", "max_results": 15}
    if cc in TAVILY_COUNTRY:
        body["country"] = TAVILY_COUNTRY[cc]
    r = await client.post("https://api.tavily.com/search", timeout=30, json=body,
                          headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"})
    if r.status_code != 200:
        raise ProviderError(r.status_code, r.headers.get("retry-after"))
    return [{"url": x.get("url") or "", "title": x.get("title") or "", "snippet": (x.get("content") or "")[:600],
             "engine": "tavily"} for x in r.json().get("results") or [] if isinstance(x, dict)]


class ProviderError(Exception):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"HTTP {status}")
        self.status, self.retry_after = status, retry_after


PAID = {"nimble": ("NIMBLE_API_KEY", nimble), "tavily": ("TAVILY_API_KEY", tavily)}


async def _paid(name: str, client: httpx.AsyncClient, query: str, cc: str, stats: dict) -> list[dict]:
    key_env, fn = PAID[name]
    if not os.getenv(key_env):
        return []
    ck = sha1(f"serp|{name}|{cc}|{query.strip().lower()}")
    hit = _mem.get(ck)
    if hit and time.time() - hit[0] < SERP_TTL_S:
        stats["cached"] = stats.get("cached", 0) + 1
        return hit[1]
    if KB.configured():
        stored = await KB.get_plan(ck)                     # pb_plan doubles as a small JSON cache
        if isinstance(stored, dict) and time.time() - stored.get("t", 0) < SERP_TTL_S:
            _mem[ck] = (stored["t"], stored["results"])
            stats["cached"] = stats.get("cached", 0) + 1
            return stored["results"]
    b = bucket(name)
    ok, why = b.available()
    if not ok:
        stats.setdefault("skipped", {})[name] = why
        return []
    b.take()
    try:
        res = await fn(client, query, cc)
    except ProviderError as e:
        b.fail(e.status, e.retry_after)
        log.warning("%s failed: HTTP %s", name, e.status)
        stats.setdefault("errors", {})[name] = e.status
        return []
    except (httpx.HTTPError, ValueError, KeyError) as e:
        b.fail(None)
        log.warning("%s failed: %s", name, type(e).__name__)
        stats.setdefault("errors", {})[name] = type(e).__name__
        return []
    stats[name] = stats.get(name, 0) + 1
    _mem[ck] = (time.time(), res)
    if len(_mem) > 4000:
        for k in sorted(_mem, key=lambda k: _mem[k][0])[:1000]:
            _mem.pop(k, None)
    if KB.configured():
        asyncio.ensure_future(KB.put_plan(ck, {"t": time.time(), "results": res}))
    return res


def merge(*lists: list[dict]) -> list[dict]:
    """Union by URL in priority order; snippets from every engine are kept as extra evidence."""
    out: dict[str, dict] = {}
    for lst in lists:
        for r in lst:
            u = (r.get("url") or "").strip()
            if not u or not syntactically_public(u):
                continue
            if u not in out:
                out[u] = dict(r, engines=[r.get("engine", "")])
            else:
                cur = out[u]
                if r.get("snippet") and r["snippet"] not in (cur.get("snippet") or ""):
                    cur["snippet"] = ((cur.get("snippet") or "") + " … " + r["snippet"])[:1500]
                cur["engines"].append(r.get("engine", ""))
    return list(out.values())


async def search(client: httpx.AsyncClient, query: str, cc: str, *, primary: bool, allow_social: bool,
                 stats: dict) -> list[dict]:
    """SearXNG always; Nimble + Tavily on primary queries. Runs concurrently; any provider may fail or be paused."""
    jobs = [websearch.searxng(client, query, cc, allow_social=allow_social)]
    names = ["searxng"]
    if primary:
        for name in PAID:
            jobs.append(_paid(name, client, query, cc, stats))
            names.append(name)
    results = await asyncio.gather(*jobs, return_exceptions=True)
    lists = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            log.warning("%s search failed: %r", name, res)
            continue
        if name != "searxng":                                  # same social / skip-host rules for every provider
            res = [r for r in res if r.get("url") and not websearch._skip(r["url"], allow_social)]
        lists.append((name, res))
    order = {"nimble": 0, "tavily": 1, "searxng": 2}
    lists.sort(key=lambda x: order.get(x[0], 9))
    return merge(*(r for _, r in lists))


def debug_dump() -> str:
    return json.dumps(status())
