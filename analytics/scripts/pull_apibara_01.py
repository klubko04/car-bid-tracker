"""
Apibara pull — stage 1 of the analytics pipeline. RAW JSON ONLY.

    pull_apibara_01.py  ->  raw .json
        iaai   -> apibara_json2csv_iaai_01.py
        copart -> copart_vpic_adapt_01.py -> apibara_json2csv_copart_01.py

This script does exactly two things: build a server-side query, and archive the
untouched responses. It applies NO filtering and derives NO fields — every
judgement call (damage exclusion, body style, seller classification, column
selection) belongs to the json2csv stage, where it can be re-run any number of
times against the saved JSON without spending API calls.

That split matters because of what the data is: Apibara's active database holds
roughly the last ~10 months (docs, 2026-07-27), and an observed Audi S5 IAAI
pull reached only ~5.5 months back. Lots age out. A raw archive pulled today is
the ONLY copy of that window you will ever have, so it must be stored unfiltered
and unreshaped — a filter applied at pull time destroys data permanently, while
a filter applied at convert time costs nothing.

Run (from anywhere; paths resolve off this file's location):

    python analytics/scripts/pull_apibara_01.py iaai ended \
        --make Audi --model S5 \
        --year-range 2018-2023 \
        --auction-date-range 2025-08-01 2026-08-09 \
        --seller insurance \
        --max-pages 20

BUDGET: 1 API call per page, `--max-pages` pages (default 1) of your 100/month.
--max-pages is a CAP, not a target: paging stops as soon as the cursor runs out,
so overestimating is free while underestimating silently truncates the archive.
`--dry-run` prints the request URL and resolved params without spending a call.

DESIGN RULE: EVERY FILTER MIRRORS A SERVER-SIDE PARAM
-----------------------------------------------------
Each CLI filter maps 1:1 onto an Apibara query param and takes that param's own
enum — no invented vocabulary, no client-side approximation. Client-side
filtering would be strictly worse here anyway: a lot rejected locally has
already consumed one of the 20 rows the call returned.

    positional 2   -> lot_sub_status   {open|live|ended}
    --seller       -> seller_type      {insurance|dealer}
    --cylinders    -> cylinders[]      {1,2,3,4,5,6,8,10,12}
    --make/--model -> make/model
    --year-range   -> year_from/year_to
    --auction-date-range -> auction_date_from/auction_date_to

Every enum came from GET /vehicles/filters and was then verified against live
data by test/test_apibara_filters_01.py — worth doing, because a bad value fails
SILENTLY (`type=COUPE` returns 0 rows rather than an error). Raw evidence is in
test_run/apibara_filters_01.json.

NOT AVAILABLE, DELIBERATELY ABSENT
----------------------------------
body style   `type` looks like a body filter and /vehicles/filters advertises
             SEDAN/COUPE/SUV/..., but it matches the vehicle-CLASS field, which
             is "AUTOMOBILE" on every car: type=AUTOMOBILE returned 15/15 of a
             control set, type=COUPE returned 0/15 including the two lots whose
             body_style literally says "Coupe". Filter body style at the
             json2csv stage instead (IAAI populates it on 100% of records).
damage       `damage` is an INCLUDE-only whitelist whose enum (Fire/Hail/Theft/
             Water/Chemical/Rollover/Mechanical/Vandalized/Repossession) cannot
             express the collision damage that is ~80% of lots, and there is no
             negation param. Also a json2csv-stage concern.
seller       only insurance and dealer work. non_insurance is a no-op that
             returns everything; finance returns nothing. Neither is offered.
             The two that work are not complements — omit --seller for all.

Reads APIBARA_API_KEY from the repo-root .env. Writes to
analytics/data/{sold|open}/json-raw/ — the irreplaceable layer: lots age out of
Apibara's ~10-month window, so these archives are the only lasting copy.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # repo root
ENV_PATH = ROOT / ".env"
# analytics/data/{sold|open}/<layer>/{iaai|copart}/
#   json-raw/   <- THIS SCRIPT: untouched API responses, the irreplaceable layer
#   csv-raw/    <- apibara_json2csv_<platform>_01.py: flattened, unfiltered
#   csv-cut/    <- data_pull_01.py: filtered + tier/sold_period
#
# The bucket follows lot_sub_status: `ended` lots are history (sold), while
# `open` and `live` are both still biddable and share the open bucket — the
# filename keeps them apart (apibara_iaai_open_... vs _live_...).
DATA_DIR = ROOT / "analytics" / "data"
MODE_BUCKET = {"ended": "sold", "open": "open", "live": "open"}


def layer_dir(mode, layer, platform):
    """analytics/data/<bucket>/<layer>/<platform>/ for a pull."""
    return DATA_DIR / MODE_BUCKET.get(mode, "sold") / layer / platform


BASE = "https://apibara.tech/api/v1/vehicle-auction"
RATE_DELAY = 1.5                                    # free plan = 1 req/sec

# Enums taken verbatim from GET /vehicles/filters — see the probe artifact
# test_run/apibara_filters_01.json. CLI choices are generated from these, so a
# value this script accepts is always a value the API accepts.
LOT_SUB_STATUS = {"open": "Open", "live": "Live", "ended": "Ended"}
CYLINDERS = {1, 2, 3, 4, 5, 6, 8, 10, 12}          # note: no 7, 9 or 11


# --------------------------------------------------------------------------
# env + transport
# --------------------------------------------------------------------------
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


def build_url(path, params):
    return f"{BASE}{path}?" + urllib.parse.urlencode(params, doseq=True)


def get(api_key, path, params):
    req = urllib.request.Request(build_url(path, params), headers={
        "Accept": "application/json", "X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error_body": e.read().decode("utf-8", "replace")[:500]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def multiword(value):
    """argparse nargs='+' value -> one string.

    Model names contain spaces ("ES 350", "IS 300"), so `--model ES 350` has to
    work without quoting — unquoted it arrives as ['ES', '350'] and, before
    nargs, argparse rejected the '350' as an unrecognised positional.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return " ".join(str(x) for x in value).strip()


