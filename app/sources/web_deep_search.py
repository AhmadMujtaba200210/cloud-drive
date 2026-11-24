"""Deep web search: Firecrawl (search + page markdown) -> LLM chain (Groq -> Gemini -> OpenRouter) extraction.

Anti-hallucination guarantees (enforced in code, not trusted to the prompt):
  * an offer is kept only if its source_url is one of the pages Firecrawl actually fetched;
  * the vendor name must appear on that page;
  * a price is kept only if the model's verbatim price_text occurs on the page and contains the price digits;
    otherwise the offer becomes contact-only (price=None);
  * phone/email are kept only if they occur on the page.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time

import httpx

from .. import firecrawl, llm
from ..config import countries, load
from ..models import Authenticity, Condition, Offer, PartQuery, Vendor
from .base import SourceAdapter

log = logging.getLogger("deep_search")

DEFAULT_COUNTRIES = ["PK", "AE", "JP", "DE", "GB", "US", "CN", "TH", "MY", "SA", "TR", "KR"]
LANG_HINT = {"JP": "Japanese", "DE": "German", "CN": "Chinese", "TH": "Thai", "TR": "Turkish", "KR": "Korean",
             "SA": "Arabic or English", "AE": "English", "PK": "English or Urdu"}
MAX_PAGE_CHARS = 9000
CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, list[Offer]]] = {}

EXPAND_SYSTEM = (
    "You plan web searches for car parts. Output JSON only. Never invent part numbers you are not sure of; use null."
)
EXTRACT_SYSTEM = (
    "You extract car-part sale listings and parts dealers from web page text. Output JSON only. "
    "Use ONLY facts written on the pages. If a field is not on the page, use null. Never guess prices, phones, "
    "addresses or ratings. price_text must be copied character-for-character from the page."
)


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _countries() -> list[str]:
    env = os.getenv("DEEP_SEARCH_COUNTRIES")
    return [c.strip().upper() for c in env.split(",")] if env else DEFAULT_COUNTRIES


async def expand_queries(q: PartQuery, ccs: list[str]) -> dict:
    base = " ".join(filter(None, [q.oem_number, q.make, q.model, str(q.year or ""), q.part_name]))
    fallback = {"queries": {cc: [f"{base} price"] for cc in ccs}, "est_weight_kg": None, "est_dims_cm": None}
    prompt = (
        f"Part: {base}. VIN: {q.vin or 'n/a'}.\n"
        f"For each country code in {ccs}, write 2 web search queries a buyer would use to find sellers or dealers of "
        f"this exact part there, in the local language ({json.dumps({c: LANG_HINT.get(c, 'English') for c in ccs})}). "
        "Include words like price / for sale / dealer / breaker / genuine.\n"
        'Also estimate the packed shipping weight and box size.\nReturn {"queries": {"<CC>": ["q1","q2"]}, '
        '"est_weight_kg": number, "est_dims_cm": [L,W,H], "oem_number_hint": string|null}'
    )
    try:
        out, _ = await llm.complete_json(EXPAND_SYSTEM, prompt, max_tokens=1500)
        qs = out.get("queries") or {}
        out["queries"] = {cc: [x for x in qs.get(cc, []) if isinstance(x, str)][:2] or fallback["queries"][cc] for cc in ccs}
        return out
    except (llm.LLMUnavailable, AttributeError):
        return fallback


def _extract_prompt(q: PartQuery, cc: str, pages: list[dict]) -> str:
    blocks = []
    for i, p in enumerate(pages):
        blocks.append(f"### PAGE {i}\nURL: {p['url']}\nTITLE: {p.get('title') or ''}\n{(p.get('markdown') or p.get('description') or '')[:MAX_PAGE_CHARS]}")
    return (
        f"Wanted part: {q.make} {q.model} {q.year or ''} {q.part_name} (OEM {q.oem_number or 'unknown'}). "
        f"Search country: {cc}.\nFrom the pages below, list every seller listing or parts dealer relevant to this part. "
        "Skip unrelated parts and other car models.\n"
        'Return {"offers": [{"source_url": str, "vendor_name": str, "country_code": str|null, "city": str|null, '
        '"address": str|null, "phone": str|null, "email": str|null, "title": str, "price": number|null, '
        '"currency": "ISO 4217"|null, "price_text": str|null, "condition": "new"|"used"|"refurbished"|null, '
        '"authenticity": "oem"|"oes"|"aftermarket"|null, "rating": number|null, "review_count": int|null, '
        '"ships_to_pakistan": bool|null}]}\n\n' + "\n\n".join(blocks)
    )


def verify(raw: dict, pages_by_url: dict[str, dict], default_cc: str, provider: str, est: dict) -> Offer | None:
    """Turn one LLM-extracted record into an Offer, or drop it if it is not grounded in a fetched page."""
    page = pages_by_url.get((raw.get("source_url") or "").strip())
    if not page:
        return None
    text = _norm((page.get("markdown") or "") + " " + (page.get("title") or "") + " " + (page.get("description") or ""))
    name = (raw.get("vendor_name") or "").strip()
    if not name or _norm(name) not in text:
        return None

    price, currency, evidence = raw.get("price"), (raw.get("currency") or "").upper() or None, None
    fx_table = load("fx")["pkr_per_unit"]
    ptxt = raw.get("price_text")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None:
        int_digits = _digits(str(int(price)))
        ok = (ptxt and _norm(ptxt) in text and int_digits and int_digits in _digits(ptxt)
              and currency in fx_table and price > 0)
        if ok:
            evidence = ptxt
        else:
            price, currency = None, None

    phone = raw.get("phone")
    if phone and (len(_digits(phone)) < 7 or _digits(phone)[-7:] not in _digits(text)):
        phone = None
    email = raw.get("email")
    if email and email.lower() not in text:
        email = None
    cc = (raw.get("country_code") or default_cc).upper()
    if cc not in countries():
        cc = default_cc
    rating = raw.get("rating")
    rating = float(rating) if isinstance(rating, (int, float)) and 0 <= rating <= 5 else None

    def enum(cls, v, d):
        try:
            return cls(v)
        except ValueError:
            return d

    w = est.get("est_weight_kg")
    dims = est.get("est_dims_cm")
    has_est = isinstance(w, (int, float)) and w > 0 and isinstance(dims, list) and len(dims) == 3
    vid = hashlib.sha1(f"{name}|{page['url']}".encode()).hexdigest()[:12]
    return Offer(
        vendor=Vendor(
            id=f"web:{vid}", name=name, country=cc, region=(raw.get("city") or "-").strip(),
            address=raw.get("address"), phone=phone, email=email, website=page["url"], rating=rating,
            review_count=int(raw.get("review_count") or 0) if rating is not None else 0,
            ships_to_pk=raw.get("ships_to_pakistan") is not False, source=f"web_deep_search/{provider}",
        ),
        title=(raw.get("title") or name)[:200], condition=enum(Condition, raw.get("condition"), Condition.used),
        authenticity=enum(Authenticity, raw.get("authenticity"), Authenticity.aftermarket),
        price=price, currency=currency or "PKR", url=page["url"], evidence=evidence,
        weight_kg=float(w) if has_est else 3.0, dims_cm=tuple(float(x) for x in dims) if has_est else (40, 40, 25),
        weight_is_estimate=True, handling_days=3,
    )


class WebDeepSearch(SourceAdapter):
    name = "web_deep_search"

    def enabled(self) -> bool:
        has_llm = any(p.key for p in llm.default_chain())
        return has_llm and firecrawl.enabled() and os.getenv("DISABLE_DEEP_SEARCH") != "1"

    async def search(self, q: PartQuery) -> list[Offer]:
        key = q.model_dump_json(include={"make", "model", "year", "part_name", "oem_number"})
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        ccs = _countries()
        plan = await expand_queries(q, ccs)
        fc_sem, llm_sem = asyncio.Semaphore(4), asyncio.Semaphore(3)
        offers: list[Offer] = []

        async with httpx.AsyncClient(timeout=60) as client:
            async def one_country(cc: str) -> list[Offer]:
                pages: dict[str, dict] = {}
                for query in plan["queries"][cc]:
                    async with fc_sem:
                        try:
                            for p in await firecrawl.search(client, query, country=cc, limit=5):
                                if p.get("url"):
                                    pages.setdefault(p["url"].strip(), p)
                        except httpx.HTTPError as e:
                            log.info("firecrawl %s failed: %s", cc, e)
                if not pages:
                    return []
                async with llm_sem:
                    try:
                        out, provider = await llm.complete_json(EXTRACT_SYSTEM, _extract_prompt(q, cc, list(pages.values())),
                                                                client=client, max_tokens=6000)
                    except llm.LLMUnavailable:
                        return []
                raws = out.get("offers", []) if isinstance(out, dict) else []
                return [o for r in raws if isinstance(r, dict) and (o := verify(r, pages, cc, provider, plan))]

            for res in await asyncio.gather(*(one_country(c) for c in ccs), return_exceptions=True):
                if isinstance(res, list):
                    offers.extend(res)
        _cache[key] = (time.time(), offers)
        return offers
