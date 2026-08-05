"""
Apibara live test — can Copart close the gaps IAAI's `details` block fills?

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_sold_copart_02.py

Budget: up to 3 calls of your 100/mo — one per enabled test in RUN below.
Set any entry to False to skip it.

BACKGROUND
----------
Copart search records carry `details: None` (20/20 observed), which removes
ACV, estimated repair cost, storage coordinates, branch id, bidder counts and
bid increment in one go. IAAI carries all of it. These tests probe whether the
same data is reachable through *other* endpoints.

TEST 1 — /vehicles/{VIN}/history          (is prior-run history platform-agnostic?)
    Confirmed working for IAAI (4 runs returned for WAUC4CF54JA058014).
    The path param is documented as "VIN or lot number" with no platform
    qualifier, so it *should* work for Copart. Untested until now.
    Falls back to the lot number if the VIN 404s.

TEST 2 — /vehicles/{VIN}                  (is the search payload merely TRIMMED?)
    The single-lot detail endpoint may return fields the paginated search omits.
    If `details` / `facility` come back populated here, the Copart gap is a
    serialisation artifact of /vehicles, not missing data — which would change
    the whole workaround strategy.

TEST 3 — /locations?platform=copart       (branch table + join feasibility)
    Returns platform, facility_id, name, city, state, zip, latitude, longitude.
    Copart lots expose NO branch id (`facility.id` null 20/20), so the only
    join key is the display string, e.g. "Peoria (IL)" -> locations.name.
    This test pulls the table and scores the join against the 17 distinct
    location strings seen in apibara_sold_copart_01.json, including awkward
    ones like "Philadelphia East-sublot (PA)".

Reads APIBARA_API_KEY from the repo-root .env; saves raw JSON to
test_run/apibara_sold_copart_02.json.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "test_run"
BASELINE = OUT_DIR / "apibara_sold_copart_01.json"

BASE = "https://apibara.tech/api/v1/vehicle-auction"
RATE_DELAY = 1.5

RUN = {"history": True, "detail": True, "locations": True}   # 1 call each

# Copart lot from apibara_sold_copart_01.json — sold $2,000, Peoria (IL)
TEST_VIN = "WAUC4CF54JA050754"
TEST_LOT = "47624636"


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


def baseline_locations():
    """Distinct location.display strings from the _01 pull."""
    if not BASELINE.exists():
        return []
    d = json.loads(BASELINE.read_text(encoding="utf-8"))
    return sorted({(v.get("location") or {}).get("display")
                   for p in d.get("pages", []) if p.get("status") == 200
                   for v in (p.get("raw", {}).get("data") or [])
                   if (v.get("location") or {}).get("display")})


def norm_branch(s):
    """'Philadelphia East-sublot (PA)' -> ('philadelphia east', 'PA')."""
    s = s or ""
    m = re.search(r"\(([A-Z]{2})\)\s*$", s)
    state = m.group(1) if m else ""
    name = re.sub(r"\s*\([A-Z]{2}\)\s*$", "", s)
    name = re.sub(r"[-\s]*sublot\b", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip(), state


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    out = {"vin": TEST_VIN, "lot": TEST_LOT, "run": RUN, "tests": {}}
    calls = 0

    # ---------------------------------------------------------------- TEST 1
    if RUN["history"]:
        print("=" * 78)
        print(f"TEST 1 — /vehicles/{{id}}/history for a COPART lot "
              f"(vin {TEST_VIN}, lot {TEST_LOT})")
        print("=" * 78)
        for kind, ident in (("vin", TEST_VIN), ("lot_number", TEST_LOT)):
            if calls:
                time.sleep(RATE_DELAY)
            code, data = get(api_key, f"/vehicles/{urllib.parse.quote(ident, safe='')}/history")
            calls += 1
            print(f"\n  GET /vehicles/{ident}/history -> HTTP {code}  (as {kind})")
            out["tests"].setdefault("history", []).append(
                {"kind": kind, "ident": ident, "status": code, "raw": data})
            if code == 200:
                rows = ((data.get("data") or {}).get("history")
                        if isinstance(data.get("data"), dict) else data.get("data")) or []
                print(f"  -> {len(rows)} history row(s)")
                for r in rows:
                    print(f"       {r.get('date')}  ${r.get('price'):>9,}  "
                          f"{r.get('status')}  [{r.get('platform')}]")
                print("\n  VERDICT: /history IS platform-agnostic — prior runs available "
                      "for Copart.")
                break
            print("  " + json.dumps(data)[:200])
            if code in (401, 403):
                print("  -> auth/plan issue, not a path issue. Stopping.")
                break
        else:
            print("\n  VERDICT: no history for this Copart lot via either identifier.")

    # ---------------------------------------------------------------- TEST 2
    if RUN["detail"]:
        if calls:
            time.sleep(RATE_DELAY)
        print("\n" + "=" * 78)
        print(f"TEST 2 — /vehicles/{TEST_VIN}  (is the search payload merely trimmed?)")
        print("=" * 78)
        code, data = get(api_key, f"/vehicles/{urllib.parse.quote(TEST_VIN, safe='')}")
        calls += 1
        out["tests"]["detail"] = {"status": code, "raw": data}
        print(f"\n  HTTP {code}")
        if code == 200:
            rec = data.get("data", data)
            if isinstance(rec, list):
                rec = rec[0] if rec else {}
            det = rec.get("details")
            fac = rec.get("facility") or {}
            specs = rec.get("vehicle_specs") or {}
            cond = rec.get("condition") or {}
            print(f"  details:            {type(det).__name__}"
                  f"{' (keys: ' + ', '.join(sorted(det)) + ')' if isinstance(det, dict) and det else ''}")
            print(f"  facility.lat/lng:   {fac.get('lat')} / {fac.get('lng')}   zip={fac.get('zip')}")
            print(f"  vehicle_specs.body_style: {specs.get('body_style')!r}")
            print(f"  condition.secondary_damage: {cond.get('secondary_damage')!r}")
            rich = bool(det) or fac.get("lat") is not None or specs.get("body_style")
            print("\n  VERDICT: " + (
                "detail endpoint returns MORE than search — the Copart gap is a\n"
                "           serialisation artifact of /vehicles. Worth 1 call per car."
                if rich else
                "detail endpoint returns the SAME trimmed payload — the Copart gap\n"
                "           is genuinely missing upstream data, not a serialisation choice."))
        else:
            print("  " + json.dumps(data)[:200])

    # ---------------------------------------------------------------- TEST 3
    if RUN["locations"]:
        if calls:
            time.sleep(RATE_DELAY)
        print("\n" + "=" * 78)
        print("TEST 3 — /locations?platform=copart  (branch table + join feasibility)")
        print("=" * 78)
        code, data = get(api_key, "/locations", {"platform": "copart", "per_page": 100})
        calls += 1
        out["tests"]["locations"] = {"status": code, "raw": data}
        print(f"\n  HTTP {code}")
        if code == 200:
            rows = data.get("data") or []
            print(f"  {len(rows)} location(s) returned; "
                  f"more pages: {bool((data.get('meta') or {}).get('next_cursor'))}")
            if rows:
                print(f"  sample row: {json.dumps(rows[0])[:300]}")
                have_geo = sum(1 for r in rows if r.get("latitude") is not None)
                print(f"  rows with latitude: {have_geo}/{len(rows)}")

                idx = {}
                for r in rows:
                    idx[norm_branch(f"{r.get('name')} ({r.get('state')})")] = r
                targets = baseline_locations()
                print(f"\n  join test against {len(targets)} distinct location.display strings:")
                hit = 0
                for t in targets:
                    key = norm_branch(t)
                    r = idx.get(key)
                    if r:
                        hit += 1
                        print(f"    OK    {t:34s} -> {r.get('name')} "
                              f"({r.get('zip')}) {r.get('latitude')},{r.get('longitude')}")
                    else:
                        print(f"    MISS  {t:34s} -> no match on {key}")
                out["tests"]["locations"]["join_hits"] = hit
                out["tests"]["locations"]["join_total"] = len(targets)
                print(f"\n  VERDICT: {hit}/{len(targets)} joined by normalised name+state.")
                if targets and hit == len(targets):
                    print("           Name-string join is viable — build the branch table once "
                          "and cache.")
                elif hit:
                    print("           Partial join. Unmatched branches need manual aliases or "
                          "a zip fallback.")
                else:
                    print("           Join failed — /locations names do not line up with "
                          "location.display.")
        else:
            print("  " + json.dumps(data)[:300])

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_sold_copart_02.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {calls} API call(s) used. Raw JSON -> {out_path}")


if __name__ == "__main__":
    main()
