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
