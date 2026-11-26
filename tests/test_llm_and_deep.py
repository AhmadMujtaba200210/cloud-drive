import asyncio

import httpx
import pytest

from app import llm
from app.sources.web_deep_search import verify


def chain():
    return [llm.Provider("groq", "https://g.test", "K1", "m1"),
            llm.Provider("gemini", "https://ge.test", "K2", "m2"),
            llm.Provider("openrouter", "https://o.test", "K3", "m3")]


def run(handler, monkeypatch, keys=("K1", "K2", "K3")):
    for k in ("K1", "K2", "K3"):
        monkeypatch.delenv(k, raising=False)
    for k in keys:
        monkeypatch.setenv(k, "x")
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await llm.complete_json("s", "u", chain=chain(), client=c)
    return asyncio.run(go())


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_primary_used_when_healthy(monkeypatch):
    out, used = run(lambda r: ok('{"a": 1}'), monkeypatch)
    assert used == "groq" and out == {"a": 1}


def test_falls_back_on_http_error_then_bad_json(monkeypatch):
    def h(r):
        if r.url.host == "g.test":
            return httpx.Response(429, text="rate limited")
        if r.url.host == "ge.test":
            return ok("sorry, I cannot")          # not JSON -> next provider
        return ok('```json\n{"a": 3}\n```')
    out, used = run(h, monkeypatch)
    assert used == "openrouter" and out == {"a": 3}


def test_missing_key_skipped(monkeypatch):
    out, used = run(lambda r: ok('{"a": 2}'), monkeypatch, keys=("K2",))
    assert used == "gemini"


def test_all_fail_raises(monkeypatch):
    with pytest.raises(llm.LLMUnavailable):
        run(lambda r: httpx.Response(500), monkeypatch)


def test_network_error_falls_back(monkeypatch):
    def h(r):
        if r.url.host == "g.test":
            raise httpx.ConnectTimeout("t")
        return ok('{"a": 5}')
    assert run(h, monkeypatch)[1] == "gemini"


# ---------- grounding / anti-hallucination ----------
PAGE = {"url": "https://shop.example/ae/bmw-wheel", "title": "Deira Spares",
        "markdown": "Deira Spares LLC - BMW i7 G70 steering wheel, used. Price: AED 3,250. Call +971 4 555 1234. Dubai."}
PAGES = {PAGE["url"]: PAGE}
EST = {"est_weight_kg": 3.5, "est_dims_cm": [45, 45, 20]}
GOOD = {"source_url": PAGE["url"], "vendor_name": "Deira Spares LLC", "city": "Dubai", "country_code": "AE",
        "price": 3250, "currency": "AED", "price_text": "AED 3,250", "phone": "+971 4 555 1234",
        "condition": "used", "authenticity": "oem", "title": "BMW i7 steering wheel"}


def test_grounded_offer_kept():
    o = verify(GOOD, PAGES, "AE", "groq", EST)
    assert o.price == 3250 and o.currency == "AED" and o.vendor.phone and o.evidence == "AED 3,250"
    assert o.weight_kg == 4.0 and o.dims_cm == (50, 50, 25) and o.weight_is_estimate   # floored by min_pack


def test_unknown_url_dropped():
    assert verify({**GOOD, "source_url": "https://invented.example"}, PAGES, "AE", "groq", EST) is None


def test_invented_vendor_dropped():
    assert verify({**GOOD, "vendor_name": "Emirates Genuine Parts"}, PAGES, "AE", "groq", EST) is None


def test_invented_price_becomes_contact_only():
    o = verify({**GOOD, "price": 2900, "price_text": "AED 2,900"}, PAGES, "AE", "groq", EST)
    assert o is not None and o.price is None
    o = verify({**GOOD, "price": 9999, "price_text": "AED 3,250"}, PAGES, "AE", "groq", EST)
    assert o.price is None           # price digits not in the quoted text


