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
