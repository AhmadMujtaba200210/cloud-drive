"""Google Places (New) Text Search adapter: finds parts DEALERS (name, address, phone, rating) when no price is listed.

Results are contact-only offers (price=None). Needs GOOGLE_PLACES_API_KEY.
Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""
from __future__ import annotations

import os

import httpx

from ..models import Authenticity, Condition, Offer, PartQuery, Vendor
from .base import SourceAdapter

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.internationalPhoneNumber",
    "places.websiteUri", "places.rating", "places.userRatingCount", "places.addressComponents",
])

# City list per country to sweep. Pakistan first (local), then main parts hubs abroad.
HUBS = {
    "PK": ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Peshawar", "Faisalabad", "Multan"],
    "AE": ["Dubai", "Sharjah", "Abu Dhabi"],
    "JP": ["Tokyo", "Osaka", "Nagoya", "Yokohama"],
    "DE": ["Munich", "Berlin", "Frankfurt"],
    "GB": ["London", "Birmingham"],
    "US": ["Los Angeles", "Houston", "New York"],
    "TH": ["Bangkok"], "MY": ["Kuala Lumpur"], "SA": ["Riyadh", "Jeddah"], "CN": ["Guangzhou", "Shanghai"],
}


class GooglePlaces(SourceAdapter):
    name = "google_places"

    def enabled(self) -> bool:
        return bool(os.getenv("GOOGLE_PLACES_API_KEY"))

    async def search(self, q: PartQuery) -> list[Offer]:
        out: list[Offer] = []
        headers = {"X-Goog-Api-Key": os.environ["GOOGLE_PLACES_API_KEY"], "X-Goog-FieldMask": FIELDS}
        async with httpx.AsyncClient(timeout=10) as client:
            for country, cities in HUBS.items():
                for city in cities:
                    body = {"textQuery": f"{q.make} {q.part_name} auto parts dealer in {city}", "pageSize": 10}
                    r = await client.post(URL, json=body, headers=headers)
                    if r.status_code != 200:
                        continue
                    for p in r.json().get("places", []):
                        v = Vendor(
                            id=f"gplaces:{p['id']}", name=p.get("displayName", {}).get("text", "Dealer"),
                            country=country, region=city, address=p.get("formattedAddress"),
                            phone=p.get("internationalPhoneNumber"), website=p.get("websiteUri"),
                            rating=p.get("rating"), review_count=p.get("userRatingCount", 0), source=self.name,
                        )
                        out.append(Offer(vendor=v, title=f"{q.make} {q.model} {q.part_name} - contact dealer for price",
                                         condition=Condition.new, authenticity=Authenticity.aftermarket, price=None))
        return out