def parse_year_range(s):
    m = re.fullmatch(r"\s*(\d{4})\s*(?:[-:]\s*(\d{4}))?\s*", s or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"--year-range wants YYYY-YYYY or YYYY, got {s!r}")
    lo = int(m.group(1))
    hi = int(m.group(2) or m.group(1))
    if hi < lo:
        raise argparse.ArgumentTypeError(f"--year-range {lo}-{hi} is backwards")
    return lo, hi


def parse_date(s):
    try:
        return dt.date.fromisoformat(s.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"dates must be YYYY-MM-DD, got {s!r}") from None


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="pull_apibara_01.py",
        description="Pull raw Apibara auction JSON into data/{sold|open}/json-raw/. "
                    "No filtering, no derived fields — see apibara_json2csv_*.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Each page costs 1 call of the 100/month free plan. "
               "Use --dry-run to see the request without spending one.")
    ap.add_argument("platform", choices=["iaai", "copart"],
                    type=str.lower, help="auction platform")
    ap.add_argument("mode", choices=list(LOT_SUB_STATUS), type=str.lower,
                    help="lot_sub_status: open = biddable, live = auction "
                         "running now, ended = sold/finished")
    ap.add_argument("--make", nargs="+", metavar="MAKE",
                    help="e.g. Lexus. Multi-word values need no quotes")
    ap.add_argument("--model", nargs="+", metavar="MODEL",
                    help='e.g. "ES 350" or ES 350 — both work')
    ap.add_argument("--year-range", type=parse_year_range, metavar="YYYY-YYYY")
    ap.add_argument("--auction-date-range", nargs="+", type=parse_date,
                    metavar="YYYY-MM-DD",
                    help="one date = today..that date; two = from..to")
    ap.add_argument("--seller", choices=["insurance", "dealer"], type=str.lower,
                    help="seller_type. Omit for all sellers; the two values "
                         "are not complements — see module docstring")
    ap.add_argument("--cylinders", action="append", default=[], metavar="N",
                    help=f"cylinders[]; repeatable or comma-separated. "
                         f"Allowed: {','.join(map(str, sorted(CYLINDERS)))}")
    ap.add_argument("--max-pages", type=int, default=1,
                    help="page cap; EACH PAGE IS ONE API CALL (default 1). "
                         "Paging stops early when the cursor runs out")
    ap.add_argument("--per-page", type=int, default=20,
                    help="lots per page, API max 20 (default 20)")
    ap.add_argument("--out", help="output basename or path (default: auto-named)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved request, make no call")
    return ap


def resolve_params(args):
    """CLI args -> the exact query params sent to /vehicles."""
    params = {"platform": args.platform, "per_page": min(args.per_page, 20)}
    if args.make:
        params["make"] = multiword(args.make)
    if args.model:
        params["model"] = multiword(args.model)
    if args.year_range:
        params["year_from"], params["year_to"] = args.year_range
    params["lot_sub_status"] = LOT_SUB_STATUS[args.mode]
    if args.auction_date_range:
        d = args.auction_date_range
        if len(d) > 2:
            raise SystemExit("--auction-date-range takes one or two dates")
        # One date = "between today and that date", in whichever direction it
        # lies: a past date looks backwards, a future date forwards. Both ends
        # are sorted, because an inverted from/to is not an error to the API --
        # it silently matches (almost) nothing.
        lo, hi = sorted([dt.date.today(), d[0]] if len(d) == 1 else list(d))
        params["auction_date_from"] = lo.isoformat()
        params["auction_date_to"] = hi.isoformat()

    cyl = set()
    for raw in args.cylinders:
        for tok in str(raw).split(","):
            tok = tok.strip()
            if not tok:
                continue
            if not tok.isdigit() or int(tok) not in CYLINDERS:
                raise SystemExit(
                    f"--cylinders must be one of "
                    f"{','.join(map(str, sorted(CYLINDERS)))}, got {tok!r}")
            cyl.add(int(tok))
    if cyl:
        params["cylinders[]"] = sorted(cyl)

    if args.seller:
        params["seller_type"] = args.seller
    return params


