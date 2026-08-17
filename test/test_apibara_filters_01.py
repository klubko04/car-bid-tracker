"""
Apibara live test — are BODY STYLE and SELLER TYPE filterable SERVER-SIDE?

Run (from anywhere; paths resolve off this file's location):
    python test/test_apibara_filters_01.py

Budget: up to 5 calls of your 100/mo — one per enabled entry in RUN below.
Set any entry to False to skip it.

WHY
---
analytics/scripts/pull_apibara_01.py filters body style and seller class
CLIENT-side, which burns page slots: an excluded lot still consumes one of the
20 rows a call returns. https://apibara.tech/llms-full.txt lists two params
that were never tried and would move both filters server-side:

    type          "Use values from /vehicles/filters" — docs never enumerate it.
                  Prior evidence says this is a vehicle CLASS, not a body style:
                  the response field `type` is "AUTOMOBILE" on 100% of records
                  observed. TEST 1 settles what values actually exist.
    seller_type   documented enum: dealer | finance | insurance | non_insurance
                  Four buckets — richer than the two that IAAI records expose
                  (seller.type is only ever "insurance" or "unknown" there).
                  If the SERVER knows a lot is `dealer` or `finance` while the
                  RESPONSE says "unknown", the filter beats the payload and the
                  insurance/dealer/other split becomes exact instead of inferred.

CONTROL SET
-----------
Every /vehicles probe reuses the exact PARAMS of test_apibara_sold_iaai_01.py,
whose 15 lots are already on disk in test_run/apibara_sold_iaai_01.json. That
baseline is the answer key:

    body_style   12 Sedan/Hatchback · 2 Coupe · 1 Convertible
    seller       10 seller.type="insurance" · 5 seller.type="unknown", of which
                 "Turo Inc" and "Alamo Financial Group" are the third-party
                 sellers the "other" bucket is meant to catch, and 3 are nameless

So each probe has a known-correct expected result, printed next to what came
back. A filter that returns all 15 is being IGNORED, not applied — that is the
failure mode to watch for, and it is why every probe diffs against the baseline
instead of just counting rows.

Reads APIBARA_API_KEY from the repo-root .env; saves raw JSON to
test_run/apibara_filters_01.json.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "test_run"
BASELINE = OUT_DIR / "apibara_sold_iaai_01.json"

BASE = "https://apibara.tech/api/v1/vehicle-auction"
RATE_DELAY = 1.5

# 1 call each. Flip to False to skip, or name the ones you want on the command
# line:  python test/test_apibara_filters_01.py type_automobile seller_finance
RUN = {
    "filters": True,          # enumerate /vehicles/filters
    "type_coupe": True,       # type=COUPE — does `type` mean body style?
    "type_automobile": True,  # type=AUTOMOBILE — control for the above
    "seller_insurance": True,     # seller_type=insurance
    "seller_dealer": True,        # seller_type=dealer
    "seller_non_insurance": True,  # seller_type=non_insurance
    "seller_finance": True,       # seller_type=finance
    "cylinders_6": True,      # cylinders[]=6 — positive control, expect all 15
    "cylinders_4": True,      # cylinders[]=4 — negative control, expect 0
}

# identical to test_apibara_sold_iaai_01.py — the control set
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
        return e.code, {"error_body": e.read().decode("utf-8", "replace")[:800]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def load_baseline():
    """-> {lot_number: {body_style, seller_type, seller_name}} from _01's pull."""
    if not BASELINE.exists():
        return {}
    d = json.loads(BASELINE.read_text(encoding="utf-8"))
    out = {}
    for p in d.get("pages", []):
        if p.get("status") != 200:
            continue
        for v in (p.get("raw", {}).get("data") or []):
            out[v.get("lot_number")] = {
                "body_style": (v.get("vehicle_specs") or {}).get("body_style"),
                "seller_type": (v.get("seller") or {}).get("type"),
                "seller_name": (v.get("seller") or {}).get("name"),
            }
    return out


