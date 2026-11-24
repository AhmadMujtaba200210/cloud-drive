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
    assert o.weight_kg == 3.5 and o.weight_is_estimate


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
