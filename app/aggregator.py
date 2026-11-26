"""Search pipeline: adapters -> offers -> landed cost in PKR -> filter -> rank -> group by country/region."""
from __future__ import annotations

import asyncio
from collections import defaultdict

from . import analysis, customs, fx, progress, shipping
from .config import countries
from .models import (CountryGroup, LandedOption, Offer, PartQuery, SearchResponse, SearchScope, ShippingQuote,
                     SortMode)
from .sources.base import SourceAdapter
from .sources.demo_catalog import DemoCatalog
from .sources.ebay import EbayBrowse
from .sources.google_places import GooglePlaces
from .sources.web_deep_search import WebDeepSearch

ALL_ADAPTERS: list[SourceAdapter] = [DemoCatalog(), EbayBrowse(), GooglePlaces(), WebDeepSearch()]
CONF_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def is_cached(q: PartQuery) -> bool:
    """True if the expensive deep search would be served from cache (so it costs no LLM quota)."""
    deep = [a for a in ALL_ADAPTERS if isinstance(a, WebDeepSearch) and a.enabled()]
    return bool(deep) and all(a.is_cached(q) for a in deep)

# Bayesian-average prior so a 5.0 from 3 reviews does not beat a 4.7 from 2,000.
PRIOR_RATING, PRIOR_WEIGHT = 3.5, 20


def trust_score(rating: float | None, reviews: int) -> float:
    r = rating if rating is not None else PRIOR_RATING
    return (PRIOR_WEIGHT * PRIOR_RATING + r * reviews) / (PRIOR_WEIGHT + reviews)


def _passes(q: PartQuery, o: Offer) -> bool:
    # Pakistan-only mode: Pakistani sellers, plus leads whose location is unproven (shown as contact-only).
    if q.scope == SearchScope.local and o.vendor.country not in ("PK", "XX"):
        return False
    if o.condition not in q.conditions or o.authenticity not in q.authenticity:
        return False
    if q.min_vendor_rating and (o.vendor.rating or 0) < q.min_vendor_rating:
        return False
    return o.in_stock


def build_option(q: PartQuery, o: Offer) -> LandedOption:
    is_local = o.vendor.country == "PK"
    price_pkr = round(fx.to_pkr(o.price, o.currency)) if o.price is not None else None

    if is_local:
        quotes = shipping.local_quotes(o, q.customer_city)
        for s in quotes:
            s.duties_pkr = 0
            s.landed_total_pkr = None if price_pkr is None else price_pkr + s.cost_pkr
        best = quotes[0] if quotes else None
        return LandedOption(
            offer=o, is_local=True, item_price_pkr=price_pkr, shipping=best, all_shipping_quotes=quotes, customs=None,
            total_landed_pkr=best.landed_total_pkr if best else price_pkr,
            days_min=o.handling_days + (best.transit_days_min if best else 0),
            days_max=o.handling_days + (best.transit_days_max if best else 0),
        )

    quotes = shipping.international_quotes(o, q.customer_city)
    per_quote: list[tuple[ShippingQuote, object]] = []
    for s in quotes:
        channel = "cargo" if s.mode == "sea" else "courier"
        cb = customs.estimate(q.part_name, price_pkr or 0, s.cost_pkr, channel, q.is_tax_filer, o.condition.value)
        if price_pkr is not None:
            s.duties_pkr = cb.total_duties_pkr
            s.landed_total_pkr = price_pkr + s.cost_pkr + cb.total_duties_pkr
        per_quote.append((s, cb))

    if not per_quote:
        reason = ("Location unverified: ask seller" if o.vendor.country == "XX" else
                  "Does not ship to Pakistan" if not o.vendor.ships_to_pk else "No carrier rate for this origin")
        return LandedOption(offer=o, is_local=False, item_price_pkr=price_pkr, shipping=None, customs=None,
                            total_landed_pkr=None, days_min=None, days_max=None, tags=[reason])

    def door_days(pair):
        s, cb = pair
        return o.handling_days + s.transit_days_max + cb.clearance_days_max

    if q.sort == SortMode.fastest:
        chosen = min(per_quote, key=door_days)
    else:
        chosen = min(per_quote, key=lambda p: p[0].landed_total_pkr if p[0].landed_total_pkr is not None else p[0].cost_pkr)
    s, cb = chosen
    return LandedOption(
        offer=o, is_local=False, item_price_pkr=price_pkr, shipping=s,
        all_shipping_quotes=[p[0] for p in per_quote], customs=cb, total_landed_pkr=s.landed_total_pkr,
        days_min=o.handling_days + s.transit_days_min + cb.clearance_days_min,
        days_max=door_days(chosen),
    )


