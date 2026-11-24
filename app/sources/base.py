"""Source adapter interface. Each adapter turns a PartQuery into Offers (priced listings or contact-only dealers)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Offer, PartQuery


class SourceAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def search(self, q: PartQuery) -> list[Offer]: ...
