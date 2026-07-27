"""
Apibara live test — SOLD lots only. Audi S5 (2018-2023).
Standard library only (no httpx/requests/dotenv) — runs on any Python.

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_sold01.py

Budget: 1 call for the search + up to MAX_HISTORY_LOOKUPS calls for sale
history (default 1) = 2 calls of your 100/mo. Set MAX_HISTORY_LOOKUPS = 0
to spend only 1.

Why two params: `lot_status` only accepts All / Timed / Buy Now — it does NOT
filter sold lots. Ended auctions come from `lot_sub_status=Ended`. Per the
OpenAPI schema the search endpoint is oriented at active auctions and final
sale prices live on /vehicles/{slugVin}/history, so this script does both:
  1) search Ended lots, report which ones carry a final price inline
  2) pull /history for the first sold lot to show what that payload holds

Reads APIBARA_API_KEY from the repo-root .env, prints each lot's sold fields,
and saves the full raw JSON to test_run/apibara_sold_results01.json.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "test_run"

BASE = "https://apibara.tech/api/v1/vehicle-auction"
PER_PAGE = 20            # max on the free plan; still 1 call
RATE_DELAY = 1.5         # free plan is 1 req/sec — stay under it
MAX_HISTORY_LOOKUPS = 1  # extra calls for /history detail; 0 = skip

SEARCH = {
    "label": "Audi S5 (2018-2023) — SOLD / ended lots",
    "params": {
        "make": "Audi",
        "model": "S5",
        "year_from": 2018,
        "year_to": 2023,
        "lot_sub_status": "Ended",
    },
}


def read_env_key(path=ENV_PATH, name="APIBARA_API_KEY"):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) and v else "—"


def get(api_key, path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error_body": e.read().decode("utf-8", "replace")[:500]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def sold_price(v):
    """Final/sold price if the record carries one inline, else None."""
    p = v.get("pricing") or {}
    for k in ("last_sold_price_usd", "sold_price_usd", "final_bid_usd"):
        if p.get(k) is not None:
            return p[k]
    return None


def is_sold(v):
    """Did this lot actually sell? Ended != sold (no-sale / relist exist)."""
    a = v.get("auction") or {}
    return bool(
        sold_price(v) is not None
        or a.get("last_sold_status")
        or a.get("last_sold_day")
        or a.get("sold_buy_now")
        or a.get("sold_timed")
    )


def show_vehicle(v, i):
    pricing = v.get("pricing") or {}
    auction = v.get("auction") or {}
    cond = v.get("condition") or {}
    odo = v.get("odometer") or {}
    loc = v.get("location") or {}
    sd = v.get("sale_document") or {}

    rc = cond.get("run_condition")
    run = (rc.get("value") if isinstance(rc, dict) else rc) or "—"

    print(f"  [{i}] {v.get('year','?')} {v.get('make','')} {v.get('model','')}"
          f"  ({v.get('platform','?')}  lot {v.get('lot_number','?')})"
          f"  {'SOLD' if is_sold(v) else 'ended, no sale evidence'}")
    print(f"      VIN:        {v.get('vin') or '—'}   slug: {v.get('slug_vin') or '—'}")
    print(f"      FINAL/SOLD: {money(sold_price(v))}"
          f"     Last bid: {money(pricing.get('current_bid_usd'))}"
          f"     Buy-Now: {money(pricing.get('buy_now_usd'))}")
    print(f"      Sold meta:  status={auction.get('last_sold_status') or '—'}"
          f"  day={auction.get('last_sold_day') or '—'}"
          f"  buy_now={auction.get('sold_buy_now')}  timed={auction.get('sold_timed')}")
    print(f"      Auction:    state={auction.get('state','?')}"
          f"  at {auction.get('auction_at') or auction.get('full_date') or '—'}")
    print(f"      Damage:     {cond.get('primary_damage') or '—'}"
          f"  / 2nd: {cond.get('secondary_damage') or '—'}"
          f"  | runs: {run}  | keys: {cond.get('has_key')}")
    print(f"      Title/doc:  {sd.get('name') or '—'}  (group: {sd.get('sale_document_group') or '—'})")
    print(f"      Odometer:   {odo.get('mi','?')} mi     Location: {loc.get('display') or '—'}")


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    out = {"search": SEARCH["label"], "params": None, "raw": None, "history": []}
    calls = 0

    print("=" * 74)
    print(SEARCH["label"])
    print("=" * 74)

    params = {**SEARCH["params"], "per_page": PER_PAGE}
    out["params"] = params
    code, data = get(api_key, "/vehicles", params)
    calls += 1
    if code != 200:
        out["status"] = code
        out["body"] = data
        print(f"  HTTP {code}: {json.dumps(data)[:400]}")
        if code == 422:
            print("  -> 422 usually means lot_sub_status=Ended was rejected; "
                  "check the enum in the OpenAPI schema.")
        _save(out, calls)
        return

    out["raw"] = data
    vehicles = data.get("data") or []
    nxt = (data.get("meta") or {}).get("next_cursor")
    sold = [v for v in vehicles if is_sold(v)]
    print(f"  Returned {len(vehicles)} ended lot(s); {len(sold)} carry sold "
          f"evidence  (more pages: {bool(nxt)})\n")
    for i, v in enumerate(vehicles, 1):
        show_vehicle(v, i)

    # /history for the first sold lot — prior auction runs (no-sales included).
    # NB: the path param is documented as {slugVin} but wants a BARE VIN or lot
    # number; passing the record's slug_vin field returns an nginx 404.
    targets = [v for v in (sold or vehicles) if v.get("vin")][:MAX_HISTORY_LOOKUPS]
    for v in targets:
        time.sleep(RATE_DELAY)
        vin = v["vin"]
        code, hist = get(api_key, f"/vehicles/{vin}/history", {})
        calls += 1
        print(f"\n  --- /vehicles/{vin}/history -> HTTP {code} ---")
        print("  " + json.dumps(hist, indent=2)[:1500].replace("\n", "\n  "))
        out["history"].append({"vin": vin, "status": code, "raw": hist})

    _save(out, calls)


def _save(out, calls):
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_sold_results01.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 74)
    print(f"Done. {calls} API call(s) used. Raw JSON saved to {out_path}")


if __name__ == "__main__":
    main()
