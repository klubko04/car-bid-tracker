# Car Bid Tracker

Track salvage-auction cars you want to bid on, with per-platform fee stacks, hidden-cost contingencies, the 40–60% rebuild rule, a value ranking across your watchlist — and a **scanner** that watches MarketCheck's auction inventory (Copart/IAAI and other US auction sources) for cars matching your saved searches.

## Run it (Windows)

```powershell
cd C:\Users\broho\Documents\Claude\Projects\Cars
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000

## Data sources

The app uses two APIs for different jobs:

- **Apibara** — *discovery* (real Copart + IAAI). Every field that matters for salvage: current bid, primary/secondary damage, title/sale-document, odometer, run condition, seller type, sale date, location, photos. Live bid data updates ~15s, records ~30min. This is the scanner's source and the Add-Car lookup source.
- **MarketCheck** — *pricing* (dealer retail comps + VIN listing history). Feeds the ⟳ comps clean-value button and VIN due-diligence. Not used for discovery (its auction slice is broker mirrors).

## The scanner (Apibara — Copart + IAAI)

1. `.env` already has `APIBARA_API_KEY` set. Restart the server; the header badge reads "Scanner: Apibara (Copart + IAAI live)".
2. In the **Scanner** tab, save a search (make `Lexus`, model `IS 300`, years, max price, ZIP + radius or state).
3. The background poller runs every `POLL_INTERVAL_HOURS` (default 24) while the server runs; new matches land in the Watchlist with a NEW badge and *real* damage/title/bid, so the max-bid verdict is meaningful immediately. "Run now" polls on demand.
4. Add-Car → **Fetch** now pulls a real Copart/IAAI lot by **lot number or VIN**.

Endpoint: `GET https://apibara.tech/api/v1/vehicle-auction/vehicles` (auth `X-API-Key`, cursor pagination, max 20/page).

**Free Test-plan budget: 100 calls/month, 1 req/sec.** One search polled every 24h ≈ 30 calls/month. Keep 1–2 active searches, or upgrade the plan. `APIBARA_MAX_PAGES=1` caps each poll to one 20-lot page — raise only with plan headroom. Budget/plan errors show on the search card.

**Email alerts:** fill the `SMTP_*` values in `.env` (Gmail = App Password). Otherwise alerts are in-app (NEW badges).

## Copart CSV import (the real salvage feed)

MarketCheck's auction slice is broker mirrors — thin. The richest legal source is Copart's own member CSV:

