# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Env: conda env "carbid" (python 3.12) — see environment.yml / .vscode/settings.json
pip install -r requirements.txt

uvicorn main:app --reload          # http://127.0.0.1:8000
```

There is **no test suite, linter, or formatter configured.** `test/*.py` are *not* unit tests —
they are standalone stdlib-only probe scripts that make **live** Apibara calls against the real API
(each burns 1–2 of a 100/month free-plan budget) and write raw JSON into `test_run/` (gitignored).
Do not run them casually; each file's docstring states its call cost. They import nothing from
`app/` — they exist to pin down real API field shapes, and every mapping bug found so far
(`sale_document.name` vs `type`, sold lots carrying `last_sold_price_usd` instead of
`current_bid_usd`) came from diffing their output against `apibara.normalize_record`.

```bash
python test/test_apibara_search01.py      # 2 calls: live Lexus ES 350 + Audi S5 searches
python test/test_apibara_sold01.py        # 2 calls: ended lots (lot_sub_status=Ended) + one /history
python test/test_apibara_history01.py     # 1-2 calls: prior auction runs for one VIN
python test/test_apibara_sold_iaai_01.py  # 1 call: sold IAAI lots + IAAI-only client-side filters
python test/test_apibara_sold_copart_01.py # 1 call: sold Copart lots
```

They resolve `.env` and `test_run/` off `Path(__file__).parent.parent`, so they run from any
working directory. Server-side filters live in a `PARAMS`/`SEARCHES` constant at the top of each
file; client-side filters (`BODY_STYLES`, `EXCLUDE_DAMAGE`) are separate constants, because
Apibara has no body-style param and its `damage` filter is include-only with an enum that cannot
express collision damage.

To exercise the app with no API keys and no HTTP, use the built-in mock modes:

```bash
APIBARA_MOCK=1 uvicorn main:app --reload        # canned Copart+IAAI records
MARKETCHECK_MOCK=1 uvicorn main:app --reload    # canned listings + canned clean-value
```

`.env` is gitignored; copy `.env.example`. `tracker.db` (SQLite) is created on first run and is
also gitignored — deleting it is the reset button.

## Architecture

FastAPI backend + single-file vanilla-JS frontend (`static/index.html`, ~620 lines, no build step).
All state lives in SQLite via stdlib `sqlite3` (no ORM). Everything is one process; the scanner is
an `asyncio` task inside the web server, not a separate worker.

**Two external APIs with strictly separate jobs** — do not conflate them:

- **Apibara** (`app/apibara.py`) — *discovery + lot lookup*. Real Copart/IAAI salvage fields:
  current bid, primary/secondary damage, sale document, odometer, run condition. Free plan is
  **100 calls/month, 1 req/sec**, hence `RATE_DELAY = 1.1` and `APIBARA_MAX_PAGES=1`.
- **MarketCheck** (`app/scanner.py`) — *pricing only*: `fetch_clean_value()` pulls median dealer
  asking price to anchor the whole max-bid calculation. Its auction slice is broker mirrors, so it
  is only a *fallback* discovery source when Apibara is unconfigured.
- **Copart member CSV** (`app/copart_csv.py`) — offline first-party bulk import, no API quota.
- **`app/auction_api.py`** — legacy demo/aggregator lookup, last-resort fallback with 3 hardcoded lots.

### Source dispatch (the layering that matters)

`scanner.py` is the orchestrator, not just the MarketCheck client. `scanner._discover()` picks the
source: **Apibara if configured, else MarketCheck**. Everything downstream — dedup, watchlist
insert, email alert, `update_search_run` — is source-agnostic. Each adapter's job is to emit the
same normalized car dict (`apibara.normalize_record`, `scanner.normalize_listing`,
`copart_csv.row_to_car`), keyed for dedup by **`mc_listing_id`** — a legacy name that is now the
universal dedup key across all sources (`apibara:{platform}:{lot}`, `copart:{lot}`, or a raw
MarketCheck id). Dedup is two-layer: `seen_listings` table by that key, plus `db.vin_tracked()` so
the same VIN appearing on two sources isn't added twice.

Adding a new discovery source = new adapter module emitting that dict + a branch in `_discover()`.

### The money math (`app/calculator.py` + `app/fees.py`)

The whole app exists to answer "what is the most I can bid?". `fees.py` holds hand-maintained 2026
fee tables per buying route (Copart direct, IAAI direct, and three broker stacks) — **this is the
file to edit when fees change**, it is data, not logic. `calculator.py`:

- `budget_ceiling = clean_value × target_ratio` (default 0.50 — the 40–60% rebuild rule)
- `find_max_bid()` binary-searches in `$25` steps for the largest bid where
  `bid + fees + repair + transport + contingencies ≤ ceiling`. Fees are non-linear (tiered), so
  this is a search, not algebra.
- `rebuilt_resale = clean_value × rebuilt_factor` (default 0.70); `margin` and a `verdict`
  (`strong` / `marginal` / `over_budget` / `losing` / `set_clean_value` / `no_bid_set`) drive the UI.

`car_metrics()` is called on every car in every response via `_with_metrics()` in `main.py` —
metrics are always derived, never stored.

`fee_breakdown()` **raises `ValueError` on an unknown platform**, and `platform` arrives from client
payloads unvalidated, so a bad platform string surfaces as a 500.

## Gotchas

- **Env vars are read at import time** into module-level constants (`apibara.API_KEY`,
  `scanner.MC_API_KEY`, `POLL_INTERVAL_HOURS`, …). `main.py` calls `load_dotenv()` *before*
  importing `app.*` — that import order is load-bearing, hence the `# noqa: E402` block. Changing
  `.env` requires a server restart; tests must set env before import.
- **`POLL_INTERVAL_HOURS` defaults disagree**: `scanner.py` defaults to `12`, the docstring and
  `.env.example` say `24`. The code wins.
- The poller task only starts if `scanner.is_configured()` at startup, and it sleeps
  `POLL_INTERVAL_HOURS` between cycles with no persistence — restarting the server restarts the
  clock and burns quota.
- `/api/searches/run` guards on `scanner.is_configured()` but its 400 message says
  "set MARKETCHECK_API_KEY", which is misleading when Apibara is the active source.
- `CarPayload`/`SearchPayload` are all-optional and serialized with `exclude_none=True`, and
  `db._clean_payload` skips `None` — so **PATCH can never clear a field back to null**, only
  overwrite with a value. Same mechanism silently drops an invalid `status`.
- DB migrations are ad-hoc: `db._CAR_MIGRATIONS` adds missing columns via `ALTER TABLE` in
  `init_db()`. New columns on `cars` must be added there *and* to `CAR_FIELDS` to be writable.
- `contingencies` is a JSON blob in a TEXT column; `db._row_to_dict` parses it out, `_clean_payload`
  re-serializes. Contingency *keys* live in `fees.CONTINGENCY_COSTS` — amounts are edited there,
  per-car booleans are stored on the car.
- Damage-string → `damage_type` code mapping is duplicated three times with different needle lists
  (`apibara._DAMAGE_MAP`, `copart_csv._DAMAGE_MAP`, `auction_api._DAMAGE_ALIASES`). Changing
  categories means touching all three.
- Copart CSV headers drift, so `copart_csv` matches normalized headers against candidate lists and
  hunts for the header row in the first 10 lines. Import matches rows against **active saved
  searches only** and returns an error if there are none.

## Domain notes

All fee figures are estimates from public 2026 schedules; IAAI's public buyer fee is an
*approximation* (Copart tier + 1%) because IAAI doesn't publish one. Every auction bid is a binding
contract (Copart relist penalty max($600, 10%), IAAI max($1,000, 15%)), so surfacing "these are
estimates, verify before bidding" in any user-facing output is intentional, not boilerplate.
