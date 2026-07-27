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

`test/*.py` are **not** unit tests — there is no test suite. They are standalone, stdlib-only
scripts that make **live** Apibara calls to pin down the API's real field shapes, and write raw
JSON to `test_run/` (gitignored). Each burns 1–2 of the 100/month free-plan budget, so the cost is
stated in every docstring. They import nothing from `app/` and resolve `.env` / `test_run/` off
their own file location, so they run from any working directory.

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
- `app/auction_api.py` — demo lookup fallback
- `app/db.py` — SQLite storage (`tracker.db`)
- `static/index.html` — the UI
- `test/` — live API probe scripts + `run_sold.sh` (see above); `test_run/` holds their JSON output
- `.cc-discussion/` — session logs from Claude Code work, written for Obsidian
