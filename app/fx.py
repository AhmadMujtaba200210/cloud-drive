"""Currency conversion to PKR. Static fallback table, optional live refresh (FX_LIVE=1)."""
from __future__ import annotations

import os
import time

import httpx

from .config import load

_LIVE_URL = "https://open.er-api.com/v6/latest/PKR"
_cache: dict = {"rates": None, "as_of": None, "fetched": 0.0}


def _static() -> tuple[dict[str, float], str]:
    cfg = load("fx")
    return dict(cfg["pkr_per_unit"]), cfg["as_of"]


def rates() -> tuple[dict[str, float], str]:
    """Return (PKR per 1 unit of currency, as_of label)."""
    if os.getenv("FX_LIVE") != "1":
        return _static()
    if _cache["rates"] and time.time() - _cache["fetched"] < 3600:
        return _cache["rates"], _cache["as_of"]
    try:
        r = httpx.get(_LIVE_URL, timeout=5)
        r.raise_for_status()
        body = r.json()
        # feed gives units of X per 1 PKR -> invert
        live = {cur: 1.0 / v for cur, v in body["rates"].items() if v}
        base, _ = _static()
        merged = {**base, **{k: live[k] for k in base if k in live}}
        _cache.update(rates=merged, as_of=body.get("time_last_update_utc", "live"), fetched=time.time())
        return merged, _cache["as_of"]
    except Exception:
        return _static()


def to_pkr(amount: float, currency: str) -> float:
    table, _ = rates()
    cur = currency.upper()
    if cur not in table:
        raise ValueError(f"No FX rate for {cur}; add it to config/fx.json")
    return amount * table[cur]
