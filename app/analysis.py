"""Market analysis over landed options: dispersion, outliers, import-vs-local economics, duty break-even."""
from __future__ import annotations

import math
import statistics as st
from collections import Counter, defaultdict

from .models import LandedOption

MAD_Z_CUTOFF = 3.5   # Iglewicz & Hoaglin modified z-score threshold


def _q(v: list[float]) -> dict:
    return {"n": len(v), "min": round(min(v)), "median": round(st.median(v)), "max": round(max(v))}


def flag_outliers(opts: list[LandedOption]) -> int:
    """Tag offers whose item price is anomalously LOW vs peers of the same condition (log-price modified z-score).
    Scam listings and wrong-part matches cluster there. Needs >= 5 peers."""
    groups: dict[str, list[LandedOption]] = defaultdict(list)
    for o in opts:
        if o.item_price_pkr and o.item_price_pkr > 0:
            groups[o.offer.condition.value].append(o)
    flagged = 0
    for g in groups.values():
        if len(g) < 5:
            continue
        logs = [math.log(o.item_price_pkr) for o in g]
        med = st.median(logs)
        mad = st.median(abs(x - med) for x in logs)
        if mad == 0:
            continue
        for o, x in zip(g, logs):
            z = 0.6745 * (x - med) / mad
            if z < -MAD_Z_CUTOFF:
                o.tags.append("Unusually cheap: verify")
                flagged += 1
    return flagged


def analyse(local: list[LandedOption], intl: list[LandedOption], contact: list[LandedOption]) -> dict:
    priced = [o for o in local + intl if o.total_landed_pkr is not None]
    out: dict = {"priced_offers": len(priced), "contact_only_offers": len(contact)}
    if not priced:
        return out

    by_cc: dict[str, list[float]] = defaultdict(list)
    for o in priced:
        by_cc[o.offer.vendor.country].append(o.total_landed_pkr)
    out["landed_by_country"] = {cc: _q(v) for cc, v in sorted(by_cc.items(), key=lambda kv: min(kv[1]))}
    out["landed_overall"] = _q([o.total_landed_pkr for o in priced])
    out["sources"] = dict(Counter(o.offer.vendor.source.split("/")[0] for o in priced + contact))
    out["contact_only_by_country"] = dict(Counter(o.offer.vendor.country for o in contact))

    best_local = min((o for o in local if o.total_landed_pkr is not None), key=lambda o: o.total_landed_pkr, default=None)
    best_imp = min((o for o in intl if o.total_landed_pkr is not None), key=lambda o: o.total_landed_pkr, default=None)
    if best_imp:
        item, ship, duty = best_imp.item_price_pkr, best_imp.shipping.cost_pkr, best_imp.customs.total_duties_pkr
        tot = best_imp.total_landed_pkr
        out["cheapest_import_cost_split_pct"] = {
            "item": round(100 * item / tot, 1), "shipping": round(100 * ship / tot, 1), "duties_taxes": round(100 * duty / tot, 1)}
    if best_local and best_imp:
        diff = best_local.total_landed_pkr - best_imp.total_landed_pkr
        out["import_vs_local"] = {
            "best_local_pkr": best_local.total_landed_pkr, "best_import_pkr": best_imp.total_landed_pkr,
            "import_saves_pkr": round(diff), "import_saves_pct": round(100 * diff / best_local.total_landed_pkr, 1),
            "extra_days": (best_imp.days_max or 0) - (best_local.days_max or 0),
        }
        duty = best_imp.customs.total_duties_pkr
        if duty > 0:
            # duty multiplier m at which item + ship + m*duty == best local
            m = (best_local.total_landed_pkr - item - ship) / duty
            out["duty_break_even_multiplier"] = round(m, 2)
            if m <= 0:
                note = "Import loses even with zero duty."
            elif diff > 0:
                note = f"Import stays cheaper unless actual duties exceed {m:.2f}x the estimate."
            else:
                note = f"Import would only win if actual duties were below {m:.2f}x the estimate."
            out["duty_break_even_note"] = note
    return out


def dedupe(opts: list[LandedOption]) -> list[LandedOption]:
    """Same vendor + country + item price from several sources/pages -> keep the first."""
    seen, out = set(), []
    for o in opts:
        k = (o.offer.vendor.name.strip().lower(), o.offer.vendor.country, o.item_price_pkr)
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
    return out