def summarize(rows, base, label, expect_lots=None):
    """Print what came back and diff it against the baseline answer key."""
    got = {v.get("lot_number") for v in rows}
    print(f"\n  {label}: {len(rows)} lot(s) returned")

    for v in rows:
        bs = (v.get("vehicle_specs") or {}).get("body_style") or "—"
        s = v.get("seller") or {}
        print(f"      {str(v.get('lot_number')):>10s}  {bs:<18s} "
              f"seller.type={str(s.get('type') or '—'):<14s} {s.get('name') or '—'}")

    if base:
        missing = set(base) - got
        print(f"      vs baseline({len(base)}): kept {len(got & set(base))}, "
              f"dropped {len(missing)}, new {len(got - set(base))}")
        if len(got) == len(base):
            print("      *** returned the FULL baseline -> the filter matched "
                  "everything. Either it was ignored, or the value is one every "
                  "control lot genuinely has — check which against the answer "
                  "key printed in the header. ***")
    if expect_lots is not None:
        exact = got == set(expect_lots)
        print(f"      expected exactly {sorted(expect_lots)} -> "
              f"{'MATCH' if exact else 'MISMATCH'}")
    return got


def main():
    if len(sys.argv) > 1:                      # run only the named tests
        wanted = set(sys.argv[1:])
        bad = wanted - set(RUN)
        if bad:
            raise SystemExit(f"unknown test(s) {sorted(bad)}; "
                             f"choose from {sorted(RUN)}")
        for k in RUN:
            RUN[k] = k in wanted

    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    base = load_baseline()
    coupes = [k for k, v in base.items() if v["body_style"] == "Coupe"]
    insured = [k for k, v in base.items() if v["seller_type"] == "insurance"]
    unknown = [k for k, v in base.items() if v["seller_type"] == "unknown"]
    named_unknown = [k for k in unknown if (base[k]["seller_name"] or "unknown")
                     .lower() != "unknown"]

    out = {"params": PARAMS, "run": RUN, "tests": {}}
    calls = 0

    print("=" * 78)
    print("SERVER-SIDE FILTER PROBE — body style (`type`) and `seller_type`")
    print(f"  control set: {PARAMS}")
    if base:
        print(f"  baseline on disk: {len(base)} lot(s) — "
              f"{len(coupes)} Coupe, {len(insured)} insurance, "
              f"{len(unknown)} unknown ({len(named_unknown)} of them named)")
    else:
        print(f"  NO BASELINE at {BASELINE} — diffs will be skipped")
    print("=" * 78)

    # ---- TEST 1: what values does `type` actually take? -------------------
    type_values = []
    if RUN["filters"]:
        print("\n[1] GET /vehicles/filters — enumerate `type` and `seller_type`")
        code, data = get(api_key, "/vehicles/filters")
        calls += 1
        out["tests"]["filters"] = {"status": code, "raw": data}
        if code != 200:
            print(f"    HTTP {code}: {json.dumps(data)[:300]}")
        else:
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            print(f"    top-level keys: {sorted(payload.keys())[:40]}")
            for key in ("types", "type", "vehicle_types", "body_styles",
                        "seller_types", "seller_type"):
                if key in payload:
                    val = payload[key]
                    print(f"    {key}: {json.dumps(val)[:600]}")
                    if key in ("types", "type", "vehicle_types", "body_styles"):
                        if isinstance(val, list):
                            type_values = [
                                (x.get("value") or x.get("name") or x.get("label"))
                                if isinstance(x, dict) else x for x in val]
        time.sleep(RATE_DELAY)

    # ---- TEST 2: does `type` filter body style? ---------------------------
    if RUN["type_coupe"]:
        candidate = next((t for t in type_values
                          if "coupe" in str(t).lower()), None)
        print(f"\n[2] GET /vehicles with type=<coupe> "
              f"(candidate from TEST 1: {candidate!r})")
        if candidate is None:
            candidate = "Coupe"
            print("    no coupe-like value in /vehicles/filters — trying the "
                  "literal 'Coupe' anyway, to see if it 422s or is ignored")
        params = {**PARAMS, "type": candidate}
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        out["tests"]["type_coupe"] = {"params": params, "status": code,
                                      "raw": data}
        if code != 200:
            print(f"    HTTP {code}: {json.dumps(data)[:300]}")
            print("    -> `type` REJECTS this value; not a body-style filter")
        else:
            summarize(data.get("data") or [], base,
                      f"type={candidate}", expect_lots=coupes or None)
        time.sleep(RATE_DELAY)

    # ---- control: type=AUTOMOBILE is what every record actually carries ----
    if RUN["type_automobile"]:
        print("\n[*] GET /vehicles with type=AUTOMOBILE — control for TEST 2")
        print("    all 15 baseline records carry type='AUTOMOBILE'. If this "
              "returns 15, `type` filters the vehicle CLASS field and simply "
              "has no body-style meaning. If it returns 0 too, `type` is "
              "broken rather than orthogonal.")
        params = {**PARAMS, "type": "AUTOMOBILE"}
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        out["tests"]["type_automobile"] = {"params": params, "status": code,
                                           "raw": data}
        if code != 200:
            print(f"    HTTP {code}: {json.dumps(data)[:300]}")
        else:
            summarize(data.get("data") or [], base, "type=AUTOMOBILE")
        time.sleep(RATE_DELAY)

    # ---- does `seller_type` filter, and how many buckets? -----------------
    for key, value in (("seller_insurance", "insurance"),
                       ("seller_dealer", "dealer"),
                       ("seller_non_insurance", "non_insurance"),
                       ("seller_finance", "finance")):
        if not RUN[key]:
            continue
        expect = insured if value == "insurance" else None
        print(f"\n[*] GET /vehicles with seller_type={value}")
        params = {**PARAMS, "seller_type": value}
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        out["tests"][key] = {"params": params, "status": code, "raw": data}
        if code != 200:
            print(f"    HTTP {code}: {json.dumps(data)[:300]}")
        else:
            rows = data.get("data") or []
            summarize(rows, base, f"seller_type={value}", expect_lots=expect)
            if value != "insurance" and rows:
                hits = [v.get("lot_number") for v in rows
                        if v.get("lot_number") in named_unknown]
                print(f"      of the {len(named_unknown)} named-but-untyped "
                      f"baseline lots (Turo / Alamo), this returned: "
                      f"{hits or 'none'}")
        time.sleep(RATE_DELAY)

    # ---- does cylinders[] work, and is the bracket syntax right? ----------
    # Every control lot is a 3.0L V-6, so 6 must return all 15 and 4 must return
    # none. Testing both directions separates "filter works" from "param name
    # rejected/ignored" — a single 0-row result cannot tell those apart, which
    # is exactly the trap `type=COUPE` fell into above.
    for key, n, expect_all in (("cylinders_6", 6, True), ("cylinders_4", 4, False)):
        if not RUN[key]:
            continue
        print(f"\n[*] GET /vehicles with cylinders[]={n} "
              f"(expect {'all 15' if expect_all else '0'} — every control lot "
              f"is a V-6)")
        params = {**PARAMS, "cylinders[]": n}
        code, data = get(api_key, "/vehicles", params)
        calls += 1
        out["tests"][key] = {"params": params, "status": code, "raw": data}
        if code != 200:
            print(f"    HTTP {code}: {json.dumps(data)[:300]}")
            print("    -> bracket syntax rejected; send `cylinders` instead")
        else:
            rows = data.get("data") or []
            summarize(rows, base, f"cylinders[]={n}")
            ok = (len(rows) == len(base)) if expect_all else (len(rows) == 0)
            print(f"      -> {'AS EXPECTED' if ok else 'UNEXPECTED'}")
        time.sleep(RATE_DELAY)

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "apibara_filters_01.json"
    out["calls_used"] = calls
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {calls} API call(s) used. Raw JSON -> {out_path}")


if __name__ == "__main__":
    main()
