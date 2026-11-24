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

    async def run(self, q: PartQuery) -> tuple[list[Offer], dict]:
        """Offers plus per-call diagnostics. Adapters with a richer pipeline override this."""
        return await self.search(q), {}