def default_basename(args, params):
    bits = ["apibara", args.platform, args.mode]
    bits += [multiword(x).lower().replace(" ", "-")
             for x in (args.make, args.model) if x]
    if args.year_range:
        bits.append(f"{args.year_range[0]}-{args.year_range[1]}")
    if "auction_date_from" in params:
        bits.append(f"{params['auction_date_from']}_{params['auction_date_to']}")
    bits.append(dt.datetime.now().strftime("%Y%m%dT%H%M%S"))
    return "_".join(bits)


def resolve_out_path(args, params):
    out_dir = layer_dir(args.mode, "json-raw", args.platform)
    if args.out:
        p = Path(args.out)
        if not p.is_absolute():
            p = out_dir / p
        return p.with_suffix(".json")
    return (out_dir / default_basename(args, params)).with_suffix(".json")


# --------------------------------------------------------------------------
def summarize(records):
    """Coverage report. The date span is the point: it shows how far back the
    archive actually reaches, which is shorter than most requested windows."""
    if not records:
        print("  no records returned")
        return
    days = sorted(filter(None, ((r.get("auction") or {}).get("last_sold_day")
                                or (r.get("ad") or "")[:10] for r in records)))
    sellers, origins, damage = {}, {}, {}
    for r in records:
        s = (r.get("seller") or {}).get("type") or "—"
        sellers[s] = sellers.get(s, 0) + 1
        o = ((r.get("details") or {}).get("attributes") or {}).get("Origin") or "—"
        origins[o] = origins.get(o, 0) + 1
        dmg = (r.get("condition") or {}).get("primary_damage") or "—"
        damage[dmg] = damage.get(dmg, 0) + 1

    print(f"\n  records:      {len(records)}")
    if days:
        print(f"  date span:    {days[0]} .. {days[-1]}")
    print(f"  seller.type:  {dict(sorted(sellers.items(), key=lambda kv: -kv[1]))}")
    if origins != {"—": len(records)}:
        print(f"  seller Origin:{dict(sorted(origins.items(), key=lambda kv: -kv[1]))}"
              f"   <- IAAI's own field; trust this one")
    # seller.type is Apibara's normalisation and it under-reports: it says
    # "unknown" for lots IAAI itself labels Insurance (26% of one observed
    # pull). Flag the gap here so it never again looks like the server-side
    # seller_type filter leaked non-insurance lots.
    unknown = sellers.get("unknown", 0)
    if unknown and origins.get("Insurance"):
        print(f"  note: {unknown} lot(s) have seller.type='unknown' but "
              f"Origin='Insurance' — Apibara's field is incomplete, not the "
              f"filter. json2csv resolves this into seller_class.")
    top = dict(sorted(damage.items(), key=lambda kv: -kv[1])[:6])
    print(f"  top damage:   {top}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be >= 1")

    params = resolve_params(args)
    out_path = resolve_out_path(args, params)

    print("=" * 78)
    print(f"{args.platform.upper()} — lot_sub_status={LOT_SUB_STATUS[args.mode]}")
    print(f"  server-side: {params}")
    print(f"  budget:      up to {args.max_pages} call(s)")
    print("=" * 78)

    if args.dry_run:
        print(f"\n  DRY RUN — no call made.\n  GET {build_url('/vehicles', params)}")
        print(f"  would write -> {out_path}")
        return 0

    api_key = read_env_key()
    if not api_key:
        raise SystemExit(f"No APIBARA_API_KEY found in {ENV_PATH}")

    out = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": argv,
        "platform": args.platform,
        "mode": args.mode,
        "server_params": params,
        "pages": [],
    }
    calls, cursor, records = 0, None, []

    while calls < args.max_pages:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        code, data = get(api_key, "/vehicles", page_params)
        calls += 1
        out["pages"].append({"status": code, "raw": data})
        if code != 200:
            print(f"\n  HTTP {code}: {json.dumps(data)[:400]}")
            break
        rows = data.get("data") or []
        records.extend(rows)
        cursor = (data.get("meta") or {}).get("next_cursor")
        print(f"  page {calls}: {len(rows)} lot(s)   (more pages: {bool(cursor)})")
        if not cursor:
            break
        time.sleep(RATE_DELAY)

    if cursor:
        print(f"\n  *** TRUNCATED: --max-pages {args.max_pages} reached with a "
              f"live cursor. Re-run with a higher cap to get the rest. ***")

    out["counts"] = {"records": len(records), "calls_used": calls,
                     "truncated": bool(cursor)}
    summarize(records)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {calls} API call(s) used.")
    print(f"  JSON -> {out_path}")
    if args.platform == "copart":
        print("  next: python analytics/scripts/copart_vpic_adapt_01.py "
              f"{out_path.name}")
    else:
        print("  next: python analytics/scripts/apibara_json2csv_iaai_01.py "
              f"{out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
