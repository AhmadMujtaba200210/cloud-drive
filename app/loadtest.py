"""Load-test mode (LOADTEST_MODE=1). NEVER enable in production.

Replaces every outside dependency with a stand-in that waits a realistic time and returns realistic data, so a load
test measures THIS server (event loop, queueing, streaming, verification, landed-cost maths, nginx) without spending
AI / Google / Tavily quota or hitting real shops' websites:

  SearXNG search      0.3-1.5 s   20 results across .pk shops, marketplaces and social posts
  Nimble / Tavily     3-12 s / 1-4 s
  Firecrawl scrape    0.5-3 s     a product page with a price, seller name and phone
  LLM                 2-20 s      planner / triage / extraction JSON consistent with the fake pages

Each simulated user sends X-Loadtest-User, used as its client identity so per-visitor limits apply per user.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re

ENABLED = os.getenv("LOADTEST_MODE") == "1"


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16))


SHOPS = ["autostore.pk", "gari.pk", "pkgreenautos.com", "ndestore.com", "qualityautos.pk", "partex.com.pk",
         "gerage.pk", "kabulicarparts.com", "stonetech.pk", "pakautoparts.com"]


async def fake_searxng(client, query, country, pages=1, allow_social=False):
    await asyncio.sleep(random.uniform(0.3, 1.5))
    r = _rng(query)
    out = []
    for i in range(20):
        shop = r.choice(SHOPS)
        slug = re.sub(r"\W+", "-", query.lower())[:60]
        out.append({"url": f"https://www.{shop}/p/{slug}-{r.randint(1, 10**6)}", "title": f"{query} | {shop}",
                    "snippet": f"{query} available at {shop}. Rs {r.randint(30, 400) * 100:,}", "engine": "loadtest"})
    if allow_social:
        out.append({"url": f"https://www.facebook.com/marketplace/item/{r.randint(10**9, 10**10)}/",
                    "title": f"{query} for sale", "snippet": f"{query} Rs {r.randint(30, 300) * 100:,} Lahore call 0300 1234567",
                    "engine": "loadtest"})
    return out


async def fake_paid(client, query, cc):
    await asyncio.sleep(random.uniform(3, 12) if "nimble" in str(fake_paid.__name__) else random.uniform(1, 4))
    return (await fake_searxng(client, query + " g", cc))[:10]


async def fake_scrape(client, url, timeout_ms=20000, wait_ms=0):
    await asyncio.sleep(random.uniform(0.5, 3.0))
    r = _rng(url)
    host = url.split("/")[2].removeprefix("www.")
    price = r.randint(30, 400) * 100
    md = "\n".join([f"# {host} Toyota Corolla AC condenser", "Home > Parts > Cooling",
                    *[f"Other item {i} Rs {r.randint(5, 90) * 100:,}" for i in range(r.randint(5, 60))],
                    f"## Toyota Corolla 2002-2008 AC condenser", f"Price: Rs {price:,}", "In stock, new, aftermarket",
                    f"Seller: {host} Lahore Pakistan", "Call 0300 1234567", "Ships across Pakistan"])
    return {"url": url, "title": f"{host} Toyota Corolla AC condenser", "markdown": md, "links": []}


async def fake_llm(system, user, *, chain=None, client=None, timeout=45, max_tokens=4096):
    await asyncio.sleep(random.uniform(2, 20) if "extract" in system.lower() else random.uniform(1, 5))
    if "plan web research" in system:
        ccs = re.findall(r"'([A-Z]{2})'", user) or ["PK"]
        return {"thinking": "Load test plan.", "aliases": ["condenser"], "est_weight_kg": 4, "est_dims_cm": [60, 45, 8],
                "queries": {cc: [f"loadtest {cc} q{i} {random.random():.6f}" for i in range(5)] for cc in ccs}}, "loadtest"
    if "rank web search results" in system:
        n = len(re.findall(r"^\d+\. ", user, re.M))
        k = int(re.search(r"up to (\d+)", user).group(1)) if re.search(r"up to (\d+)", user) else 6
        return {"keep": list(range(min(n, k))), "why": "load test"}, "loadtest"
    offers = []
    for m in re.finditer(r"URL: (\S+)\nTITLE: [^\n]*\n(?:SEARCH SNIPPET: [^\n]*\n)?([\s\S]*?)</page>", user):
        url, body = m.group(1), m.group(2)
        host = url.split("/")[2].removeprefix("www.")
        pm = re.search(r"Price: (Rs [\d,]+)", body) or re.search(r"(Rs [\d,]+)", body)
        if pm:
            offers.append({"source_url": url, "vendor_name": host, "country_code": "PK", "city": "Lahore",
                           "phone": "0300 1234567", "title": "Toyota Corolla 2002-2008 AC condenser",
                           "price": float(pm.group(1)[3:].replace(",", "")), "currency": "PKR", "price_text": pm.group(1),
                           "condition": "new", "authenticity": "aftermarket", "item_sold": "AC condenser",
                           "is_requested_part": True, "location_quote": "Lahore Pakistan", "oem_numbers": []})
    return {"offers": offers}, "loadtest"


def install() -> None:
    """Patch outside dependencies. Called from app.main at import time when LOADTEST_MODE=1."""
    if not ENABLED:
        return
    from . import firecrawl, llm, searchproviders, websearch
    from .sources import web_deep_search

    async def healthy(client):
        return True

    async def nimble(client, query, cc):
        await asyncio.sleep(random.uniform(3, 12))
        return (await fake_searxng(client, query + " google", cc))[:10]

    async def tavily(client, query, cc):
        await asyncio.sleep(random.uniform(1, 4))
        return (await fake_searxng(client, query + " tavily", cc))[:10]

    websearch.searxng = fake_searxng
    websearch.searxng_url = lambda: "http://loadtest"
    searchproviders.PAID = {"nimble": ("LOADTEST_MODE", nimble), "tavily": ("LOADTEST_MODE", tavily)}
    firecrawl.scrape = fake_scrape
    firecrawl.healthy = healthy
    llm.complete_json = fake_llm
    llm.default_chain = lambda: [llm.Provider("loadtest", "http://x", "LOADTEST_MODE", "m")]
    web_deep_search.KB.configured = staticmethod(lambda: False)
    os.environ["DISABLE_DEMO"] = "1"
    print(json.dumps({"loadtest_mode": True}))
