"""
Apibara live test — prior auction runs for ONE vehicle.

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_history01.py

Budget: 1-2 calls of your 100/mo. Tries the VIN first; only if that 404s does
it retry with the lot number. Stops at the first success.

Why this exists: the /vehicles search payload carries only a single
`last_sold_*` snapshot (the most recent run) — no prior attempts. The OpenAPI
spec documents GET /vehicles/{slugVin}/history as "auction history records for
a vehicle by VIN or lot number". A first attempt using the record's `slug_vin`
field returned an nginx 404, which suggests the path wants the bare VIN or lot
number rather than the slug. This isolates that question.

Reads APIBARA_API_KEY from the repo-root .env and saves the full raw JSON to
test_run/apibara_history_results01.json.
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

# 2018 Audi S5 — stat.vin shows 4 IAAI runs: 07-08 $6,500 no-sale,
# 07-11 $8,450 no-sale, 07-22 $7,100 no-sale, then sold 07-25 $7,850.
VIN = "WAUC4CF54JA058014"
LOT = "44948246"
SLUG = "2018-audi-s5-30t-premium-plus-WAUC4CF54JA058014"

# identifiers to try, in order; stops at the first HTTP 200
CANDIDATES = [("vin", VIN), ("lot_number", LOT)]


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


def get(api_key, path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error_body": e.read().decode("utf-8", "replace")[:500]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    out = {"vin": VIN, "lot": LOT, "slug_vin_already_tried_404": SLUG,
           "attempts": []}
    calls = 0

    print("=" * 74)
    print(f"Auction history for {VIN} (lot {LOT})")
    print("Expecting 4 runs if history is exposed: 07-08, 07-11, 07-22, 07-25")
    print("=" * 74)

    for kind, ident in CANDIDATES:
        if calls:
            time.sleep(RATE_DELAY)
        path = f"/vehicles/{urllib.parse.quote(str(ident), safe='')}/history"
        code, data = get(api_key, path)
        calls += 1
        print(f"\n  GET {path}  ->  HTTP {code}   (tried as {kind})")
        out["attempts"].append({"kind": kind, "ident": ident, "path": path,
                                "status": code, "raw": data})
        if code == 200:
            print("  " + json.dumps(data, indent=2)[:2500].replace("\n", "\n  "))
            rows = data.get("data") if isinstance(data, dict) else data
            if isinstance(rows, list):
                print(f"\n  -> {len(rows)} history row(s) returned.")
            break
        print("  " + json.dumps(data)[:300])
        if code in (401, 403):
            print("  -> auth/plan issue, not a path issue. Stopping.")
            break

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_history_results01.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 74)
    print(f"Done. {calls} API call(s) used. Raw JSON saved to {out_path}")


if __name__ == "__main__":
    main()
