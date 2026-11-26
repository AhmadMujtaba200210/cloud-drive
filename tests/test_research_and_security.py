"""Live research feed, deeper research helpers, and public-server hardening."""
import asyncio
import time
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
    main._running["testclient"] = "some-other-search"
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
    age = {"s": 600.0}                                    # 10 min old: reused even though it has 1 price

    async def fake_find(*a):
        return [stored.model_dump(mode="json")], {"kept": 1}, "brake pad set", age["s"]

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


# ---------------- social toggle, PK phones, price hunt ----------------
def test_social_toggle_changes_cache_keys_and_defaults_on():
    from app.sources.web_deep_search import WebDeepSearch, _scope_tag
    on, off = PartQuery(**BMW), PartQuery(**BMW, include_social=False)
    assert on.include_social is True
    assert WebDeepSearch.cache_key(on) != WebDeepSearch.cache_key(off)
    assert _scope_tag(on).startswith("local+social|") and _scope_tag(off).startswith("local|")


def test_pakistani_local_mobile_number_proves_pk():
    from app.sources.web_deep_search import _phone_cc
    assert _phone_cc("0300 1234567") == "PK" and _phone_cc("0321-7654321") == "PK"
    assert _phone_cc("+971 50 123 4567") == "AE"
    assert _phone_cc("042 111 222 333") is None          # landline / UAN: not enough on its own


def test_price_hunt_stops_by_record_value_rule(monkeypatch):
    """With many verified prices and a low-yield last round, m/(n+m) < 12% -> no further round."""
    from app.sources import web_deep_search as wds
    calls = []

    async def research(cc, qs, label, pick, follow, paid=0):
        calls.append(qs)
        return []
    mk = lambda p: verify({"source_url": f"https://s{p}.pk/x", "vendor_name": f"Shop{p}", "price": p, "currency": "PKR",  # noqa: E731
                           "price_text": f"Rs {p}", "country_code": "PK", "title": "air filter", "item_sold": "air filter",
                           "is_requested_part": True},
                          {f"https://s{p}.pk/x": {"url": f"https://s{p}.pk/x", "markdown": f"Shop{p} air filter Rs {p}"}},
                          "PK", "g", EST, "air filter")
    offers = [mk(p) for p in range(1000, 1030)]            # n = 30, first-round m = max(3, 15) = 15 -> 33%: hunt
    stats = {}
    q = PartQuery(make="Toyota", model="Corolla", part_name="air filter")
    asyncio.run(wds.WebDeepSearch()._hunt(q, offers, research, time.time() + 300, stats, True))
    # round 1 yields 0 new prices -> m = 0 -> p = 0% < 12% -> stop after exactly one round
    assert len(calls) == 1 and stats["hunt"]["rounds"] == 1
    assert stats["hunt"]["start_min_pkr"] == 1000


@pytest.mark.parametrize("url,post", [
    ("https://www.facebook.com/marketplace/item/2541595412345678/", True),
    ("https://www.facebook.com/goldmineautos/posts/suzuki-mehran-headlight-123", True),
    ("https://www.facebook.com/groups/carpartspk/posts/987654321/", True),
    ("https://www.instagram.com/reel/DVAjyKSDXK6/", True), ("https://www.instagram.com/p/B5XpSyCnJHy/", True),
    ("https://www.tiktok.com/@mbh_autos/video/7686810665801059605", True),
    ("https://www.tiktok.com/discover/suzuki-mehran-parts", False), ("https://www.tiktok.com", False),
    ("https://www.facebook.com/marketplace/category/suzuki", False), ("https://www.instagram.com/explore/tags/mehran/", False),
])
def test_only_real_social_posts_are_kept(url, post):
    from app import websearch
    assert websearch._skip(url, allow_social=True) is (not post)


# ---------------- Pakistani naming, foreign stores, search-provider router ----------------
@pytest.mark.parametrize("make,model,year,exp_make,exp_model,gen", [
    ("Toyota", "Corolla Indus", 2002, "Toyota", "Corolla", (2002, 2008)),
    ("corolla", "indus", 2002, "Toyota", "Corolla", (2002, 2008)),
    ("Indus", "Corolla", 2005, "Toyota", "Corolla", (2002, 2008)),
    ("Honda", "Civic Reborn", 2010, "Honda", "Civic", (2006, 2012)),
    ("Pak Suzuki", "Mehran VXR", 2015, "Suzuki", "Mehran", (1989, 2019)),
    ("BMW", "i7", 2024, "BMW", "i7", None),
])
def test_pakistani_vehicle_naming(make, model, year, exp_make, exp_model, gen):
    from app.vehicles import normalize
    v = normalize(make, model, year)
    assert (v["make"], v["model"]) == (exp_make, exp_model)
    assert (v["generation"][:2] if v["generation"] else None) == gen


