import asyncio

import pytest
from fastapi.testclient import TestClient

from app import customs, shipping
from app.aggregator import search, trust_score
from app.main import app
from app.models import Authenticity, Condition, Offer, PartQuery, SortMode, Vendor
from app.sources.base import SourceAdapter
from app.sources.demo_catalog import DemoCatalog

BMW = dict(make="BMW", model="i7", year=2024, part_name="steering wheel", customer_city="Lahore")


def run(q, adapters=None):
    return asyncio.run(search(q, adapters))


# ---------- customs ----------
def test_classify_longest_keyword_wins():
    assert customs.classify("BMW steering wheel")[0] == "8708.94"
    assert customs.classify("front brake pad set")[0] == "8708.30"
    assert customs.classify("mystery widget")[0] == "8708.99"


def test_duty_cascade_matches_hand_calc():
    cb = customs.estimate("steering wheel", 100_000, 20_000, "courier", True)
    av = (100_000 + 20_000 + 1_000) * 1.01            # CIF incl 1% insurance, +1% landing
    cd, acd, rd = av * .35, av * .06, av * .10
    st = (av + cd + acd + rd) * .18
    it = (av + cd + acd + rd + st) * .06
    assert cb.assessable_value_pkr == round(av)
    assert abs(cb.total_duties_pkr - (cd + acd + rd + st + it)) <= 1


def test_non_filer_pays_more():
    f = customs.estimate("steering wheel", 100_000, 20_000, "courier", True).total_duties_pkr
    nf = customs.estimate("steering wheel", 100_000, 20_000, "courier", False).total_duties_pkr
    assert nf > f


def test_de_minimis_and_zero():
    cb = customs.estimate("oil filter", 900, 5_000, "courier", True)
    assert cb.de_minimis_applied and cb.total_duties_pkr == 0
    assert customs.estimate("oil filter", 0, 0, "courier", True).total_duties_pkr == 0


def test_airbag_dg_warning():
    assert any("Dangerous Goods" in w for w in customs.estimate("airbag", 50_000, 10_000, "courier", True).warnings)


# ---------- shipping ----------
def _offer(country="JP", w=3.2, dims=(45, 45, 20), price=1000.0, cur="USD", region="Tokyo"):
    v = Vendor(id="x", name="x", country=country, region=region)
    return Offer(vendor=v, title="t", condition=Condition.new, authenticity=Authenticity.oem,
                 price=price, currency=cur, weight_kg=w, dims_cm=dims)


def test_volumetric_weight_billed():
    # 45*45*20/5000 = 8.1 kg volumetric > 3.2 actual -> 8.5 kg chargeable
    assert shipping.chargeable_kg(_offer(), 5000) == 8.5


def test_unknown_country_no_quotes():
    assert shipping.international_quotes(_offer(country="ZZ"), "Lahore") == []


def test_sea_only_for_heavy():
    light = {q.mode for q in shipping.international_quotes(_offer(w=3), "Lahore")}
    heavy = {q.mode for q in shipping.international_quotes(_offer(w=80, dims=(120, 80, 60)), "Lahore")}
    assert "sea" not in light and "sea" in heavy


def test_aramex_weight_cap():
    carriers = {q.carrier for q in shipping.international_quotes(_offer(w=100, dims=(50, 50, 50)), "Lahore")}
    assert "Aramex" not in carriers and "DHL Express" in carriers


# ---------- pipeline ----------
def test_search_groups_and_reconciles():
    r = run(PartQuery(**BMW))
    assert r.local and r.international and r.contact_only
    # every landed total = item + shipping + duties
    for g in r.international:
        for opts in g.regions.values():
            for o in opts:
                assert o.total_landed_pkr == o.item_price_pkr + o.shipping.cost_pkr + o.customs.total_duties_pkr
    # countries sorted by best landed price
    bests = [g.best_total_pkr for g in r.international]
    assert bests == sorted(bests)
    assert r.cheapest_overall.total_landed_pkr == min(
        [o.total_landed_pkr for o in r.local] + bests)


def test_no_match_returns_empty_not_crash():
    r = run(PartQuery(make="Lada", model="Niva", part_name="flux capacitor"))
    assert r.local == [] and r.international == [] and r.cheapest_overall is None


def test_filters():
    r = run(PartQuery(**BMW, authenticity=[Authenticity.oem], conditions=[Condition.new]))
    allopts = r.local + [o for g in r.international for v in g.regions.values() for o in v]
    assert allopts and all(o.offer.authenticity == Authenticity.oem and o.offer.condition == Condition.new for o in allopts)
    r = run(PartQuery(**BMW, min_vendor_rating=4.7))
    assert all((o.offer.vendor.rating or 0) >= 4.7 for g in r.international for v in g.regions.values() for o in v)


def test_sort_fastest_and_top_rated():
    r = run(PartQuery(**{**BMW, "sort": SortMode.fastest}))
    for g in r.international:
        for opts in g.regions.values():
            assert [o.days_max for o in opts] == sorted(o.days_max for o in opts)
    assert trust_score(4.7, 2000) > trust_score(5.0, 3)


def test_broken_adapter_is_isolated():
    class Boom(SourceAdapter):
        name = "boom"
        def enabled(self): return True
        async def search(self, q): raise RuntimeError("down")
    r = run(PartQuery(**BMW), [Boom(), DemoCatalog()])
    assert r.cheapest_overall is not None


def test_api_endpoint():
    c = TestClient(app)
    assert c.get("/").status_code == 200
    r = c.post("/api/search", json=BMW)
    assert r.status_code == 200 and r.json()["cheapest_overall"]
    assert c.post("/api/search", json={"make": "BMW"}).status_code == 422
