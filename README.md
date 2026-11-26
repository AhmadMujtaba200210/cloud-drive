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

### Deeper research (v0.2)
- **Follows product links**: a listing / category / search page (≥4 prices, or a listing-shaped URL) has its same-site
  product links that name the model + part (or OEM number) opened too, for exact prices and seller details.
- **OEM-number pass**: when a manufacturer part number is printed on pages from ≥2 different sites, it is searched
  directly in PK / AE / JP / US. Part numbers find listings that never use the part's name.
- **Confidence + checks per offer** (`high | medium | low`): price printed on page, seller location proof, model on
  page, OEM number match. The buyer sees the list of checks; dedupe keeps the best-evidenced copy.
- **Resilient search**: SearXNG runs bing, brave, yahoo, naver, yandex, mwmbl + duckduckgo (`infra/searxng/settings.yml`);
  an empty query is retried, then simplified. Time budget `DEEP_SEARCH_BUDGET_S` (default 170 s) always returns
  partial results instead of hanging. Scraped pages are cached for 6 h.

### Knowledge base: Oracle Autonomous Database (vector) — fewer AI calls, same accuracy
`app/kb.py` stores research plans (14 d), scraped pages (24 h), **AI extractions keyed by the page's exact content
hash** (14 d: an unchanged page is never sent to the AI twice; a changed price is a new hash), verified results
(12 h) and known-good seller pages (30 d), with 384-dim embeddings generated locally (fastembed, no API).
Vector search proposes similar earlier searches; the validator `same_part()` accepts a match only when the
canonical part signature is identical (plural / synonym / filler-insensitive, front≠rear, left≠right), since
embedding distance alone ranks "radiator fan" closer to "radiator" than "brake pad set" to "brake pads".
Verification in code (price on page, location proof) always re-runs on reused extractions. Every DB call has a
6 s timeout and fails soft. Setup on the server: wallet in `/etc/partsbridge/oracle-wallet`, then
`sudo deploy/oracle_setup.sh` (creates least-privilege user `PARTSBRIDGE_APP`; the ADMIN password is not stored).
Maintenance: `python -m app.kb ping | purge`.

### Pakistan-only mode (default)
`scope=local` searches only Pakistan, deeper: planner queries in English / Roman Urdu / Urdu, site-targeted
queries for daraz.pk, pakwheels.com, olx.com.pk, plus Facebook / Instagram shops (verified against the search
engine's public snippet, since those pages need a login). `scope=worldwide` adds the 12-country comparison.

### Traffic analytics: self-hosted Umami
`deploy/umami/docker-compose.yml` runs Umami 2.20 + its own Postgres (dashboard on 127.0.0.1:3001, no public ports;
the DB service is named `umami-db` because the shared Docker network already has a `db`). nginx serves the tracker
from the app's own origin (`/u.js`, `/api/send`, rate-limited), so the strict CSP needs no exception. No cookies,
Do Not Track honoured, query strings stripped. Product events: `search_started / search_completed / search_failed /
search_abandoned / no_results`, `open_product / seller_website / contact_call / contact_whatsapp / contact_email`
(with seller country and whether it was a social post), `sort_change`, `price_slider / price_preset /
budget_suggestion_used`, `filter_country / filter_condition`, `toggle_pakistan_only / toggle_social`,
`autocomplete_pick / autocomplete_custom` (custom values show what the make/model/part lists are missing).
Public view: Umami's read-only share page on its own host (portfolio Caddy). Only the share page, its assets and GET
stats for this one site are proxied; `/login`, `/api/auth/*` and every other admin route return 404 there, so the
admin UI is reachable only through an SSH tunnel to 127.0.0.1:3001.

### Search providers (app/searchproviders.py)
SearXNG (free, self-hosted) runs every query; Nimble (`/v2/serp`, real Google results localized with `country`)
and Tavily (`/search`, `country=pakistan`) run the most important queries of each search (3 per country in
Pakistan-only mode, 1 worldwide). Google `/goto?url=` links are resolved via their 302. Every paid provider has a
shared per-minute token bucket, a daily budget (`NIMBLE_DAILY_CAP=160`, `TAVILY_DAILY_CAP=30`, i.e. inside the free
tiers) and a circuit breaker (429 -> Retry-After; 401/402/432/433 -> 30 min; errors -> 60 s), so with many users one
throttled provider never stalls a search. Results are merged by URL (Google order first, every engine's snippet kept
as evidence) and cached for 3 days in memory + the Oracle knowledge base. Keys live only in `/etc/partsbridge/env`.

### Pakistani naming (app/vehicles.py)
"Corolla Indus" / "Indus Corolla" -> Toyota Corolla, "Pak Suzuki" -> Suzuki, "Honda Atlas" -> Honda, trims such as
"Civic Reborn" -> Civic. Year -> generation range ("Corolla 2002" -> 2002-2008), because sellers list parts that way;
a generation-range query is searched first. Pakistan-only mode drops overseas stores (RockAuto, eBay, Amazon,
PartsGeek…) and foreign ccTLDs before triage. Cache keys use the canonical vehicle and a pipeline version; a stored
result with fewer than 3 verified prices is only reused for 30 minutes.

### Live research feed
`POST /api/search/stream` (Server-Sent Events) narrates the real pipeline as it runs: the planner's strategy,
each search query, which pages the AI picked and why, every page opened, each price confirmed on its page, claims
discarded, the OEM pass, and the landed-cost calculation; then a final `result` event. The page types these out
like a model thinking, with live counters and a "still working on Japan, China…" line during quiet stretches.
If the stream drops, the browser falls back to polling `/api/search`; the server keeps researching and caches it.

### Public-server hardening
Strict CSP (no inline script/style), `nosniff`, `frame-ancestors 'none'`, Permissions-Policy · no search on page
load · cached searches (and sort-tab changes) cost no quota · one running search per IP, `MAX_CONCURRENT_SEARCHES`
(default 3) server-wide with a queue message · input length/charset limits (fields go into LLM prompts) · scraped
text is fenced as untrusted in every prompt · SSRF guard on every fetched URL and redirect hop (no private /
loopback / metadata IPs, DNS-checked) · capped fetch size · no internal error text in API responses ·
`DISABLE_DOCS=1` hides /docs and /openapi.json in production.

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
If Caddy already runs on the VM, the installer adds a Caddy site block instead of nginx, served at
`https://<ip-with-dashes>.sslip.io/` (or your `DOMAIN`) so Let's Encrypt can issue a real certificate; a bare IP cannot get one.

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