@pytest.mark.parametrize("url,foreign", [
    ("https://www.rockauto.com/en/catalog/toyota", True), ("https://www.ebay.com/itm/1", True),
    ("https://ve.ebay.com/b/x", True), ("https://www.amazon.ae/x", True), ("https://shop.co.uk/x", True),
    ("https://www.pakwheels.com/x", False), ("https://www.daraz.pk/x", False), ("https://autostore.pk/x", False),
    ("https://www.olx.com.pk/item/x", False), ("https://ndestore.com/x", False),
])
def test_foreign_store_filter(url, foreign):
    from app.sources.web_deep_search import _foreign_store
    assert _foreign_store(url) is foreign


def test_provider_bucket_limits_budget_and_pauses():
    from app.searchproviders import Bucket
    b = Bucket(per_minute=2, daily_cap=3)
    assert b.available()[0]; b.take()
    assert b.available()[0]; b.take()
    assert b.available() == (False, "rate limit")              # per-minute tokens exhausted
    b.tokens = 5
    assert b.available()[0]; b.take()
    assert b.available() == (False, "daily budget used")       # daily cap reached
    b2 = Bucket(per_minute=10, daily_cap=100)
    b2.fail(432)                                               # Tavily out of credits -> long pause
    ok, why = b2.available()
    assert not ok and "quota" in why


def test_router_merges_providers_and_survives_failures(monkeypatch):
    import httpx
    from app import searchproviders as sp, websearch
    async def searx(client, q, cc, pages=1, allow_social=False):
        return [{"url": "https://www.pakwheels.com/a", "title": "A", "snippet": "from searx", "engine": "bing"}]
    async def nimble(client, q, cc):
        return [{"url": "https://www.pakwheels.com/a", "title": "A", "snippet": "Rs 7,500 from google", "engine": "google(nimble)"},
                {"url": "https://www.daraz.pk/b", "title": "B", "snippet": "", "engine": "google(nimble)"}]
    async def tavily(client, q, cc):
        raise sp.ProviderError(429, "20")
    monkeypatch.setattr(websearch, "searxng", searx)
    monkeypatch.setattr(sp, "PAID", {"nimble": ("NIMBLE_API_KEY", nimble), "tavily": ("TAVILY_API_KEY", tavily)})
    monkeypatch.setenv("NIMBLE_API_KEY", "x"); monkeypatch.setenv("TAVILY_API_KEY", "y")
    monkeypatch.setattr(sp.KB, "configured", lambda: False)
    sp.BUCKETS.clear(); sp._mem.clear()
    stats = {}
    async def go():
        async with httpx.AsyncClient() as c:
            return await sp.search(c, "corolla ac condenser", "PK", primary=True, allow_social=True, stats=stats)
    res = asyncio.run(go())
    assert [r["url"] for r in res] == ["https://www.pakwheels.com/a", "https://www.daraz.pk/b"]   # Google order first
    assert "Rs 7,500" in res[0]["snippet"] and "from searx" in res[0]["snippet"]                 # evidence merged
    assert stats["errors"]["tavily"] == 429 and sp.bucket("tavily").available()[0] is False       # tavily paused
    res2 = asyncio.run(go())                                                                      # 2nd call: cache
    assert stats["cached"] >= 1 and len(res2) == 2
    sp.BUCKETS.clear(); sp._mem.clear()


