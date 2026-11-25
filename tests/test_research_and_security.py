"""Live research feed, deeper research helpers, and public-server hardening."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import main, netsafe, progress
from app.main import app
from app.models import PartQuery
from app.sources.web_deep_search import looks_like_listing, product_links, verify

BMW = {"make": "BMW", "model": "i7", "year": 2024, "part_name": "steering wheel", "customer_city": "Lahore"}
EST = {"est_weight_kg": 6, "est_dims_cm": [50, 50, 25]}


@pytest.fixture(autouse=True)
def _clean_guard():
    main._hits.clear(); main._running.clear()
    yield
    main._hits.clear(); main._running.clear()


# ---------------- SSRF guard ----------------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:3002/v2/scrape", "http://localhost/", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.250:8090/", "http://192.168.1.1/", "http://[::1]/", "file:///etc/passwd", "ftp://x.com/a",
    "http://intranet/", "http://printer.local/", "http://user:pw@shop.com/", "http://shop.com:8888/",
])
def test_blocks_internal_urls(url):
    assert not netsafe.syntactically_public(url)
    assert not asyncio.run(netsafe.is_public_url(url))


def test_allows_public_urls():
    assert netsafe.syntactically_public("https://www.pakwheels.com/accessories-spare-parts/x")
    assert netsafe.syntactically_public("http://8.8.8.8/")


def test_public_name_resolving_to_private_ip_is_blocked(monkeypatch):
    async def private(host):
        return ["93.184.216.34", "10.0.0.5"]          # one public + one private answer: reject
    async def public(host):
        return ["93.184.216.34"]
    netsafe._dns_cache.clear()
    monkeypatch.setattr(netsafe, "_resolve", private)
    assert not asyncio.run(netsafe.is_public_url("https://shop-a.example.com/"))
    monkeypatch.setattr(netsafe, "_resolve", public)
    assert asyncio.run(netsafe.is_public_url("https://shop-b.example.com/"))
    netsafe._dns_cache.clear()


# ---------------- input validation ----------------
@pytest.mark.parametrize("bad", [
    {**BMW, "make": "x" * 41}, {**BMW, "part_name": "a"}, {**BMW, "year": 1800}, {**BMW, "vin": "not a vin!"},
    {**BMW, "conditions": []}, {**BMW, "make": "   "},
])
def test_rejects_bad_input(bad):
    assert TestClient(app).post("/api/search", json=bad).status_code == 422


def test_strips_control_and_markup_chars():
    q = PartQuery(**{**BMW, "part_name": "steering\x00 wheel <script>"})
    assert q.part_name == "steering wheel script"


# ---------------- headers, docs, no auto search ----------------
def test_security_headers_and_static_split():
    c = TestClient(app)
    r = c.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<script>" not in r.text and "app.js" in r.text          # no inline script under the CSP
    assert c.get("/static/app.js").status_code == 200
    assert "run();\n</script>" not in r.text                         # page no longer searches on load


def test_customs_estimate_bounds():
    c = TestClient(app)
    assert c.get("/api/customs/estimate", params={"part_name": "x" * 101, "value_pkr": 1}).status_code == 422
    assert c.get("/api/customs/estimate", params={"part_name": "headlight", "value_pkr": -5}).status_code == 422


# ---------------- streaming endpoint ----------------
def _events(text: str):
    out = []
    for chunk in text.split("\n\n"):
        ev, data = None, ""
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if ev:
            out.append((ev, json.loads(data)))
    return out


def test_stream_narrates_then_returns_result():
    r = TestClient(app).post("/api/search/stream", json=BMW)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    evs = _events(r.text)
    kinds = [e for e, _ in evs]
    assert kinds[0] == "step" and kinds[-1] == "result"
    assert any(d.get("kind") == "cost" for e, d in evs if e == "step")
    assert evs[-1][1]["cheapest_overall"]


def test_stream_validates_and_limits(monkeypatch):
    c = TestClient(app)
    assert c.post("/api/search/stream", json={"make": "BMW"}).status_code == 422
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "1")
    assert c.post("/api/search/stream", json=BMW).status_code == 200
    assert c.post("/api/search/stream", json=BMW).status_code == 429


def test_one_running_search_per_ip():
    main._running.add("testclient")
    r = TestClient(app).post("/api/search", json=BMW)
    assert r.status_code == 429 and "already have a search running" in r.json()["detail"]


def test_cached_searches_are_free(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "1")
    monkeypatch.setattr(main.aggregator, "is_cached", lambda q: True)
    c = TestClient(app)
    for _ in range(3):
        assert c.post("/api/search", json=BMW).status_code == 200


def test_progress_is_noop_without_listener():
    progress.say("think", "nobody listening")        # must not raise


def test_progress_reaches_child_tasks():
    async def go():
        q = asyncio.Queue()
        progress.bind(q)
        await asyncio.gather(asyncio.create_task(asyncio.sleep(0)), _child())
        return [q.get_nowait()[1]["text"] for _ in range(q.qsize())]
    async def _child():
        progress.say("read", "from a child task")
    assert asyncio.run(go()) == ["from a child task"]


# ---------------- deeper research ----------------
LISTING = {"url": "https://www.pakwheels.com/accessories-spare-parts/search/-/?q=corolla+air+filter",
           "markdown": "Rs 1,200\nRs 1,500\nRs 2,100\nRs 900\n",
           "links": ["https://www.pakwheels.com/accessories-spare-parts/toyota-corolla-2017-air-filters/2501421",
                     "https://www.pakwheels.com/login", "https://evil.example.com/toyota-corolla-air-filter",
                     "https://www.pakwheels.com/accessories-spare-parts/honda-civic-floor-mats/77",
                     "https://www.pakwheels.com/accessories-spare-parts/corolla-air-filter-genuine/99#reviews",
                     "http://127.0.0.1:3002/corolla-air-filter"]}


def test_follows_only_same_site_product_links():
    q = PartQuery(make="Toyota", model="Corolla", year=2017, part_name="air filter")
    assert looks_like_listing(LISTING)
    links = product_links(LISTING, q, 5)
    assert links[0].endswith("2501421") or links[0].endswith("/99")
    assert all("pakwheels.com" in u and "login" not in u and "#" not in u for u in links)
    assert len(links) == 2


def test_confidence_and_checks():
    url = "https://autoparts.ae/corolla-air-filter"
    page = {"url": url, "title": "Toyota Corolla air filter 17801-0T030",
            "markdown": "Gulf Auto Parts\nToyota Corolla 2014-2019 air filter 17801-0T030\nAED 45.00\nTel +971 4 555 1234"}
    raw = {"source_url": url, "vendor_name": "Gulf Auto Parts", "price": 45, "currency": "AED", "price_text": "AED 45.00",
           "country_code": "AE", "phone": "+971 4 555 1234", "title": "Corolla air filter", "item_sold": "air filter",
           "is_requested_part": True, "oem_numbers": ["17801-0T030", "99999-INVENTED"], "fits_note": "Toyota Corolla 2014-2019"}
    o = verify(raw, {url: page}, "AE", "g", EST, "air filter", model="Corolla")
    assert o.confidence == "high" and o.oem_number == "17801-0T030" and o.fits_note
    assert any("printed on the page" in c for c in o.checks)
    o2 = verify(raw, {url: page}, "AE", "g", EST, "air filter", model="Corolla", oem_number="12345-WRONG")
    assert o2.confidence != "high" and any("NOT on this page" in c for c in o2.checks)


# ---------------- Pakistan-only mode ----------------
def test_local_scope_is_default_and_keeps_only_pakistani_sellers():
    from app.aggregator import search
    from app.sources.demo_catalog import DemoCatalog
    q = PartQuery(**BMW)
    assert q.scope.value == "local"
    r = asyncio.run(search(q, [DemoCatalog()]))
    assert r.international == []
    assert all(o.offer.vendor.country in ("PK", "XX") for o in r.local + r.contact_only)
    w = asyncio.run(search(PartQuery(**BMW, scope="worldwide"), [DemoCatalog()]))
    assert w.international


def test_cache_key_separates_scopes():
    from app.sources.web_deep_search import WebDeepSearch
    k = WebDeepSearch.cache_key
    assert k(PartQuery(**BMW)) != k(PartQuery(**BMW, scope="worldwide"))


def test_social_results_only_in_local_mode():
    from app import websearch
    assert websearch._skip("https://www.facebook.com/groups/carparts/posts/1")
    assert not websearch._skip("https://www.facebook.com/groups/carparts/posts/1", allow_social=True)
    assert websearch._skip("https://www.youtube.com/watch?v=1", allow_social=True)


def test_snippet_only_social_post_is_grounded_in_snippet():
    url = "https://www.facebook.com/karachiautoparts/posts/123"
    page = {"url": url, "title": "Karachi Auto Parts", "snippet_only": True,
            "markdown": "Karachi Auto Parts\nCorolla 2017 air filter genuine Rs 2,500 Karachi, call 0300 1234567"}
    raw = {"source_url": url, "vendor_name": "Karachi Auto Parts", "price": 2500, "currency": "PKR",
           "price_text": "Rs 2,500", "country_code": "PK", "phone": "0300 1234567", "title": "Corolla air filter",
           "item_sold": "air filter", "is_requested_part": True}
    o = verify(raw, {url: page}, "PK", "g", EST, "air filter", model="Corolla")
    assert o.vendor.country == "PK" and o.price == 2500
    assert any("social media post" in c for c in o.checks)


# ---------------- knowledge base (Oracle) ----------------
from app import kb as kbmod  # noqa: E402


@pytest.mark.parametrize("a,b,same", [
    ("brake pads", "Brake Pad Set", True), ("Genuine front brake pads", "front brake pad", True),
    ("front brake pads", "rear brake pads", False), ("brake pads", "front brake pads", False),
    ("radiator", "radiator fan", False), ("head lamp", "Headlights", True), ("left headlight", "LH headlight", True),
    ("left headlight", "right headlight", False), ("air filter", "cabin air filter", False), ("disc pads", "brake pads", True),
])
def test_part_validator(a, b, same):
    assert kbmod.same_part(a, b) is same


def test_query_key_ignores_plural_and_case_but_not_scope():
    k = kbmod.query_key
    assert k("Honda", "Civic", 2018, "Brake Pads", "local") == k("honda", "civic ", 2018, "brake pad set", "local")
    assert k("Honda", "Civic", 2018, "brake pads", "local") != k("Honda", "Civic", 2018, "brake pads", "worldwide")
    assert k("Honda", "Civic", 2018, "brake pads", "local") != k("Honda", "Civic", 2019, "brake pads", "local")


def test_kb_is_off_without_credentials(monkeypatch):
    for v in ("ORACLE_DB_USER", "ORACLE_DB_PASSWORD", "ORACLE_DB_DSN"):
        monkeypatch.delenv(v, raising=False)
    assert not kbmod.KB.configured()
    assert asyncio.run(kbmod.KB().get_plan("x")) is None          # fails soft, no exception


def test_kb_result_reuse_skips_research(monkeypatch):
    from app.sources import web_deep_search as wds
    from app.models import Offer
    stored = verify({"source_url": "https://autostore.pk/x", "vendor_name": "Autostore", "price": 22000, "currency": "PKR",
                     "price_text": "Rs 22,000", "country_code": "PK", "title": "Civic pads", "item_sold": "brake pads",
                     "is_requested_part": True},
                    {"https://autostore.pk/x": {"url": "https://autostore.pk/x", "markdown": "Autostore Civic brake pads Rs 22,000"}},
                    "PK", "g", EST, "brake pads")
    calls = {"research": 0}

    async def fake_find(*a):
        return [stored.model_dump(mode="json")], {"kept": 1}, "brake pad set", 3600.0

    async def boom(self, q):
        calls["research"] += 1
        raise AssertionError("must not research")
    monkeypatch.setattr(wds.KB, "configured", lambda: True)
    monkeypatch.setattr(wds.KB, "find_result", fake_find)
    monkeypatch.setattr(wds.WebDeepSearch, "_research", boom)
    wds._cache.clear()
    offers, stats = asyncio.run(wds.WebDeepSearch().run(PartQuery(make="Honda", model="Civic", year=2018, part_name="brake pads")))
    assert calls["research"] == 0 and stats["cache"] == "knowledge base"
    assert isinstance(offers[0], Offer) and offers[0].price == 22000
    wds._cache.clear()
