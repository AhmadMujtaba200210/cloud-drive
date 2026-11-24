"""Shipping quotes: international carriers to Pakistan, sea LCL for heavy parts, and local couriers."""
from __future__ import annotations

import math

from .config import load
from .fx import to_pkr
from .models import Offer, ShippingQuote

MAJOR_CITIES_SAME_HUB = {"karachi", "lahore", "islamabad", "rawalpindi"}


def volumetric_kg(dims_cm: tuple[float, float, float], divisor: float) -> float:
    l, w, h = dims_cm
    return l * w * h / divisor


def chargeable_kg(offer: Offer, divisor: float) -> float:
    """Express carriers bill the greater of actual and volumetric weight, rounded up to 0.5 kg."""
    raw = max(offer.weight_kg, volumetric_kg(offer.dims_cm, divisor))
    return math.ceil(raw * 2) / 2


def international_quotes(offer: Offer, customer_city: str) -> list[ShippingQuote]:
    cfg = load("shipping")
    zone = cfg["zones"].get(offer.vendor.country)
    if zone is None or not offer.vendor.ships_to_pk:
        return []
    quotes: list[ShippingQuote] = []
    ckg = chargeable_kg(offer, cfg["volumetric_divisor_express"])

    for c in cfg["international_carriers"]:
        if ckg > c["max_kg"] or zone not in c["rates_usd"]:
            continue
        first, addl = c["rates_usd"][zone]
        base_usd = first + max(0.0, ckg - 0.5) * addl   # first 0.5 kg flat, then per kg (billed in 0.5 kg steps)
        total_usd = base_usd * (1 + c["fuel_surcharge_pct"] / 100)
        dmin, dmax = c["transit_days"][zone]
        quotes.append(ShippingQuote(
            carrier=c["carrier"], service=c["service"], mode=c["mode"],
            chargeable_kg=ckg, cost_pkr=round(to_pkr(total_usd, "USD")),
            transit_days_min=dmin, transit_days_max=dmax,
            carrier_phone=c.get("phone"), carrier_website=c.get("website"),
            rate_basis=f"USD {first} first 0.5 kg + USD {addl}/kg after, +{c['fuel_surcharge_pct']}% fuel; {ckg} kg chargeable",
        ))

    sea = cfg["sea_freight"]
    if offer.weight_kg >= sea["min_kg_to_offer"] and zone in sea["usd_per_cbm"]:
        l, w, h = offer.dims_cm
        cbm = max(sea_min := cfg["sea_min_cbm"], l * w * h / 1_000_000)
        usd = sea["fixed_usd"] + cbm * sea["usd_per_cbm"][zone]
        onward = cfg["domestic_onward"]
        onward_pkr = onward["base_pkr"] + onward["per_kg_pkr"] * offer.weight_kg
        dmin, dmax = sea["transit_days"][zone]
        quotes.append(ShippingQuote(
            carrier=sea["carrier"], service=sea["service"], mode="sea",
            chargeable_kg=offer.weight_kg, cost_pkr=round(to_pkr(usd, "USD") + onward_pkr),
            transit_days_min=dmin, transit_days_max=dmax,
            rate_basis=f"USD {sea['fixed_usd']} fixed + {cbm:.2f} CBM (min {sea_min}) x USD {sea['usd_per_cbm'][zone]}, + PKR {onward_pkr:.0f} Karachi->{customer_city}",
        ))
    return sorted(quotes, key=lambda q: q.cost_pkr)


def local_quotes(offer: Offer, customer_city: str) -> list[ShippingQuote]:
    cfg = load("shipping")
    same_city = offer.vendor.region.strip().lower() == customer_city.strip().lower()
    ckg = math.ceil(max(offer.weight_kg, 0.5))
    out = []
    for c in cfg["local_carriers"]:
        days = c["same_city_days"] if same_city else c["intercity_days"]
        out.append(ShippingQuote(
            carrier=c["carrier"], service=c["service"], mode="local", chargeable_kg=ckg,
            cost_pkr=round(c["base_pkr"] + c["per_kg_pkr"] * ckg),
            transit_days_min=days[0], transit_days_max=days[1],
            carrier_phone=c.get("phone"), carrier_website=c.get("website"),
            rate_basis=f"PKR {c['base_pkr']} + PKR {c['per_kg_pkr']}/kg x {ckg} kg",
        ))
    return sorted(out, key=lambda q: q.cost_pkr)
