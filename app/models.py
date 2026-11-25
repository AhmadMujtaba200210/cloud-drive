"""Domain models for the PartsBridge PK search pipeline."""
from __future__ import annotations

from enum import Enum
from typing import Optional

import re

from pydantic import BaseModel, Field, field_validator


class Condition(str, Enum):
    new = "new"
    used = "used"
    refurbished = "refurbished"


class Authenticity(str, Enum):
    oem = "oem"                # genuine manufacturer part
    oes = "oes"                # original-equipment supplier, unbranded
    aftermarket = "aftermarket"


class SearchScope(str, Enum):
    local = "local"            # Pakistani sellers only: PK websites, marketplaces, social media shops
    worldwide = "worldwide"    # also compare imports from 12 countries, with shipping + customs


class SortMode(str, Enum):
    cheapest = "cheapest"      # lowest total landed cost in PKR
    fastest = "fastest"        # shortest door-to-door days
    top_rated = "top_rated"    # highest vendor rating (reviews-weighted)
    best_value = "best_value"  # blended cost / speed / trust score


_CTRL = re.compile(r"[\x00-\x1f\x7f<>{}`]")


class PartQuery(BaseModel):
    # Length caps + character cleanup: these fields go into LLM prompts and search queries.
    make: str = Field(..., min_length=1, max_length=40, examples=["BMW"])
    model: str = Field(..., min_length=1, max_length=60, examples=["i7"])
    year: Optional[int] = Field(None, ge=1950, le=2030, examples=[2024])
    part_name: str = Field(..., min_length=2, max_length=100, examples=["steering wheel"])
    oem_number: Optional[str] = Field(None, max_length=40, description="Manufacturer part number if known")
    vin: Optional[str] = Field(None, max_length=17, pattern=r"^[A-HJ-NPR-Za-hj-npr-z0-9]{11,17}$")
    customer_city: str = Field("Karachi", max_length=40, examples=["Lahore"])
    is_tax_filer: bool = Field(True, description="On FBR Active Taxpayer List (affects withholding tax)")
    conditions: list[Condition] = Field(default_factory=lambda: list(Condition), min_length=1, max_length=3)
    authenticity: list[Authenticity] = Field(default_factory=lambda: list(Authenticity), min_length=1, max_length=3)
    min_vendor_rating: float = Field(0.0, ge=0, le=5)
    sort: SortMode = SortMode.cheapest
    scope: SearchScope = SearchScope.local

    @field_validator("make", "model", "part_name", "oem_number", "customer_city", mode="before")
    @classmethod
    def _clean(cls, v):
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", _CTRL.sub(" ", v)).strip()
            return v or None
        return v


class Vendor(BaseModel):
    id: str
    name: str
    country: str               # ISO-3166 alpha-2
    region: str                # city / emirate / prefecture / state
    address: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    rating: Optional[float] = Field(None, ge=0, le=5)
    review_count: int = 0
    ships_to_pk: bool = True
    source: str = "unknown"    # which adapter produced this record
    is_demo: bool = False      # True for seed/sample data, never real
    location_evidence: Optional[str] = None  # how the country was established (domain / phone code / page text)


class Offer(BaseModel):
    vendor: Vendor
    title: str
    oem_number: Optional[str] = None
    condition: Condition
    authenticity: Authenticity
    price: Optional[float] = None       # None => "price on request, contact dealer"
    currency: str = "PKR"
    weight_kg: float = 1.0
    dims_cm: tuple[float, float, float] = (30, 30, 30)
    url: Optional[str] = None
    in_stock: bool = True
    handling_days: int = 2
    weight_is_estimate: bool = False   # True when weight/dims are an LLM estimate, not seller data
    evidence: Optional[str] = None     # verbatim price text found on the source page
    confidence: Optional[str] = None   # high | medium | low, from the code checks in web_deep_search.verify
    checks: list[str] = []             # human-readable verification notes shown to the buyer
    fits_note: Optional[str] = None    # fitment text copied from the page (models / years / engine)


class ShippingQuote(BaseModel):
    carrier: str
    service: str
    mode: str                  # express | economy | sea | local
    chargeable_kg: float
    cost_pkr: float
    transit_days_min: int
    transit_days_max: int
    carrier_phone: Optional[str] = None
    carrier_website: Optional[str] = None
    rate_basis: str = ""       # human-readable explanation of how the rate was built
    is_estimate: bool = True
    duties_pkr: Optional[float] = None        # Pakistan duties/taxes if shipped this way
    landed_total_pkr: Optional[float] = None  # item + this shipping + duties


class DutyLine(BaseModel):
    name: str
    rate_pct: float
    base_pkr: float
    amount_pkr: float


class CustomsBreakdown(BaseModel):
    hs_code: str
    hs_description: str
    channel: str               # courier | cargo | none
    declared_value_pkr: float
    assessable_value_pkr: float
    lines: list[DutyLine]
    total_duties_pkr: float
    clearance_days_min: int
    clearance_days_max: int
    de_minimis_applied: bool = False
    rules: list[str] = []
    warnings: list[str] = []
    config_last_verified: Optional[str] = None


class LandedOption(BaseModel):
    offer: Offer
    is_local: bool
    item_price_pkr: Optional[float]
    shipping: Optional[ShippingQuote]
    all_shipping_quotes: list[ShippingQuote] = []
    customs: Optional[CustomsBreakdown]
    total_landed_pkr: Optional[float]
    days_min: Optional[int]
    days_max: Optional[int]
    value_score: Optional[float] = None
    tags: list[str] = []


class CountryGroup(BaseModel):
    country: str
    country_name: str
    regions: dict[str, list[LandedOption]]
    best_total_pkr: Optional[float]


class SearchResponse(BaseModel):
    query: PartQuery
    fx_rates_pkr: dict[str, float]
    fx_as_of: str
    local: list[LandedOption]
    international: list[CountryGroup]
    contact_only: list[LandedOption]   # dealers with no listed price
    cheapest_overall: Optional[LandedOption]
    sources_used: list[str]
    diagnostics: dict = {}
    analysis: dict = {}
    disclaimers: list[str]
