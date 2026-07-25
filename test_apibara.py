"""
Apibara live test — 2 searches, 2 API calls total (of your 100/mo).
Standard library only (no httpx/requests/dotenv) — runs on any Python.

Run:
    python test_apibara.py

Reads APIBARA_API_KEY from .env, runs:
  1) Lexus IS 350, 2016-2020
  2) Audi A4,      2016-2020
prints what each lot exposes (live bid, damage, title, odometer, etc.),
and saves the full raw JSON to apibara_test_results.json.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://apibara.tech/api/v1/vehicle-auction"
PER_PAGE = 10  # results per search; still 1 call regardless (max 20)

SEARCHES = [
    {"label": "Lexus IS 350 (2016-2020)",
     "params": {"make": "Lexus", "model": "IS 350", "year_from": 2016, "year_to": 2020}},
    {"label": "Audi A4 (2016-2020)",
     "params": {"make": "Audi", "model": "A4", "year_from": 2016, "year_to": 2020}},
]


def read_env_key(path=".env", name="APIBARA_API_KEY"):
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


def show_vehicle(v, i):
    pricing = v.get("pricing") or {}
    auction = v.get("auction") or {}
    cond = v.get("condition") or {}
    odo = v.get("odometer") or {}
    loc = v.get("location") or {}
    media = v.get("media") or {}
    sd = v.get("sale_document") or {}
    seller = v.get("seller") or {}

    print(f"  [{i}] {v.get('year','?')} {v.get('make','')} {v.get('model','')}"
          f"  ({v.get('platform','?')}  lot {v.get('lot_number','?')})")
    print(f"      VIN:       {v.get('vin') or '—'}")
    print(f"      LIVE BID:  {money(pricing.get('current_bid_usd') or pricing.get('current_bid'))}"
          f"     Buy-Now: {money(pricing.get('buy_now_usd'))}"
          f"     Last sold: {money(pricing.get('last_sold_usd') or pricing.get('last_sold_price'))}")
    print(f"      Auction:   {auction.get('state','?')}  at {auction.get('auction_at') or auction.get('auction_date') or '—'}")
    print(f"      Damage:    {cond.get('primary_damage') or '—'}"
          f"  / 2nd: {cond.get('secondary_damage') or '—'}"
          f"  | loss: {cond.get('loss_type') or '—'}"
          f"  | runs: {cond.get('run_cond') or cond.get('running_condition') or '—'}"
          f"  | keys: {cond.get('has_key')}")
    print(f"      Title/doc: {sd.get('type') or sd.get('name') or sd.get('normalized_type') or '—'}")
    print(f"      Odometer:  {odo.get('mi','?')} mi")
    print(f"      Location:  {loc.get('display') or '—'}   seller: {seller.get('name') or seller.get('type') or '—'}")
    print(f"      Media:     {media.get('thumbs_count') or media.get('image_count') or 0} photos"
          f"  video: {media.get('has_video')}")


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit("No APIBARA_API_KEY found in .env")

    out = {"searches": []}
    calls = 0
    for s in SEARCHES:
        print("\n" + "=" * 70)
        print(s["label"])
        print("=" * 70)
        params = {**s["params"], "per_page": PER_PAGE, "lot_status": "All"}
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        if code != 200:
            print(f"  HTTP {code}: {json.dumps(data)[:400]}")
            out["searches"].append({"label": s["label"], "status": code, "body": data})
            continue
        vehicles = data.get("data") or []
        nxt = (data.get("meta") or {}).get("next_cursor")
        print(f"  Returned {len(vehicles)} lot(s) on this page  (more available: {bool(nxt)})\n")
        for i, v in enumerate(vehicles, 1):
            show_vehicle(v, i)
        out["searches"].append({"label": s["label"], "params": params, "raw": data})

    with open("apibara_test_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 70)
    print(f"Done. {calls} API call(s) used. Raw JSON saved to apibara_test_results.json")
    print("Send me that file and I'll lock the field mapping to the real data.")


if __name__ == "__main__":
    main()
