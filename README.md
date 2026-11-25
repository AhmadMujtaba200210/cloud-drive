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

## Deep web search: 100% free and self-hosted

```
query ─► LLM: 2 local-language queries per country (ja / de / th / tr / ar …) + packed-size estimate
      ─► SearXNG (self-hosted metasearch, JSON API)                     ~500 results / 12 countries
      ─► LLM triage: keep the 6 best pages per country (drops whole cars, other makes, forums)
      ─► Firecrawl Local /v2/scrape → markdown (plain HTTP fetch fallback)
      ─► condense: keep part/price lines + footer contact lines (fits free-tier token limits)
      ─► LLM extraction, 3 pages per call
      ─► code verification (not trusted to the prompt):
           URL was fetched · vendor name on page · price text on page AND parses to the price (±1%)
           · phone/email on page · not an accessory/cover · seller country proven by domain, phone code,
           verbatim address, or (weak, never "local") currency
      ─► landed cost, ranking, market analysis
```

### One-time setup (no Docker, no paid API)
```bash
scripts/local_search_stack.sh install   # cargo install firecrawl-local + SearXNG from official git
scripts/local_search_stack.sh start     # SearXNG :8888, Firecrawl Local :3002 (FIRECRAWL_CLOUD=off)
scripts/local_search_stack.sh status
cp .env.example .env                     # add LLM keys; SEARXNG_URL / FIRECRAWL_BASE_URL already point local
uvicorn app.main:app --port 8000
```

LLM fallback: **Groq `openai/gpt-oss-120b` → OpenRouter `openai/gpt-oss-120b` → OpenRouter `openrouter/free` → Gemini `gemini-3.5-flash-lite` → Gemini `gemini-3.1-flash-lite`** (Gemini last; Flash-Lite only, enforced in code).
A provider is skipped on missing key, HTTP error, timeout or non-JSON; after a 429/5xx it cools down (Retry-After, else 30–60 s).
The response's `diagnostics.web_deep_search` shows every stage count, drop reasons, providers used and timing.

### Market analysis (`app/analysis.py`)
Per-country landed-price min/median/max · import-vs-local saving and extra days · cost split (item / shipping / duties)
of the cheapest import · **duty break-even multiplier** (how wrong the duty estimate can be before the verdict flips)
· "Unusually cheap: verify" flag via log-price modified z-score (MAD, cutoff 3.5) · cross-source de-duplication.

## Deploy on an Oracle Cloud VM (public site)

On the VM (Ubuntu 22.04/24.04 or Oracle Linux 8/9, x86 or ARM Ampere):
```bash
git clone -b claude/global-car-parts-marketplace-1deni7 https://github.com/AhmadMujtaba200210/cloud-drive.git
cd cloud-drive
sudo deploy/install.sh                                         # serves http://<public-ip>/
# or with HTTPS:  sudo DOMAIN=parts.example.com EMAIL=you@example.com deploy/install.sh
sudo nano /etc/partsbridge/env                                  # paste LLM keys (never committed)
sudo systemctl restart partsbridge-app
```
OCI console: VCN → Subnet → Security List → add Ingress TCP 80 and 443 from 0.0.0.0/0 (the script opens the OS firewall itself).
Updates: `sudo deploy/update.sh`. Logs: `journalctl -u partsbridge-app -f` (also `-searxng`, `-firecrawl`).

What runs: nginx :80/:443 (public) → uvicorn 127.0.0.1:8000; SearXNG 127.0.0.1:8888; Firecrawl Local 127.0.0.1:3002
(cloud off). Only nginx is reachable from outside. Demo data is off in production.
Quota protection: nginx 6 searches/min per IP (burst 3) + app `RATE_LIMIT_PER_HOUR` (default 30) per IP;
set `ACCESS_TOKEN` in `/etc/partsbridge/env` to make the site private (`https://host/?token=VALUE`).

## Architecture

```
app/sources/*      adapters -> Offer list (priced or contact-only)
  demo_catalog     fictional seed data ([DEMO]); disable with DISABLE_DEMO=1
  ebay             eBay Browse API, priced listings that deliver to PK   (EBAY_OAUTH_TOKEN)
  web_deep_search  SearXNG + Firecrawl Local + LLM chain (app/websearch.py, app/firecrawl.py, app/llm.py)
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
