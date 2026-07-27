"""
Apibara live test — SOLD (ended) COPART lots, with the filters Copart supports.

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_sold_copart_01.py

Budget: 1 call per page, MAX_PAGES pages (default 1) = 1 call of your 100/mo.

FILTER GRANULARITY ON COPART
----------------------------
i)   DATE RANGE   server-side, identical to IAAI. auction_date_from /
                  auction_date_to, YYYY-MM-DD. Platform-agnostic param.
ii)  BODY STYLE   NOT AVAILABLE, at all. No server-side param, and
                  vehicle_specs.body_style is null on 33/33 Copart records
                  observed (it is IAAI-only). The `title` string carries trim
                  ("2018 AUDI S5 PRESTIGE"), never body. So coupe-vs-sedan
                  cannot be filtered on Copart — the only workaround is a
                  model-name split where the manufacturer provides one
                  (Audi S5 = coupe vs S5 Sportback = 4-door).
                  BODY_STYLES below is therefore deliberately absent.
iii) DAMAGE       same server-side limitation as IAAI: `damage` is an
                  INCLUDE-only whitelist whose enum cannot express collision
                  damage, and there is no exclude param. Exclusion is
                  client-side against condition.primary_damage.

Copart damage vocabulary is COARSER than IAAI's — no positional detail.
Observed: Front end / Minor dent-scratches / Rear end / Side / Normal wear /
Hail / Mechanical / All over. Note "Rear end" (Copart) vs "Rear" (IAAI) and
"Side" (Copart) vs "Right side"/"Left rear" (IAAI); needles differ per platform.

Copart records carry NO `details` block, so there is no ActualCashValue and no
EstimatedRepairCost — the two fields that make IAAI records self-pricing.
seller.type is clean here though (insurance / non_insurance, no "unknown").

Reads APIBARA_API_KEY from the repo-root .env; saves raw JSON to
test_run/apibara_sold_copart_01.json.
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
RATE_DELAY = 1.5
MAX_PAGES = 1                      # each page = 1 API call, 20 lots

# --- server-side filters (sent to the API) ---------------------------------
PARAMS = {
    "platform": "copart",
    "make": "Audi",
    "model": "S5",
    "year_from": 2018,
    "year_to": 2023,
    "lot_sub_status": "Ended",     # sold / finished lots
    "auction_date_from": "2026-04-20",   # (i) YYYY-MM-DD
    "auction_date_to": "2026-05-11",
    "per_page": 20,
}

# --- client-side filters (applied to the response) -------------------------
# (ii) no body-style filter is possible on Copart — see module docstring.

# (iii) drop lots whose primary/secondary damage matches any needle.
# Needles tuned to COPART's coarser vocabulary.
EXCLUDE_DAMAGE = ["water", "flood", "burn", "fire", "rollover",
                  "all over", "mechanical"]

# optional: restrict by seller type ("insurance" / "non_insurance"); [] = all
SELLER_TYPES = []


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


def damage_text(v):
    c = v.get("condition") or {}
    return f"{c.get('primary_damage') or ''} {c.get('secondary_damage') or ''}".lower()


def keep(v):
    """Client-side (iii) + seller type. Returns (bool, reason)."""
    hit = next((n for n in EXCLUDE_DAMAGE if n in damage_text(v)), None)
    if hit:
        return False, f"damage~{hit}"
    st = (v.get("seller") or {}).get("type") or ""
    if SELLER_TYPES and st not in SELLER_TYPES:
        return False, f"seller={st or 'null'}"
    return True, ""


def show(v, i):
    p = v.get("pricing") or {}
    a = v.get("auction") or {}
    c = v.get("condition") or {}
    rc = c.get("run_condition")
    run = (rc.get("value") if isinstance(rc, dict) else rc) or "—"

    print(f"  [{i}] {v.get('year','?')} {v.get('make','')} {v.get('model','')}"
          f"  lot {v.get('lot_number','?')}   {v.get('title') or ''}")
    print(f"      SOLD {money(p.get('last_sold_price_usd'))} on {a.get('last_sold_day') or '—'}"
          f"   status={a.get('last_sold_status') or '—'}"
          f"   last bid={money(p.get('current_bid_usd'))}"
          f"   buy_now={money(p.get('buy_now_usd'))}")
    print(f"      Damage:  {c.get('primary_damage') or '—'}"
          f"  / 2nd: {c.get('secondary_damage') or '—'}   runs: {run}")
    print(f"      Odo:     {(v.get('odometer') or {}).get('mi') or '—'} mi"
          f"   loc: {(v.get('location') or {}).get('display') or '—'}")
    print(f"      Seller:  {(v.get('seller') or {}).get('type') or '—'}"
          f"   title: {(v.get('sale_document') or {}).get('name') or '—'}")
    print("      (no ACV / est. repair — Copart records carry no details block)")


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    out = {"platform": "copart", "params": PARAMS,
           "exclude_damage": EXCLUDE_DAMAGE, "seller_types": SELLER_TYPES,
           "pages": []}
    calls, cursor, kept, dropped = 0, None, [], []

    print("=" * 78)
    print("COPART — sold/ended lots")
    print(f"  server-side: {PARAMS}")
    print(f"  client-side: exclude_damage={EXCLUDE_DAMAGE}  "
          f"seller_types={SELLER_TYPES or 'ALL'}")
    print("  NOTE: body-style / coupe-vs-sedan filtering is impossible on Copart.")
    print("=" * 78)

    while calls < MAX_PAGES:
        params = dict(PARAMS)
        if cursor:
            params["cursor"] = cursor
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        out["pages"].append({"status": code, "raw": data})
        if code != 200:
            print(f"\n  HTTP {code}: {json.dumps(data)[:400]}")
            break
        rows = data.get("data") or []
        for v in rows:
            ok, why = keep(v)
            (kept if ok else dropped).append((v, why))
        cursor = (data.get("meta") or {}).get("next_cursor")
        print(f"\n  page {calls}: {len(rows)} lot(s) returned"
              f"   (more pages: {bool(cursor)})")
        if not cursor:
            break
        time.sleep(RATE_DELAY)

    print(f"\n  KEPT {len(kept)}   dropped {len(dropped)} by client-side filters\n")
    for i, (v, _) in enumerate(kept, 1):
        show(v, i)
    if dropped:
        print("\n  --- dropped ---")
        for v, why in dropped:
            print(f"      lot {v.get('lot_number','?'):>10s} {v.get('year')} "
                  f"{v.get('model','')}  ({why})")

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_sold_copart_01.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {calls} API call(s) used. Raw JSON -> {out_path}")


if __name__ == "__main__":
    main()