def test_invented_phone_stripped():
    o = verify({**GOOD, "phone": "+971 50 999 8888"}, PAGES, "AE", "groq", EST)
    assert o.vendor.phone is None and o.price == 3250


def test_unknown_currency_and_country():
    o = verify({**GOOD, "currency": "XYZ", "country_code": "ZZ"}, PAGES, "AE", "groq", EST)
    assert o.price is None and o.vendor.country == "AE"


# ---------- country & relevance (bugs found in live run) ----------
from app.sources.web_deep_search import resolve_country  # noqa: E402


def test_foreign_com_shop_not_marked_local():
    page = {"url": "https://carismodesign.com/bmw", "title": "Carismo", "markdown": "Carismo BMW i7 steering wheel $1,150.00"}
    raw = {"source_url": page["url"], "vendor_name": "Carismo", "country_code": "PK", "price": 1150, "currency": "USD",
           "price_text": "$1,150.00", "title": "steering wheel", "is_requested_part": True}
    o = verify(raw, {page["url"]: page}, "PK", "groq", EST, "steering wheel")
    # no evidence of location -> price withheld (no fake 'local, zero duty' total)
    assert o is not None and o.price is None and o.vendor.location_evidence == "unverified" and o.vendor.country == "XX"
    # a verbatim address quote on the page is accepted as evidence
    page2 = {**page, "markdown": page["markdown"] + " Warehouse: 12 Main St, Sydney NSW, Australia"}
    o2 = verify({**raw, "country_code": "US", "location_quote": "12 Main St, Sydney NSW"}, {page["url"]: page2}, "PK", "groq", EST, "steering wheel")
    assert o2.price is None or o2.vendor.country != "PK"


def test_accessory_dropped():
    raw = {**GOOD, "title": "Steering wheel cover hand-stitched", "item_sold": "steering wheel cover"}
    assert verify(raw, PAGES, "AE", "groq", EST, "steering wheel") is None
    assert verify({**GOOD, "is_requested_part": False}, PAGES, "AE", "groq", EST, "steering wheel") is None
    # but a cover is fine if the buyer asked for a cover
    assert verify(raw, PAGES, "AE", "groq", EST, "steering wheel cover") is not None


def test_country_evidence():
    assert resolve_country("JP", "https://www.goo-net.com/x", "中古 部品 東京都", None)[0] == "JP"
    assert resolve_country(None, "https://x.com", "", "+92 300 1234567") == ("PK", "phone code")
    assert resolve_country("DE", "https://x.com", "nothing", None) == (None, "unverified")


def test_weak_currency_evidence_never_makes_local():
    assert resolve_country("US", "https://x.com", "", None, currency="USD") == ("US", "weak: currency matches country")
    assert resolve_country("PK", "https://x.com", "", None, currency="PKR") == (None, "unverified")
    assert resolve_country("JP", "https://x.com", "", None, currency="USD") == (None, "unverified")


def test_condense_keeps_listing_and_footer():
    from app.sources.web_deep_search import condense
    md = "\n".join(["[Home](https://x.com/) [Menu](https://x.com/m)"] * 400
                   + ["BMW i7 Steering Wheel $749.55"] + ["nav link"] * 400
                   + ["Contact us - 2500 Troy Ave, South El Monte, CA 91733", "Call 1-888-580-1680"])
    out = condense(md, ["BMW", "i7", "steering", "wheel"])
    assert "$749.55" in out and "Troy Ave" in out and "888-580-1680" in out and len(out) <= 3600


def test_toyota_is_not_a_toy_and_encoded_urls_match():
    url = "https://www.sbp.com/product/%E0%B9%82%E0%B8%84%E0%B8%A1-altis/"
    page = {"url": url, "title": "Sehgal Motors", "markdown": "Sehgal Motors Karachi Toyota Corolla headlight Rs.48,000 +92 21 1234567"}
    raw = {"source_url": "https://sbp.com/product/โคม-altis", "vendor_name": "Sehgal Motors", "price": 48000,
           "currency": "PKR", "price_text": "Rs.48,000", "phone": "+92 21 1234567",
           "title": "Toyota Corolla OEM headlights", "item_sold": "Toyota head lamps", "is_requested_part": True}
    o = verify(raw, {url: page}, "PK", "g", EST, "headlight")
    assert o is not None and o.price == 48000 and o.vendor.country == "PK"
    assert verify({**raw, "item_sold": "toy car model"}, {url: page}, "PK", "g", EST, "headlight") is None