def _score_and_tag(opts: list[LandedOption]) -> None:
    priced = [x for x in opts if x.total_landed_pkr is not None]
    if not priced:
        return
    lo = min(x.total_landed_pkr for x in priced); hi = max(x.total_landed_pkr for x in priced)
    dlo = min(x.days_max for x in priced); dhi = max(x.days_max for x in priced)
    for x in priced:
        cost = 1 - (x.total_landed_pkr - lo) / (hi - lo) if hi > lo else 1
        speed = 1 - (x.days_max - dlo) / (dhi - dlo) if dhi > dlo else 1
        trust = (trust_score(x.offer.vendor.rating, x.offer.vendor.review_count) - 1) / 4
        x.value_score = round(0.5 * cost + 0.2 * speed + 0.3 * trust, 4)
    min(priced, key=lambda x: x.total_landed_pkr).tags.append("Cheapest")
    min(priced, key=lambda x: x.days_max).tags.append("Fastest")
    max(priced, key=lambda x: trust_score(x.offer.vendor.rating, x.offer.vendor.review_count)).tags.append("Top rated")
    max(priced, key=lambda x: x.value_score).tags.append("Best value")
    for x in opts:
        if x.offer.authenticity.value == "oem":
            x.tags.append("Genuine OEM")


def _sort_key(mode: SortMode):
    if mode == SortMode.fastest:
        return lambda x: (x.days_max, x.total_landed_pkr)
    if mode == SortMode.top_rated:
        return lambda x: (-trust_score(x.offer.vendor.rating, x.offer.vendor.review_count), x.total_landed_pkr)
    if mode == SortMode.best_value:
        return lambda x: (-(x.value_score or 0), x.total_landed_pkr)
    return lambda x: (x.total_landed_pkr, x.days_max)


async def search(q: PartQuery, adapters: list[SourceAdapter] | None = None) -> SearchResponse:
    adapters = [a for a in (adapters or ALL_ADAPTERS) if a.enabled()]
    # Early results for the live feed: same landed-cost pipeline, run on the offers found so far.
    progress.set_partial_builder(lambda found: assemble(q, list(found), {"web_deep_search": {"partial": True}},
                                                        [a.name for a in adapters], narrate=False).model_dump(mode="json"))
    results = await asyncio.gather(*(a.run(q) for a in adapters), return_exceptions=True)
    offers, diagnostics = [], {}
    for a, r in zip(adapters, results):
        if isinstance(r, Exception):
            diagnostics[a.name] = {"error": type(r).__name__}      # no internal detail in the public response
            progress.say("warn", f"One source ({a.name}) failed; continuing with the others.")
            continue
        offers.extend(r[0])
        diagnostics[a.name] = {"offers": len(r[0]), **r[1]}
    return assemble(q, offers, diagnostics, [a.name for a in adapters])


def assemble(q: PartQuery, offers: list[Offer], diagnostics: dict, sources: list[str],
             narrate: bool = True) -> SearchResponse:
    passing = [o for o in offers if _passes(q, o)]
    if passing and narrate:
        progress.say("cost", f"Working out shipping and Pakistan customs to {q.customer_city} for {len(passing)} offers "
                             f"(carrier rates, HS code, duties, sales tax).")
    passing.sort(key=lambda o: CONF_RANK.get(o.confidence, 3))     # dedupe keeps the best-evidenced copy
    # Only listings of the requested part itself are ranked; a headlight GLASS must never be the cheapest headlight.
    related = analysis.dedupe([build_option(q, o) for o in passing if o.match_kind != "exact"])
    related.sort(key=lambda x: (x.total_landed_pkr is None, x.total_landed_pkr or 0))
    passing = [o for o in passing if o.match_kind == "exact"]
    opts = analysis.dedupe([build_option(q, o) for o in passing])
    _score_and_tag(opts)
    analysis.flag_outliers(opts)

    priced = [x for x in opts if x.total_landed_pkr is not None]
    contact_only = [x for x in opts if x.item_price_pkr is None]
    contact_only.sort(key=lambda x: (not x.is_local, -trust_score(x.offer.vendor.rating, x.offer.vendor.review_count)))
    key = _sort_key(q.sort)

    local = sorted([x for x in priced if x.is_local], key=key)
    by_country: dict[str, dict[str, list[LandedOption]]] = defaultdict(lambda: defaultdict(list))
    for x in priced:
        if not x.is_local:
            by_country[x.offer.vendor.country][x.offer.vendor.region].append(x)
    names = countries()
    groups = []
    for cc, regions in by_country.items():
        regs = {r: sorted(v, key=key) for r, v in sorted(regions.items())}
        best = min(o.total_landed_pkr for v in regs.values() for o in v)
        groups.append(CountryGroup(country=cc, country_name=names.get(cc, cc), regions=regs, best_total_pkr=best))
    groups.sort(key=lambda g: g.best_total_pkr)

    rates, as_of = fx.rates()
    return SearchResponse(
        query=q, fx_rates_pkr=rates, fx_as_of=as_of, local=local, international=groups, contact_only=contact_only,
        related=related,
        cheapest_overall=min(priced, key=lambda x: x.total_landed_pkr) if priced else None,
        sources_used=sources, diagnostics=diagnostics,
        analysis=analysis.analyse([x for x in priced if x.is_local], [x for x in priced if not x.is_local], contact_only),
        disclaimers=[
            "Shipping rates are illustrative rate cards until carrier APIs are connected.",
            "Customs duties are estimates from configurable placeholder rates; final duty is assessed by Pakistan Customs.",
            "Records marked [DEMO] are fictional sample data.",
        ],
    )
