"""Deep web search: SearXNG (free) -> LLM triage -> Firecrawl Local scrape -> LLM chain (Groq -> Gemini -> OpenRouter) extraction.

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
from urllib.parse import unquote

import httpx

from .. import firecrawl, llm, websearch
from ..config import countries, load
from ..models import Authenticity, Condition, Offer, PartQuery, Vendor
from .base import SourceAdapter

log = logging.getLogger("deep_search")

DEFAULT_COUNTRIES = ["PK", "AE", "JP", "DE", "GB", "US", "CN", "TH", "MY", "SA", "TR", "KR"]
LANG_HINT = {"JP": "Japanese", "DE": "German", "CN": "Chinese", "TH": "Thai", "TR": "Turkish", "KR": "Korean",
             "SA": "Arabic or English", "AE": "English", "PK": "English or Urdu"}
MAX_PAGE_CHARS = 3500
PAGES_PER_LLM_CALL = 3
CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, list[Offer], dict]] = {}

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


PRICE_RE = re.compile(r"(?:[$€£¥₩฿₺]|\b(?:USD|AED|SAR|JPY|EUR|GBP|CNY|RMB|THB|MYR|RM|TRY|TL|KRW|PKR|Rs\.?|Dhs?)\b)\s?\d[\d,.]*"
                      r"|\d[\d,.]*\s?(?:円|元|원|บาท|\b(?:AED|USD|EUR|JPY|PKR|SAR|TL)\b)", re.I)
CONTACT_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d|@|\b(contact|address|tel|phone|whatsapp|call|email|street|road|ave|"
                        r"industrial area|ships? from|location|住所|電話|adresse|telefon)\b", re.I)


def condense(md: str, part_words: list[str], budget: int = MAX_PAGE_CHARS, contact_budget: int = 900) -> str:
    """Keep the lines that matter for extraction instead of cutting by position (full pages start with navigation
    and end with the dealer's contact footer): part/price lines with 1 line of context, plus a reserved budget of
    contact/address lines taken from the END of the page, where footers live."""
    if len(md) <= budget:
        return md
    lines = [re.sub(r"\]\((https?://[^)]{40,})\)", "]", ln).strip() for ln in md.splitlines()]
    words = [w.lower() for w in part_words if len(w) > 2]

    contact_idx, used = [], 0
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i]
        if ln and CONTACT_RE.search(ln) and used + len(ln) <= contact_budget:
            contact_idx.append(i)
            used += len(ln) + 1
    content = set(range(min(6, len(lines))))
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(w in low for w in words) or PRICE_RE.search(ln):
            content.update((i - 1, i, i + 1))
    out_idx, size = set(contact_idx), used
    for i in sorted(k for k in content if 0 <= k < len(lines)):
        if i in out_idx or not lines[i]:
            continue
        if size + len(lines[i]) > budget:
            break
        out_idx.add(i)
        size += len(lines[i]) + 1
    return "\n".join(lines[i] for i in sorted(out_idx) if lines[i])


def _extract_prompt(q: PartQuery, cc: str, pages: list[dict]) -> str:
    blocks = []
    words = f"{q.make} {q.model} {q.part_name} {q.oem_number or ''}".split()
    for i, p in enumerate(pages):
        blocks.append(f"### PAGE {i}\nURL: {p['url']}\nTITLE: {p.get('title') or ''}\n{condense(p.get('markdown') or p.get('description') or '', words)}")
    return (
        f"Wanted part: {q.make} {q.model} {q.year or ''} {q.part_name} (OEM {q.oem_number or 'unknown'}). "
        f"Search country: {cc}.\nFrom the pages below, list every seller listing or parts dealer relevant to this part. "
        "Skip unrelated parts and other car models.\n"
        'Return {"offers": [{"source_url": str, "vendor_name": str, "country_code": str|null, "city": str|null, '
        '"address": str|null, "phone": str|null, "email": str|null, "title": str, "price": number|null, '
        '"currency": "ISO 4217"|null, "price_text": str|null, "condition": "new"|"used"|"refurbished"|null, '
        '"authenticity": "oem"|"oes"|"aftermarket"|null, "rating": number|null, "review_count": int|null, '
        '"ships_to_pakistan": bool|null, "item_sold": str, "is_requested_part": bool, "location_quote": str|null}]}\n"item_sold" = what is actually sold, in 2-5 words (e.g. "steering wheel cover", "complete steering wheel"). "is_requested_part" = true only if it is the requested part itself, not a cover, trim, accessory, sticker, the whole car, or a part for another model.\n"country_code" = where the SELLER is located per the page (address, phone code, domain); null if the page does not say. "location_quote" = the exact text on the page that shows where the seller is (address, "ships from ...", city), copied verbatim; null if none.\n\n' + "\n\n".join(blocks)
    )


UNKNOWN_CC = "XX"
TLD_CC = {".pk": "PK", ".ae": "AE", ".sa": "SA", ".om": "OM", ".qa": "QA", ".jp": "JP", ".kr": "KR", ".cn": "CN",
          ".th": "TH", ".my": "MY", ".sg": "SG", ".de": "DE", ".uk": "GB", ".it": "IT", ".tr": "TR", ".ca": "CA"}
DIAL_CC = {"92": "PK", "971": "AE", "966": "SA", "968": "OM", "974": "QA", "81": "JP", "82": "KR", "86": "CN",
           "66": "TH", "60": "MY", "65": "SG", "49": "DE", "44": "GB", "39": "IT", "90": "TR", "1": "US"}
COUNTRY_ALIASES = {"PK": ["pakistan", "پاکستان", "karachi", "lahore", "islamabad", "rawalpindi"],
                   "AE": ["united arab emirates", "uae", "dubai", "sharjah", "abu dhabi", "الإمارات"],
                   "JP": ["japan", "日本", "東京", "大阪", "名古屋"], "DE": ["germany", "deutschland"],
                   "CN": ["china", "中国"], "KR": ["korea", "대한민국", "한국"], "TH": ["thailand", "ประเทศไทย"],
                   "TR": ["turkey", "türkiye"], "GB": ["united kingdom", "england"], "US": ["united states", "usa"],
                   "SA": ["saudi arabia", "riyadh", "jeddah"], "MY": ["malaysia", "kuala lumpur"]}
ACCESSORY_WORDS = ("cover", "wrap", "sticker", "decal", "trim", "emblem", "badge", "sleeve", "protector", "toy", "model kit")


def _host_cc(url: str) -> str | None:
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    return next((cc for tld, cc in TLD_CC.items() if host.endswith(tld)), None)


def _phone_cc(phone: str | None) -> str | None:
    if not phone or not phone.strip().startswith(("+", "00")):
        return None
    d = _digits(phone)
    d = d[2:] if phone.strip().startswith("00") else d
    for n in (3, 2, 1):
        if d[:n] in DIAL_CC:
            return DIAL_CC[d[:n]]
    return None


CURRENCY_CC = {"USD": "US", "AED": "AE", "JPY": "JP", "GBP": "GB", "CNY": "CN", "THB": "TH", "MYR": "MY",
               "SAR": "SA", "TRY": "TR", "KRW": "KR", "CAD": "CA", "SGD": "SG", "PKR": "PK"}


def resolve_country(raw_cc: str | None, url: str, text: str, phone: str | None,
                    location_quote: str | None = None, currency: str | None = None) -> tuple[str | None, str]:
    """Seller country with its evidence. Never defaults to the searched country: a .com shop that appears in a
    Pakistan search is not a Pakistani vendor. Returns (cc or None, evidence)."""
    names = countries()
    by_phone, by_host = _phone_cc(phone), _host_cc(url)
    raw_cc = (raw_cc or "").upper() or None
    if raw_cc in names:
        if raw_cc in (by_phone, by_host):
            return raw_cc, "domain/phone"
        lq = _norm(location_quote or "")
        if lq and len(lq) >= 6 and lq in text:
            return raw_cc, f"address on page: {location_quote.strip()[:80]}"
        if any(a in text for a in [_norm(names[raw_cc])] + COUNTRY_ALIASES.get(raw_cc, [])):
            return raw_cc, "country/city named on page"
    if by_phone:
        return by_phone, "phone code"
    if by_host:
        return by_host, "domain"
    # Weak tier: priced in the named country's own currency. Never enough to call a seller local (PK).
    if raw_cc in names and raw_cc != "PK" and CURRENCY_CC.get((currency or "").upper()) == raw_cc:
        return raw_cc, "weak: currency matches country"
    return None, "unverified"


def parse_amount(text: str | None) -> float | None:
    """Parse the first amount in a price string, handling 1,234.56 / 1.234,56 / 5.280 (TR/DE thousands) / 48,000."""
    m = re.search(r"\d[\d.,\s]*\d|\d", text or "")
    if not m:
        return None
    tok = re.sub(r"\s", "", m.group(0))
    if "," in tok and "." in tok:
        dec = "," if tok.rfind(",") > tok.rfind(".") else "."
        tok = tok.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in tok or "." in tok:
        sep = "," if "," in tok else "."
        parts = tok.split(sep)
        if all(len(p) == 3 for p in parts[1:]):     # 48,000 / 5.280 / 1.234.567 -> thousands
            tok = "".join(parts)
        elif len(parts) == 2:                       # 749.55 / 12,5 -> decimal
            tok = parts[0] + "." + parts[1]
        else:
            return None
    try:
        return float(tok)
    except ValueError:
        return None


def canon_url(u: str) -> str:
    """Compare URLs modulo percent-encoding, trailing slash, scheme and 'www.' (LLMs often decode Thai/Japanese paths)."""
    u = unquote(u.strip()).lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def _by_canonical(pages: dict[str, dict]) -> dict[str, dict]:
    return {canon_url(k): v for k, v in pages.items()}


def verify(raw: dict, pages_by_url: dict[str, dict], default_cc: str, provider: str, est: dict,
           part_name: str = "", reasons: dict | None = None) -> Offer | None:
    """Turn one LLM-extracted record into an Offer, or drop it if it is not grounded in a fetched page."""
    def drop(why: str):
        if reasons is not None:
            reasons[why] = reasons.get(why, 0) + 1
        return None

    page = pages_by_url.get((raw.get("source_url") or "").strip()) or _by_canonical(pages_by_url).get(
        canon_url(raw.get("source_url") or ""))
    if not page:
        return drop("url_not_fetched")
    text = _norm((page.get("markdown") or "") + " " + (page.get("title") or "") + " " + (page.get("description") or ""))
    name = (raw.get("vendor_name") or "").strip()
    squash = lambda x: re.sub(r"[\W_]+", "", x.lower())          # "Toyota Parts Deal" ~ "ToyotaPartsDeal.com"
    sname = squash(re.sub(r"\.(com|pk|ae|jp|de|co\.uk|net|store)$", "", name, flags=re.I))
    if not name or (_norm(name) not in text and (len(sname) < 4 or sname not in squash(text))):
        return drop("vendor_not_on_page")
    if raw.get("is_requested_part") is False:
        return drop("not_requested_part")
    sold = _norm(f"{raw.get('item_sold') or ''} {raw.get('title') or ''}")
    wanted = _norm(part_name)
    if any(re.search(rf"\b{re.escape(w)}s?\b", sold) and not re.search(rf"\b{re.escape(w)}", wanted)
           for w in ACCESSORY_WORDS):
        return drop("accessory")

    price, currency, evidence = raw.get("price"), (raw.get("currency") or "").upper() or None, None
    fx_table = load("fx")["pkr_per_unit"]
    ptxt = raw.get("price_text")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None:
        parsed = parse_amount(ptxt)
        ok = (ptxt and _norm(ptxt) in text and parsed and price > 0 and abs(price - parsed) / parsed < 0.01
              and currency in fx_table)
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
    cc, cc_evidence = resolve_country(raw.get("country_code"), page["url"], text, phone, raw.get("location_quote"),
                                     currency if price is not None else None)
    if cc is None:
        # Location unknown: shipping and duty cannot be computed honestly -> contact-only, never "local".
        cc, price, currency, evidence = UNKNOWN_CC, None, None, None
    rating = raw.get("rating")
    rating = float(rating) if isinstance(rating, (int, float)) and 0 <= rating <= 5 else None

    def enum(cls, v, d):
        try:
            return cls(v)
        except ValueError:
            return d

    # Packed size: LLM estimate, floored by a conservative per-part-type minimum (LLM boxes ran small in testing).
    from ..customs import classify
    floor = classify(part_name or raw.get("title") or "")[1].get("min_pack", {"kg": 3.0, "dims_cm": [40, 40, 25]})
    w, dims = est.get("est_weight_kg"), est.get("est_dims_cm")
    if not (isinstance(w, (int, float)) and w > 0):
        w = floor["kg"]
    if not (isinstance(dims, list) and len(dims) == 3 and all(isinstance(x, (int, float)) and x > 0 for x in dims)):
        dims = floor["dims_cm"]
    w = max(float(w), floor["kg"])
    fd = floor["dims_cm"]
    if dims[0] * dims[1] * dims[2] < fd[0] * fd[1] * fd[2]:
        dims = fd
    has_est = True
    vid = hashlib.sha1(f"{name}|{page['url']}".encode()).hexdigest()[:12]
    return Offer(
        vendor=Vendor(
            id=f"web:{vid}", name=name, country=cc, region=(raw.get("city") or "-").strip(),
            address=raw.get("address"), phone=phone, email=email, website=page["url"], rating=rating,
            review_count=int(raw.get("review_count") or 0) if rating is not None else 0,
            ships_to_pk=raw.get("ships_to_pakistan") is not False, source=f"web_deep_search/{provider}",
            location_evidence=cc_evidence,
        ),
        title=(raw.get("title") or name)[:200], condition=enum(Condition, raw.get("condition"), Condition.used),
        authenticity=enum(Authenticity, raw.get("authenticity"), Authenticity.aftermarket),
        price=price, currency=currency or "PKR", url=page["url"], evidence=evidence,
        weight_kg=float(w) if has_est else 3.0, dims_cm=tuple(float(x) for x in dims) if has_est else (40, 40, 25),
        weight_is_estimate=True, handling_days=3,
    )


TRIAGE_SYSTEM = (
    "You rank web search results for a car-part buyer. Output JSON only. Prefer pages that are a seller listing, "
    "a parts shop / breaker / dealer page for THIS make and model, or a parts marketplace result page. Reject whole-car "
    "sale listings, news, reviews, forums, and other makes that share a model code (e.g. Genesis G70 vs BMW G70)."
)


async def triage(q: PartQuery, cc: str, cands: list[dict], keep: int) -> list[dict]:
    """LLM picks the `keep` most promising URLs; falls back to search order."""
    if len(cands) <= keep:
        return cands
    listing = "\n".join(f"{i}. {c['title'][:120]} | {c['url'][:150]} | {c['snippet'][:160]}" for i, c in enumerate(cands))
    prompt = (f"Buyer wants: {q.make} {q.model} {q.year or ''} {q.part_name}. Country searched: {cc}.\n{listing}\n\n"
              f'Return {{"keep": [indices of up to {keep} best results, best first]}}')
    try:
        out, _ = await llm.complete_json(TRIAGE_SYSTEM, prompt, max_tokens=800)
        idx = [i for i in out.get("keep", []) if isinstance(i, int) and 0 <= i < len(cands)]
        chosen = [cands[i] for i in dict.fromkeys(idx)][:keep]
        return chosen or cands[:keep]
    except (llm.LLMUnavailable, AttributeError):
        return cands[:keep]


class WebDeepSearch(SourceAdapter):
    """SearXNG (free search) -> LLM triage -> Firecrawl Local scrape (plain fetch fallback) -> LLM extract -> verify.

    If SEARXNG_URL is not set but FIRECRAWL_API_KEY is, Firecrawl Cloud /v2/search is used instead.
    """
    name = "web_deep_search"

    def enabled(self) -> bool:
        has_llm = any(p.key for p in llm.default_chain())
        has_search = bool(websearch.searxng_url()) or bool(os.getenv("FIRECRAWL_API_KEY"))
        return has_llm and has_search and os.getenv("DISABLE_DEEP_SEARCH") != "1"

    async def search(self, q: PartQuery) -> list[Offer]:
        return (await self.run(q))[0]

    async def run(self, q: PartQuery) -> tuple[list[Offer], dict]:
        key = q.model_dump_json(include={"make", "model", "year", "part_name", "oem_number"})
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1], {**hit[2], "cache": "hit"}
        t0 = time.time()
        ccs = _countries()
        per_country_pages = int(os.getenv("DEEP_SEARCH_PAGES_PER_COUNTRY", "6"))
        plan = await expand_queries(q, ccs)
        stats = {"countries": ccs, "queries": 0, "search_results": 0, "pages_selected": 0, "pages_scraped": 0,
                 "scraper": None, "extracted": 0, "kept": 0, "priced_kept": 0, "dropped_ungrounded": 0,
                 "llm_providers": {}, "per_country": {}}
        sem_search, sem_scrape, sem_llm = asyncio.Semaphore(4), asyncio.Semaphore(8), asyncio.Semaphore(5)
        offers: list[Offer] = []

        async with httpx.AsyncClient(timeout=60) as client:
            use_fc = await firecrawl.healthy(client)
            stats["scraper"] = ("firecrawl-local" if firecrawl.is_local() else "firecrawl-cloud") if use_fc else "plain-http"
            use_searx = bool(websearch.searxng_url())

            async def fetch(c: dict) -> dict | None:
                if c.get("markdown"):
                    return c
                async with sem_scrape:
                    page = await firecrawl.scrape(client, c["url"]) if use_fc else None
                    page = page or await websearch.plain_fetch(client, c["url"])
                if page:
                    page["description"] = c.get("snippet", "")
                return page

            async def one_country(cc: str) -> list[Offer]:
                cands: dict[str, dict] = {}
                queries = list(plan["queries"][cc])
                if q.oem_number:
                    queries.append(f"{q.oem_number} {q.make}")
                for query in queries:
                    async with sem_search:
                        if use_searx:
                            res = await websearch.searxng(client, query, cc)
                        else:
                            res = [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", ""),
                                    "markdown": r.get("markdown")} for r in await firecrawl.search(client, query, country=cc)]
                    stats["queries"] += 1
                    for r in res:
                        if r.get("url"):
                            cands.setdefault(r["url"].strip(), r)
                stats["search_results"] += len(cands)
                chosen = await triage(q, cc, list(cands.values()), per_country_pages)
                stats["pages_selected"] += len(chosen)
                pages = {p["url"].strip(): p for p in await asyncio.gather(*(fetch(c) for c in chosen)) if p}
                stats["pages_scraped"] += len(pages)
                if not pages:
                    return []
                plist = list(pages.values())
                batches = [plist[i:i + PAGES_PER_LLM_CALL] for i in range(0, len(plist), PAGES_PER_LLM_CALL)]

                async def extract(batch):
                    async with sem_llm:
                        try:
                            out, provider = await llm.complete_json(
                                EXTRACT_SYSTEM, _extract_prompt(q, cc, batch), client=client, max_tokens=4000)
                        except llm.LLMUnavailable as e:
                            stats.setdefault("llm_errors", []).append(str(e)[:200])
                            return []
                    stats["llm_providers"][provider] = stats["llm_providers"].get(provider, 0) + 1
                    return [(r, provider) for r in (out.get("offers", []) if isinstance(out, dict) else [])
                            if isinstance(r, dict)]

                raws = [x for b in await asyncio.gather(*(extract(b) for b in batches)) for x in b]
                reasons = stats.setdefault("drop_reasons", {})
                kept = [o for r, provider in raws if (o := verify(r, pages, cc, provider, plan, q.part_name, reasons))]
                stats["extracted"] += len(raws)
                stats["dropped_ungrounded"] += len(raws) - len(kept)
                stats["per_country"][cc] = {"candidates": len(cands), "scraped": len(pages), "kept": len(kept)}
                return kept

            for res in await asyncio.gather(*(one_country(c) for c in ccs), return_exceptions=True):
                if isinstance(res, list):
                    offers.extend(res)
                else:
                    log.warning("deep search country failed: %r", res)
        stats["kept"] = len(offers)
        stats["priced_kept"] = sum(o.price is not None for o in offers)
        stats["seconds"] = round(time.time() - t0, 1)
        _cache[key] = (time.time(), offers, stats)
        return offers, stats