def test_rate_limited_provider_cools_down(monkeypatch):
    llm._cooldown_until.clear()
    calls = []
    def h(r):
        calls.append(r.url.host)
        return httpx.Response(429, headers={"retry-after": "30"}) if r.url.host == "g.test" else ok('{"a": 1}')
    run(h, monkeypatch); run(h, monkeypatch)
    assert calls.count("g.test") == 1          # second search skips the cooling provider
    llm._cooldown_until.clear()


def test_vendor_name_fuzzy_match():
    page = {"url": "https://t.com/p", "title": "", "markdown": "Sold by ToyotaPartsDeal.com, South El Monte, CA. $752.50"}
    raw = {"source_url": page["url"], "vendor_name": "Toyota Parts Deal", "title": "headlight", "is_requested_part": True}
    assert verify(raw, {page["url"]: page}, "US", "g", EST, "headlight") is not None


def test_parse_amount_formats():
    from app.sources.web_deep_search import parse_amount
    cases = {"Rs.48,000": 48000, "5.280₺": 5280, "$749.55": 749.55, "1.234,56 €": 1234.56, "AED 3,250": 3250,
             "8,000.00 บาท": 8000, "¥116,000": 116000, "12,5 EUR": 12.5, "1.234.567 KRW": 1234567, "no price": None}
    for txt, want in cases.items():
        assert parse_amount(txt) == want, txt


def test_misread_decimal_rejected():
    page = {"url": "https://b.com.tr/p", "title": "Bayramoto", "markdown": "Bayramoto Istanbul far Toyota Corolla 5.280₺"}
    raw = {"source_url": page["url"], "vendor_name": "Bayramoto", "price": 5.28, "currency": "TRY", "price_text": "5.280₺",
           "country_code": "TR", "title": "headlight", "is_requested_part": True}
    assert verify(raw, {page["url"]: page}, "TR", "g", EST, "headlight").price is None
    assert verify({**raw, "price": 5280}, {page["url"]: page}, "TR", "g", EST, "headlight").price == 5280


def test_gemini_only_lite_models(monkeypatch):
    names = {p.name: p.model for p in llm.default_chain()}
    assert all("flash-lite" in m for n, m in names.items() if n.startswith("gemini"))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
    with pytest.raises(ValueError):
        llm.default_chain()


def test_gemini_is_last_in_chain():
    names = [p.name for p in llm.default_chain()]
    assert names == ["groq", "groq-20b", "openrouter", "openrouter-free", "gemini-lite", "gemini-lite-2"]


def test_token_budget_skips_provider_without_round_trip(monkeypatch):
    """Groq says only 500 tokens are left for 60 s: a big prompt must go straight to the next provider."""
    import time
    calls = []
    def h(r):
        calls.append(r.url.host)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}]})
    llm._tokens_left.clear()
    llm._tokens_left["groq"] = (500.0, time.time() + 60)
    out, used = run(h, monkeypatch)
    assert used == "gemini" and calls == ["ge.test"]
    llm._tokens_left.clear()


def test_402_no_credits_pauses_provider(monkeypatch):
    def h(r):
        if r.url.host == "g.test":
            return httpx.Response(402, text="Insufficient credits")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})
    run(h, monkeypatch)
    import time
    assert llm._cooldown_until["groq"] > time.time() + 1000


def test_parse_reset():
    assert llm._parse_reset("7.66s") == 7.66 and llm._parse_reset("1m2.5s") == 62.5 and llm._parse_reset("120ms") == 0.12
