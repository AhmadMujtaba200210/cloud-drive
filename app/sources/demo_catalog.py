"""Seed catalog for development and demos. Every record is flagged is_demo=True and is NOT a real vendor."""
from __future__ import annotations

import json
import os

from ..config import DATA_DIR
from ..models import Offer, PartQuery, Vendor
from .base import SourceAdapter


def _matches(q: PartQuery, item: dict) -> bool:
    if q.oem_number and item.get("oem_number") and q.oem_number.replace(" ", "").lower() == item["oem_number"].replace(" ", "").lower():
        return True
    fits = item["fits"]
    if q.make.lower() != fits["make"].lower() or q.model.lower() != fits["model"].lower():
        return False
    if q.year and not (fits["year_from"] <= q.year <= fits["year_to"]):
        return False
    words = [w for w in q.part_name.lower().split() if len(w) > 2]
    return all(w in item["part_name"].lower() for w in words)


class DemoCatalog(SourceAdapter):
    name = "demo_catalog"

    def enabled(self) -> bool:
        return os.getenv("DISABLE_DEMO") != "1"

    async def search(self, q: PartQuery) -> list[Offer]:
        data = json.loads((DATA_DIR / "demo_catalog.json").read_text(encoding="utf-8"))
        vendors = {v["id"]: Vendor(**v, source=self.name, is_demo=True) for v in data["vendors"]}
        out = []
        for item in data["listings"]:
            if not _matches(q, item):
                continue
            out.append(Offer(
                vendor=vendors[item["vendor_id"]], title=item["title"], oem_number=item.get("oem_number"),
                condition=item["condition"], authenticity=item["authenticity"], price=item.get("price"),
                currency=item.get("currency", "PKR"), weight_kg=item["weight_kg"], dims_cm=tuple(item["dims_cm"]),
                in_stock=item.get("in_stock", True), handling_days=item.get("handling_days", 2),
            ))
        return out
