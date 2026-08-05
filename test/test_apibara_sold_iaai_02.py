"""
Apibara live test — CONTROL TEST for server-side proximity filtering.

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_sold_iaai_02.py

Budget: 1 call of your 100/mo.

WHAT THIS TESTS
---------------
Identical to test_apibara_sold_iaai_01.py except for three added server-side
params:  zip=98003  radius=3000  units=mi

3000 miles covers the entire lower 48 from Federal Way, WA — so the CORRECT
answer is the same ~15 lots that _01 returned. Any shortfall is the finding.

The question being settled: `zip`+`radius` filtering is computed on Apibara's
side against its own `facility` table (proven — `facility.id` equals IAAI's
`StorageLocationBranch`, coords match to the decimal, `facility.zip` exists
where IAAI provides none, and Copart records carry `facility` despite having
no `details` block at all). But `facility.lat` is populated on only **3 of 75**
observed records. If the radius filter joins against that same sparse data, it
will silently drop lots that genuinely are in range.

  same ~15 lots, `distance` populated  -> index is complete, the response field
                                          is just lazily serialised; server-side
                                          filtering is safe to rely on.
  only 1-3 lots                        -> the join really is sparse; `zip`+
                                          `radius` is unusable and client-side
                                          math on details.attributes.StorageLocation*
                                          (IAAI-only) plus /locations for Copart
                                          is the only option.

Also reports whether the top-level `distance` field populates once a zip is
supplied — it was null on all 75 records pulled so far, because no search had
ever passed one.

Reads APIBARA_API_KEY from the repo-root .env; saves raw JSON to
test_run/apibara_sold_iaai_02.json and diffs against
test_run/apibara_sold_iaai_01.json when that file exists.
"""
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "test_run"
BASELINE = OUT_DIR / "apibara_sold_iaai_01.json"

BASE = "https://apibara.tech/api/v1/vehicle-auction"

# Federal Way, WA 98003 — transport destination
DEST_ZIP = "98003"
DEST_LAT, DEST_LNG = 47.3223, -122.3126
ROAD_FACTOR = 1.2          # great-circle -> rough road miles (US long-haul)

# --- server-side filters: _01's PARAMS + the three proximity params ---------
PARAMS = {
    "platform": "iaai",
    "make": "Audi",
    "model": "S5",
    "year_from": 2018,
    "year_to": 2023,
    "lot_sub_status": "Ended",
    "auction_date_from": "2026-04-20",
    "auction_date_to": "2026-05-11",
    "per_page": 20,
    # --- the three under test ---
    "zip": DEST_ZIP,
    "radius": 3000,
    "units": "mi",
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


def haversine_mi(lat, lng):
    R = 3958.8
    p1, l1, p2, l2 = map(math.radians, (DEST_LAT, DEST_LNG, lat, lng))
    return 2 * R * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))


def coords(v):
    """(lat, lng, source) from whichever layer has them."""
    at = (v.get("details") or {}).get("attributes") or {}
    la, lo = at.get("StorageLocationLatitude"), at.get("StorageLocationLongitude")
    if la and lo:
        return float(la), float(lo), "details.attributes"
    f = v.get("facility") or {}
    if f.get("lat") is not None and f.get("lng") is not None:
        return float(f["lat"]), float(f["lng"]), "facility"
    return None, None, "none"


def baseline_lots():
    if not BASELINE.exists():
        return None
    d = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {v.get("lot_number") for p in d.get("pages", [])
            if p.get("status") == 200
            for v in (p.get("raw", {}).get("data") or [])}


def main():
    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    print("=" * 78)
    print(f"CONTROL TEST — server-side proximity filter (zip={DEST_ZIP}, "
          f"radius=3000 mi)")
    print(f"  {PARAMS}")
    print("=" * 78)

    code, data = get(api_key, "/vehicles", PARAMS)
    out = {"params": PARAMS, "dest_zip": DEST_ZIP, "status": code, "raw": data}

    if code != 200:
        print(f"\n  HTTP {code}: {json.dumps(data)[:400]}")
        if code == 422:
            print("  -> 422 means one of zip/radius/units was rejected.")
        _save(out)
        return

    rows = data.get("data") or []
    cursor = (data.get("meta") or {}).get("next_cursor")
    dist_set = sum(1 for v in rows if v.get("distance") is not None)
    fac_set = sum(1 for v in rows if (v.get("facility") or {}).get("lat") is not None)

    print(f"\n  returned {len(rows)} lot(s)   (more pages: {bool(cursor)})")
    print(f"  top-level `distance` populated: {dist_set}/{len(rows)}")
    print(f"  `facility.lat` populated:       {fac_set}/{len(rows)}\n")

    print(f"  {'lot':>10s} {'branch':>7s} {'API dist':>9s} {'haversine':>10s} "
          f"{'~road':>7s}  source            location")
    for v in sorted(rows, key=lambda x: haversine_mi(*coords(x)[:2])
                    if coords(x)[0] is not None else 9e9):
        at = (v.get("details") or {}).get("attributes") or {}
        la, lo, src = coords(v)
        hav = f"{haversine_mi(la, lo):8.0f}" if la is not None else "       —"
        road = f"{haversine_mi(la, lo) * ROAD_FACTOR:6.0f}" if la is not None else "     —"
        api_d = v.get("distance")
        print(f"  {str(v.get('lot_number')):>10s} "
              f"{str(at.get('StorageLocationBranch') or (v.get('facility') or {}).get('id') or '—'):>7s} "
              f"{str(api_d if api_d is not None else '—'):>9s} {hav} mi {road} mi  "
              f"{src:17s} {(v.get('location') or {}).get('display') or '—'}")

    base = baseline_lots()
    print()
    if base is None:
        print(f"  (no baseline at {BASELINE} — run test_apibara_sold_iaai_01.py "
              f"to enable the diff)")
    else:
        got = {v.get("lot_number") for v in rows}
        missing, extra = base - got, got - base
        print(f"  BASELINE (_01, no proximity params): {len(base)} lot(s)")
        print(f"  THIS RUN (radius=3000 mi):           {len(got)} lot(s)")
        if missing:
            print(f"  DROPPED by the filter ({len(missing)}): {sorted(missing)}")
        if extra:
            print(f"  NEW vs baseline ({len(extra)}): {sorted(extra)}")
        out["baseline_count"] = len(base)
        out["dropped"] = sorted(missing)
        out["extra"] = sorted(extra)

        print("\n  VERDICT:")
        if not missing:
            print("    No lots dropped -> Apibara's geo index is complete; the sparse")
            print("    `facility` block in responses is only lazy serialisation.")
            print("    Server-side zip+radius filtering is SAFE to rely on.")
        elif len(got) <= 3:
            print("    Nearly everything dropped -> the radius filter joins against the")
            print("    same sparse facility data seen in responses. zip+radius is")
            print("    UNUSABLE; use client-side math on StorageLocation* (IAAI only)")
            print("    plus /locations for Copart.")
        else:
            print(f"    Partial loss ({len(missing)} of {len(base)}) -> the join is")
            print("    incomplete. Server-side filtering silently hides lots; treat any")
            print("    proximity-filtered result as a floor, not a complete set.")

    _save(out)


def _save(out):
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_sold_iaai_02.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. 1 API call used. Raw JSON -> {out_path}")


if __name__ == "__main__":
    main()
