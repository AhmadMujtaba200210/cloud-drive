"""eBay Browse API adapter (official API, no scraping). Needs EBAY_OAUTH_TOKEN (application token).

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
"""
from __future__ import annotations

import os

import httpx

from ..models import Authenticity, Condition, Offer, PartQuery, Vendor
from .base import SourceAdapter

URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACES = ["EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_AU"]


def _condition(raw: str | None) -> Condition:
    raw = (raw or "").lower()
    if "refurb" in raw:
        return Condition.refurbished
    return Condition.new if raw.startswith("new") else Condition.used


class EbayBrowse(SourceAdapter):
    name = "ebay_browse"

    def enabled(self) -> bool:
        return bool(os.getenv("EBAY_OAUTH_TOKEN"))

    async def search(self, q: PartQuery) -> list[Offer]:
        terms = " ".join(filter(None, [q.oem_number, q.make, q.model, str(q.year or ""), q.part_name]))
        out: list[Offer] = []
        async with httpx.AsyncClient(timeout=10) as client:
            for mp in MARKETPLACES:
                r = await client.get(URL, params={"q": terms, "limit": 25, "filter": "deliveryCountry:PK"},
                                     headers={"Authorization": f"Bearer {os.environ['EBAY_OAUTH_TOKEN']}",
                                              "X-EBAY-C-MARKETPLACE-ID": mp})
                if r.status_code != 200:
                    continue
                for it in r.json().get("itemSummaries", []):
                    seller = it.get("seller", {})
                    loc = it.get("itemLocation", {})
                    pct = float(seller.get("feedbackPercentage") or 0)
                    vendor = Vendor(
                        id=f"ebay:{seller.get('username')}", name=seller.get("username", "eBay seller"),
                        country=loc.get("country", "US"), region=loc.get("city") or loc.get("stateOrProvince") or "-",
                        website=it.get("itemWebUrl"), rating=round(pct / 20, 2) if pct else None,
                        review_count=int(seller.get("feedbackScore") or 0), source=self.name,
                    )
                    price = it.get("price", {})
                    out.append(Offer(
                        vendor=vendor, title=it.get("title", ""), condition=_condition(it.get("condition")),
                        authenticity=Authenticity.oem if "genuine" in it.get("title", "").lower() or "oem" in it.get("title", "").lower() else Authenticity.aftermarket,
                        price=float(price["value"]) if price.get("value") else None, currency=price.get("currency", "USD"),
                        url=it.get("itemWebUrl"),
                    ))
        return out
