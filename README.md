# PartsBridge PK

Find any car part for a customer in Pakistan at the lowest **landed cost**:

1. **Local first**: Pakistani vendors with listed prices, plus dealers with no listed price (name, address, phone, rating) to contact for a quote.
2. **Worldwide**: offers grouped **country → region** (UAE/Dubai, Sharjah; Japan/Tokyo, Osaka, Nagoya; Germany; UK; US; China; Thailand; Malaysia; Saudi Arabia; Turkey…), with countries sorted by cheapest landed price.
3. **Import cost**: every international offer gets quotes from several carriers (DHL, FedEx, Aramex, Pakistan Post EMS, LCL sea for heavy parts). Each quote shows the Pakistan customs breakdown (CD, ACD, RD, Sales Tax, withholding income tax), the HS code, clearance time, import rules and warnings, all summed into a door-to-door PKR total and day range.
4. **Filters/sorts**: Cheapest, Best value (cost/speed/trust blend), Top rated (review-count-weighted Bayesian rating), Fastest; condition (new/used/refurbished), Genuine OEM only, minimum vendor rating, FBR filer status.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload      # open http://127.0.0.1:8000  (API docs at /docs)
pytest -q
```

## Deep web search (LLM + Firecrawl), runs on every search

```
query -> LLM: local-language search queries per country (ja/de/zh/ar...) + est. weight/box
      -> Firecrawl /v2/search (country-targeted, page markdown)
      -> LLM: extract sellers/dealers/prices as JSON
      -> code verification: URL was fetched, vendor name, price text, phone and email appear on that page
```
LLM fallback order: **Groq `openai/gpt-oss-120b` → Gemini `gemini-3.8-flash` → Gemini `gemini-flash-lite-latest` → OpenRouter `openai/gpt-oss-120b` → OpenRouter `openrouter/free`**. A provider is skipped on missing key, HTTP error, timeout or non-JSON output.
Keys go in `.env` (see `.env.example`). Deep search turns on only when `FIRECRAWL_API_KEY` is set: an LLM without fetched pages would invent vendors.
Live check: `python3 scripts/smoke_llm.py`. Results are cached 6 h per part.

## Architecture

```
app/sources/*      adapters -> Offer list (priced or contact-only)
  demo_catalog     fictional seed data ([DEMO]); disable with DISABLE_DEMO=1
  ebay             eBay Browse API, priced listings that deliver to PK   (EBAY_OAUTH_TOKEN)
  web_deep_search  Firecrawl + LLM chain (app/llm.py, app/firecrawl.py)
  google_places    Places Text Search: dealer name/address/phone/rating across PK + world hubs (GOOGLE_PLACES_API_KEY)
app/fx.py          PKR conversion; static table, or live feed with FX_LIVE=1
app/shipping.py    volumetric weight (LxWxH/5000), carrier rate cards, sea LCL + onward delivery, local couriers (TCS, Leopards, M&P)
app/customs.py     HS classification + Pakistan duty cascade
app/aggregator.py  filter -> landed cost per shipping quote -> score/tag -> group by country/region
config/*.json      ALL rates: edit these, not code
```

Duty cascade (`app/customs.py`):
`AV = (item + freight + 1% insurance) x 1.01 landing`; `CD, ACD, RD = rate x AV`; `ST = 18% x (AV+CD+ACD+RD)`; `WHT = filer/non-filer % x (AV+all duties+ST)`.

## Before production (must do)

| Item | Status |
|---|---|
| `config/pk_customs.json` CD/ACD/RD per HS code, WHT %, courier de minimis | **Placeholders.** Set from current Pakistan Customs Tariff / WeBOC and latest SROs; fill `last_verified`. |
| `config/shipping.json` carrier rates | **Illustrative.** Replace with DHL / FedEx / UPS / Aramex rate APIs. Carrier phone numbers are intentionally null. |
| `config/fx.json` | Static fallback. Set `FX_LIVE=1` or connect the SBP / interbank rate. |
| Vendor data | Demo only. Real sources = official APIs (eBay, Google Places), vendor onboarding (Dubai/Sharjah, Japanese exporters upload stock), and only scrapers that respect site ToS/robots.txt. PakWheels/OLX have no public API. |
| Part fitment | Matching uses make/model/year/keywords or exact OEM number. Add VIN decoding + OEM catalogue (e.g. TecDoc licence) to avoid wrong-part orders. |