def test_thin_kb_result_is_researched_again(monkeypatch):
    """A stored result with < 3 verified prices older than 30 min is not trusted: research runs again."""
    from app.sources import web_deep_search as wds
    one = verify({"source_url": "https://autostore.pk/x", "vendor_name": "Autostore", "price": 22000, "currency": "PKR",
                  "price_text": "Rs 22,000", "country_code": "PK", "title": "pads", "item_sold": "brake pads",
                  "is_requested_part": True},
                 {"https://autostore.pk/x": {"url": "https://autostore.pk/x", "markdown": "Autostore brake pads Rs 22,000"}},
                 "PK", "g", EST, "brake pads")
    async def fake_find(*a):
        return [one.model_dump(mode="json")], {"kept": 1}, "brake pads", 3600.0
    async def research(self, q):
        return [], {"fresh": True}
    monkeypatch.setattr(wds.KB, "configured", lambda: True)
    monkeypatch.setattr(wds.KB, "find_result", fake_find)
    monkeypatch.setattr(wds.KB, "put_result", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(wds.WebDeepSearch, "_research", research)
    wds._cache.clear()
    _, stats = asyncio.run(wds.WebDeepSearch().run(PartQuery(make="corolla", model="indus", year=2002, part_name="brake pads")))
    assert stats == {"fresh": True}
    assert wds.WebDeepSearch.cache_key(PartQuery(make="corolla", model="indus", year=2002, part_name="ac condenser")) == \
        wds.WebDeepSearch.cache_key(PartQuery(make="Toyota", model="Corolla", year=2002, part_name="ac condenser"))
    wds._cache.clear()


def test_condense_keeps_wanted_price_on_long_marketplace_page():
    """gari.pk case: hundreds of other products with prices above the wanted item used to fill the budget."""
    from app.sources.web_deep_search import condense
    noise = [f"Seat cover model {i} Rs {1000 + i}" for i in range(400)]
    page = "\n".join(["Gari.pk accessories"] + noise + ["## Toyota Corolla Ac Condenser", "Rs 7500", "Call 0300 1234567"])
    out = condense(page, "Toyota Corolla AC condenser".split())
    assert "Toyota Corolla Ac Condenser" in out and "Rs 7500" in out and len(out) <= 3600


def test_queue_message_shows_place_in_line(monkeypatch):
    import httpx
    from app.aggregator import search as real_search
    from app.sources.demo_catalog import DemoCatalog
    monkeypatch.setenv("MAX_CONCURRENT_SEARCHES", "1")
    main._sem = None
    async def slow(q, adapters=None):
        await asyncio.sleep(0.4)
        return await real_search(q, [DemoCatalog()])
    monkeypatch.setattr(main.aggregator, "search", slow)
    async def go():
        transport = httpx.ASGITransport(app=app, client=("1.2.3.4", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async def one(i):
                r = await c.post("/api/search/stream", json={**BMW, "part_name": f"steering wheel {i}"},
                                 headers={"x-forwarded-for": f"9.9.9.{i}"})
                return r.text
            return await asyncio.gather(one(1), one(2))
    monkeypatch.setattr(main, "_client_ip", lambda req: req.headers.get("x-forwarded-for", "x"))
    a, b = asyncio.run(go())
    texts = a + b
    assert "number 1 in line" in texts and "Your turn" in texts
    assert texts.count("event: result") == 2
    main._sem = None


def test_hung_kb_call_cannot_hang_a_search(monkeypatch):
    """A driver stuck on a dead socket that swallows cancellation: _run must still return at its deadline."""
    k = kbmod.KB()

    class StuckConn:
        call_timeout = 0
    class Acq:
        async def __aenter__(self):
            return StuckConn()
        async def __aexit__(self, *a):
            return False
    class Pool:
        def acquire(self):
            return Acq()
    async def pool():
        return Pool()
    monkeypatch.setattr(k, "_pool", pool)

    async def stubborn(conn):
        while True:                                   # ignores cancellation, like a stuck driver
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue
    async def go():
        t = time.time()
        out = await k._run(stubborn, default="fallback", timeout=0.3)
        return out, time.time() - t
    out, took = asyncio.run(go()) if False else (None, None)
    loop = asyncio.new_event_loop()
    try:
        out, took = loop.run_until_complete(go())
        assert out == "fallback" and took < 1.0
        assert k._down_until > time.time()            # breaker open: next call skips immediately
        out2 = loop.run_until_complete(k._run(stubborn, default="skip", timeout=5))
        assert out2 == "skip"
    finally:
        for t in asyncio.all_tasks(loop):
            t.cancel()
        loop.close()


def test_search_ceiling_returns_error_event(monkeypatch):
    async def forever(q, adapters=None):
        await asyncio.sleep(3600)
    monkeypatch.setattr(main.aggregator, "search", forever)
    monkeypatch.setattr(main, "SEARCH_CEILING_S", 0.5)
    r = TestClient(app).post("/api/search/stream", json=BMW)
    assert "event: error" in r.text and "took too long" in r.text


def test_dropped_visitor_can_rejoin_own_search_but_not_start_another(monkeypatch):
    """Silent network drop: the browser reconnects for the SAME part and is attached, not refused."""
    from app.main import _search_key
    q = PartQuery(**BMW)
    main._running["testclient"] = _search_key(q)
    c = TestClient(app)
    r = c.post("/api/search/stream", json=BMW)
    assert r.status_code == 200 and "Reconnected" in r.text and "event: result" in r.text
    other = {**BMW, "part_name": "front bumper"}
    assert c.post("/api/search/stream", json=other).status_code == 429


# ---------------- part matching: the requested part vs. a piece of it ----------------
from app.partmatch import classify as part_kind  # noqa: E402


@pytest.mark.parametrize("wanted,sold,kind", [
    ("headlight", "Suzuki Alto 660 2019-2024 Headlight Glass | OEM Fitment", "component"),
    ("headlight", "Suzuki Alto 2017-2023 Front Headlight Assey RH China", "exact"),
    ("headlight", "Suzuki Alto Halogen Pair", "component"),
    ("headlight", "Suzuki Mehran LED Headlight H4", "component"),
    ("headlight", "Headlight Assembly Complete Suzuki Alto 660cc", "exact"),
    ("headlight", "Genuine Right-Side Headlight for Mehran - with Crystal Clear lens", "exact"),
    ("headlight", "LED Projector Headlight Assembly Alto 2020", "exact"),
    ("headlight glass", "Suzuki Alto Headlight Glass", "exact"),
    ("headlight glass", "Suzuki Alto Headlight Assembly RH", "related"),
    ("radiator", "Honda City radiator cap", "component"),
    ("radiator", "Honda City 2010 radiator genuine", "exact"),
    ("brake pads", "Front brake pads set with wear sensor", "exact"),
    ("brake pads", "Brake pads kit Toyota", "exact"),
    ("brake pads", "Brake pad wear sensor", "component"),
    ("side mirror", "Side mirror glass left", "component"),
    ("side mirror", "Side mirror complete with motor and glass", "exact"),
    ("AC condenser", "Condenser fan motor Corolla", "component"),
    ("engine mount", "Engine mount Corolla", "exact"),
    ("tail light", "Alto LED tail light", "exact"),
    ("spark plugs", "NGK spark plugs set of 4", "exact"),
    ("headlight", "Headlight protection film transparent", "component"),
])
def test_part_kind(wanted, sold, kind):
    assert part_kind(wanted, sold)[0] == kind



def test_named_part_beats_a_wrong_ai_guess():
    """Live case: the AI called 'Suzuki Alto car headlight left side for sale' a component."""
    assert part_kind("headlight", "Suzuki Alto car headlight left side for sale", "component")[0] == "exact"
    assert part_kind("headlight", "Suzuki Alto front lamp unit", "component")[0] == "exact"   # 'unit' = assembly
    assert part_kind("headlight", "Suzuki Alto part 35120", "component")[0] == "component"   # no part noun: AI decides


def test_component_never_becomes_cheapest():
    """The reported bug: 'Headlight Glass' at PKR 1,350 was ranked the cheapest headlight."""
    from app.aggregator import assemble
    def offer(vendor, title, price):
        url = f"https://{vendor.lower().replace(' ', '')}.pk/x"
        page = {"url": url, "markdown": f"{vendor} {title} Rs {price:,} Karachi Pakistan"}
        return verify({"source_url": url, "vendor_name": vendor, "price": price, "currency": "PKR",
                       "price_text": f"Rs {price:,}", "country_code": "PK", "title": title, "item_sold": title,
                       "is_requested_part": True}, {url: page}, "PK", "g", EST, "headlight", model="Alto")
    glass = offer("Auto Parts Bank", "Suzuki Alto Headlight Glass", 1350)
    lamp = offer("PakWheels", "Suzuki Alto Front Headlight Assey RH", 6499)
    assert glass.match_kind == "component" and lamp.match_kind == "exact"
    r = assemble(PartQuery(make="Suzuki", model="Alto", year=2020, part_name="headlight"), [glass, lamp], {}, ["t"])
    assert r.cheapest_overall.offer.vendor.name == "PakWheels"
    assert [o.offer.vendor.name for o in r.related] == ["Auto Parts Bank"]
    assert all(o.offer.match_kind == "exact" for o in r.local)