1. Register free at copart.com (Basic membership).
2. Download a sales list from [Download Sales Data](https://www.copart.com/content/us/en/buyer/sales/download-sales-data).
3. Scanner tab → **Copart CSV import** → choose the file → Import.

Rows matching your active saved searches are added to the watchlist with Copart's own lot #, sale date/time, damage, run/drive, title, current high bid, **est. retail value** (pre-fills clean value) and **repair cost** (pre-fills repair estimate). Re-imports are deduped. Treat Copart's repair estimate as a floor — verify from photos.

For analytics archives, Copart APIBara JSON can also be enriched with the free
NHTSA vPIC VIN decoder. The raw response remains untouched; the derived copy is
written to the matching sold/open `json-adapted/copart/` folder. Copart
analytics are US-only: raw JSON remains a complete source capture, while the
adapter excludes Canadian and unknown-market lots before vPIC enrichment. The
CSV flattener applies the same guard defensively, so neither `csv-raw` nor
`csv-cut` can reintroduce Canada:

```bash
python analytics/scripts/copart_vpic_adapt_01.py <copart-raw-archive>.json
python analytics/scripts/apibara_json2csv_copart_01.py <copart-adapted-archive>.json
python analytics/scripts/data_pull_01.py copart <copart-adapted-archive>.json
```

The decoder fills missing trim, body class, doors, engine cylinders/horsepower,
plant and manufacturer fields. It never overwrites APIBara identity or specs;
conflicts remain attached to the record with provenance. Decodes are cached
under `analytics/data/cache/nhtsa-vpic/`, so repeat sold/open adaptations make
no NHTSA request.

The Copart flattener writes the canonical, unfiltered 91-column extract to
`csv-raw/copart/`. `data_pull_01.py copart` imports the same mapping but reads
the JSON again, then adds tier, tier source and sold period while writing
`csv-cut/copart/`; it does not read the intermediate CSV file.

`copart_image_urls` contains pipe-joined direct Copart `_hrs.jpg`/`_vhrs.jpg`
assets copied from `media.items[].large`. `pull_images_01.py` detects that
column, preserves each URL verbatim, and can consume a Copart open-lot csv-cut
without reconstructing an IAAI resizer URL.

### Copart web open-lot capture

`pull_copart_web_01.py` archives Copart's first-party search response without an
API key or APIBara quota. It defaults to six yearly searches for 2018–2023 Audi
S5 lots. Copart groups S5 and RS5 together, so the pull uses the exact `MODL=S5`
facet and then independently verifies returned year, make, and model. The raw
response keeps any rejected rows for audit; only exact matches enter the
top-level `records` list:

```bash
python analytics/scripts/pull_copart_web_01.py --dry-run
python analytics/scripts/pull_copart_web_01.py
python analytics/scripts/pull_copart_web_01.py --details --max-details 5   # diagnostic only
```

The August 17, 2026 run found 74 exact open S5 lots (45/16/2/7/2/2 for
2018–2023) in six requests: 72 U.S. and 2 Canadian (Edmonton, Montreal).
Market filtering still belongs at the adapter boundary — Canadian/unknown rows
must remain auditable in json-raw but must not reach adapted JSON or either CSV
layer.

**Seller comes from the search row, at no extra cost.** Copart ships the seller
company name in `scn`: 18 of 74 rows (25%), every one a carrier (GEICO 10,
USAA 5, CSAA 1, Bristol West 1, Farmers 1). `showSeller` is a *display* flag and
not a presence test — 14 of those 18 rows have `scn` while `showSeller` is
false. Copart publishes no seller *type* anywhere and has no seller facet, so
class is inferred from the name by `analytics/scripts/copart_seller.py`.
Absence stays `unknown` and never becomes `non_insurance`.

**`--details` is a contract probe, not a data path.** The lot-details endpoint
returns the same Solr document as the search row (111 identical keys) minus
five the search row has, including trim — and no seller field at all. It is
also WAF-blocked after roughly six lots: a full 74-lot pass scored 6 successes
against 67 Imperva failures (45 served as HTTP 200, 22 as 403), and the
lot-page HTML fallback was blocked on every row. Keep it out of scheduled runs.

**VINs are masked** — `fv` arrives as `WAUB4CF52JA******` on every row, in both
search and detail responses. This source therefore cannot feed
`copart_vpic_adapt_01.py` and cannot be VIN-joined to an APIBara pull; its lots
are keyed by lot number alone.

## Clean-value auto-fill

In any car's detail view, the **⟳ comps** button fetches the median asking price of matching used dealer listings via MarketCheck (1–2 API calls) and sets it as the clean-title value — the anchor for the whole max-bid calculation.

## How the math works

- **Fee stack** per buying route (Copart direct, IAAI direct, A Better Bid, AutoBidMaster Adv/Premium) — 2026 public schedules, editable in `app/fees.py`.
- **Budget ceiling** = clean-title value × target ratio (default 50%; the 40–60% rebuild rule).
- **Max bid** = largest bid where bid + fees + repair + transport + contingencies ≤ ceiling.
- **Margin** = est. rebuilt resale (default 70% of clean value) − all-in cost at your planned bid.
- **Contingencies** (hidden costs): inspection $150, non-runner surcharge $150, storage buffer $100, relocation buffer $300, re-registration $250 — toggle per car, amounts editable in `app/fees.py`.

All figures are estimates — platforms change fees; verify before bidding. Every bid is a binding contract (Copart relist penalty: max($600, 10%); IAAI: max($1,000, 15%)).

## API probe scripts (`test/`)

Most `test/*.py` files are standalone scripts that make **live** APIBara calls
to pin down the API's real field shapes and write raw JSON to `test_run/`
(gitignored). Each live probe states its 1–2-call cost in its docstring. The two
Copart pipeline regression files are real, zero-network unit tests:

```bash
python3 test/test_copart_vpic_adapt_01.py
python3 test/test_copart_json2csv_01.py
python3 test/test_pull_copart_web_01.py
python3 test/test_copart_seller_01.py
```

```bash
python test/test_apibara.py                 # 2 calls — original probe: Lexus IS 350 + Audi A4
python test/test_apibara_search01.py        # 2 calls — Lexus ES 350 + Audi S5, per_page=20
python test/test_apibara_history01.py       # 1-2 calls — prior auction runs for one VIN

./test/run_sold.sh iaai                     # 1 call  — sold IAAI lots + IAAI-only filters
./test/run_sold.sh copart                   # 1 call  — sold Copart lots
./test/run_sold.sh generic                  # 2 calls — ended lots + one /history lookup
./test/run_sold.sh all                      # 4 calls — all three
```

`run_sold.sh` prints the quota cost and asks for confirmation before spending anything.

Server-side filters live in a `PARAMS` / `SEARCHES` constant at the top of each script;
client-side filters (`BODY_STYLES`, `EXCLUDE_DAMAGE`, `SELLER_TYPES`) are separate constants,
because Apibara has **no body-style parameter** and its `damage` filter is include-only with an
enum that cannot express collision damage. Sold/ended lots come from `lot_sub_status=Ended`
(*not* `lot_status`, which only accepts `All` / `Timed` / `Buy Now`).

What these scripts established, verified against 60 live records:

- **Prior sale attempts** are not in the search payload — only a single `last_sold_*` snapshot.
  Full run history needs `GET /vehicles/{VIN}/history` (bare VIN or lot number, **not** the
  record's `slug_vin`), 1 call per VIN, returning `{platform, date, price, status}` per run.
- **`status` is a bid outcome, not a transaction.** `"Sold on Approval"` means the high bid missed
  the seller's reserve. Lots marked `"Sold"` demonstrably relist afterwards.
- **Reserve prices are not published** by any field; they can only be bounded from history
  (on-approval rows sit below the reserve).
- **IAAI records are far richer than Copart's**: `ActualCashValue` (27/27), `EstimatedRepairCost`
  (17/27), `body_style` (27/27) and a pre-bidder count all come from IAAI's `details` block.
  Copart records have **no `details` block at all**.

## Files

- `main.py` — FastAPI app + routes
- `app/fees.py` — fee schedules + contingency defaults (edit here)
- `app/calculator.py` — max-bid / margin math
- `app/apibara.py` — Apibara Copart/IAAI client (discovery + lookup)
- `app/scanner.py` — saved-search poller, source dispatch, MarketCheck pricing, email alerts
- `app/copart_csv.py` — Copart member CSV importer
- `analytics/scripts/copart_vpic_adapt_01.py` — fill-only Copart VIN/spec enrichment with NHTSA vPIC
- `analytics/scripts/pull_copart_web_01.py` — exact Copart open-lot search archive (seller from `scn`, no quota)
- `analytics/scripts/copart_seller.py` — shared seller taxonomy: insurance / finance / dealer / non_insurance / unknown
- `analytics/scripts/apibara_json2csv_copart_01.py` — Copart raw/adapted JSON → 91-column csv-raw
- `app/auction_api.py` — demo lookup fallback
- `app/db.py` — SQLite storage (`tracker.db`)
- `static/index.html` — the UI
- `test/` — live API probe scripts + `run_sold.sh` (see above); `test_run/` holds their JSON output
- `.cc-discussion/` — session logs from Claude Code work, written for Obsidian
