"""
Stage 3 of the analytics pipeline — filtered, enriched CSV for one make/model.

    pull_apibara_01.py  ->  raw .json
                              |
                        apibara_json2csv_{iaai|copart}_01.py   (flatten, 57 key fields incl. distance)
                              |
                        THIS SCRIPT  ->  enriched .csv

Offline. Makes no API calls — only pull_apibara_01.py ever spends quota.

    python analytics/scripts/data_pull_01.py iaai                     # newest archive
    python analytics/scripts/data_pull_01.py iaai FILE.json ...       # specific ones
    python analytics/scripts/data_pull_01.py iaai --all --tier 1      # every archive
    python analytics/scripts/data_pull_01.py iaai --columns           # show the schema

The platform argument selects which flattener to use. Each platform has its own,
because the payloads are different schemas rather than variants of one: Copart
records carry `details: None`, which removes ACV, repair estimate, body style and
storage coordinates in one go.

    iaai    -> apibara_json2csv_iaai_01.py
    copart  -> apibara_json2csv_copart_01.py   (not written yet)

FILTERS (all opt-in, all inherited from the flattener)
------------------------------------------------------
    --exclude-damage water,flood,fire   drop lots whose damage matches
    --include-damage front,rear         keep ONLY lots whose damage matches
    --body-style coupe                  keep only these body styles
    --exclude-body-style coupe          drop these
    --seller-class insurance            keep only these seller classes
    --min-photos 8                      drop thin listings
    --sold-only                         drop lots with no realised sale price
    --market unitedstates               keep only these markets (US / Canada)
    --max-odometer 100000               drop high-mileage and unknown-odometer lots
    --max-distance 3000                 drop far and unknown-location lots

ADDED COLUMNS
-------------
`tier`            Want-list tier from app/tier.py. Because one archive is one
                  make/model, `--tier 1|2|3` sets it for the whole file. Left
                  off, each row is classified from make/model/year/engine, which
                  yields None for anything not on the want-list — so an explicit
                  --tier is the way to label a model the table does not know yet.
`tier_source`     `cli` or `auto`, so an override is never mistaken for a match.
`sold_period`     `YYYY-MM` of last_sold_day. Same convention as the month
                  folder in app/image_pipeline.py, so rows join to the photo
                  archive path.

Distance USED to be computed here and now belongs to the flattener
(`distance_mi`, `distance_bucket`), because it needs no input from this stage:
IAAI's own StorageLocationLatitude/Longitude is populated on 154/154 records
across four pulls, so there is nothing to decide and no fallback to configure.
Tier is the opposite — it takes an operator decision per archive, which is
exactly why it stays here.
"""
import argparse
import csv
import datetime as dt
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))                      # for app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))   # for the flatteners

from app.tier import TIER_1, TIER_2, TIER_3, classify  # noqa: E402
import lot_history_01 as HIST  # noqa: E402

FLATTENERS = {
    "iaai": "apibara_json2csv_iaai_01",
    "copart": "apibara_json2csv_copart_01",
}

TIERS = {"1": TIER_1, "2": TIER_2, "3": TIER_3,
         "tier1": TIER_1, "tier2": TIER_2, "tier3": TIER_3,
         "tier 1": TIER_1, "tier 2": TIER_2, "tier 3": TIER_3}

# Appended after the flattener's own columns, in this order.
ENRICHED_COLUMNS = ["tier", "tier_source", "sold_period"]

# An --out that already carries a YYYYmmddTHHMMSS stamp is left alone, so
# re-running the exact command from a shell history does not keep appending.
_HAS_STAMP = re.compile(r"\d{8}T\d{6}")


def load_flattener(platform):
    mod = FLATTENERS[platform]
    try:
        return importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name != mod:
            raise
        raise SystemExit(
            f"No flattener for {platform!r}: analytics/scripts/{mod}.py does "
            f"not exist yet.\n"
            f"  Copart needs its own because its payload has `details: None` "
            f"— no ACV, no repair estimate, no body style, no branch "
            f"coordinates. See the Copart section of "
            f"analytics/schema/iaai_csv_schema.md.") from None


# --------------------------------------------------------------------------
# enrichment
# --------------------------------------------------------------------------
def engine_hint(v, flat):
    """A string app.tier._is_six_cylinder can read.

    Normally that is `vehicle_specs.engine.raw` verbatim. The fallbacks exist
    because tier classification must not depend on one optional field: a lot
    with no engine string still has a cylinder count, and failing that a
    displacement, which is the same signal the tier table itself keys on.
    """
    raw = flat.g(v, "vehicle_specs", "engine", "raw")
    if raw:
        return str(raw)
    cyl = flat.attrs(v).get("Cylinders")
    if cyl:
        return f"V-{cyl}"
    size = flat.g(v, "vehicle_specs", "engine", "size_l")
    return f"{size}L" if size else ""


def row_tier(v, flat, forced):
    if forced:
        return forced, "cli"
    t = classify(v.get("make"), v.get("model"), v.get("year"),
                 engine_hint(v, flat))
    return (t or ""), ("auto" if t else "auto:unclassified")


def sold_period(row):
    """'2026-08' from last_sold_day. Matches image_pipeline's month folder."""
    d = str(row.get("last_sold_day") or "")
    return d[:7] if len(d) >= 7 else ""


def enrich(v, row, flat, forced_tier, history=None):
    tier, tier_source = row_tier(v, flat, forced_tier)
    out = {**row,
           "tier": tier,
           "tier_source": tier_source,
           "sold_period": sold_period(row)}
    if history is not None:
        out.update(history.get(str(row.get("lot_number") or ""))
                   or HIST.blank_history())
    return out


# --------------------------------------------------------------------------
def parse_tier(s):
    key = str(s).strip().lower()
    if key in ("", "auto"):
        return None
    if key not in TIERS:
        raise argparse.ArgumentTypeError(
            f"--tier wants 1, 2, 3 or 'Tier 1'/'Tier 2'/'Tier 3', got {s!r}")
    return TIERS[key]


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="data_pull_01.py",
        description="Filtered, enriched CSV from raw Apibara archives. "
                    "Offline — no API calls.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=list(FLATTENERS), type=str.lower)
    ap.add_argument("files", nargs="*",
                    help="archive .json files (default: newest for the platform)")
    ap.add_argument("--all", action="store_true",
                    help="use every archive for this platform")
    ap.add_argument("--tier", type=parse_tier, default=None, metavar="{1|2|3}",
                    help="label every row with this tier (one archive = one "
                         "make/model). Omit to classify per row via app/tier.py")
    ap.add_argument("--exclude-damage", nargs="+", metavar="a,b,c")
    ap.add_argument("--include-damage", nargs="+", metavar="a,b,c")
    ap.add_argument("--body-style", action="append", nargs="+", default=[], metavar="STYLE")
    ap.add_argument("--exclude-body-style", action="append", nargs="+",
                    default=[], metavar="STYLE")
    ap.add_argument("--seller-class", action="append", default=[],
                    choices=["insurance", "dealer", "other", "unknown"])
    ap.add_argument("--min-photos", type=int, default=0)
    ap.add_argument("--market", action="append", default=[], metavar="MARKET",
                    help="keep only these markets: unitedstates | canada. "
                         "Filters the CSV regardless of how the archive was "
                         "pulled — older archives predate pull-time scoping")
    ap.add_argument("--max-odometer", type=int, default=0, metavar="MILES",
                    help="drop lots over this mileage (and unknown odometer)")
    ap.add_argument("--max-distance", type=int, default=0, metavar="MILES",
                    help="drop lots farther than this from 98003 (and lots with "
                         "no branch coordinates)")
    ap.add_argument("--sold-only", action="store_true")
    ap.add_argument("--history", action="store_true",
                    help="add cross-snapshot columns (relists, buy-now "
                         "appearing, departures). Widens the input set to every "
                         "archive sharing a search cohort with the selection")
    ap.add_argument("--history-cache", action="store_true",
                    help="with --history, also write the derived history "
                         "artifact under data/<bucket>/history/<platform>/")
    ap.add_argument("--out", help="output .csv path (default: alongside input)")
    ap.add_argument("--columns", "--schema", action="store_true", dest="columns",
                    help="print the output columns and exit")
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    flat = load_flattener(args.platform)
    added = ENRICHED_COLUMNS + (HIST.HISTORY_COLUMNS if args.history else [])
    columns = list(flat.COLUMNS) + added

    if args.columns:
        print(f"{len(columns)} columns = {len(flat.COLUMNS)} from "
              f"{FLATTENERS[args.platform]} + {len(added)} added here\n")
        for c in flat.COLUMNS:
            print(f"  {c:<24} {flat.SOURCE_HINTS.get(c, '')}")
        print()
        for c in ENRICHED_COLUMNS:
            print(f"  {c:<24} ** added by data_pull_01 **")
        for c in (HIST.HISTORY_COLUMNS if args.history else []):
            print(f"  {c:<24} ** added by --history (lot_history_01) **")
        return 0

    paths = flat.resolve_inputs(args)
    if args.history:
        # History over one archive is trivially empty, so pull in every other
        # snapshot of the same search. Absence is only meaningful within a
        # cohort — see lot_history_01.cohort_key.
        widened = HIST.expand_to_cohorts(paths)
        if len(widened) != len(paths):
            print(f"  --history: widened {len(paths)} -> {len(widened)} archive(s) "
                  f"across matching search cohort(s)")
        paths = widened
    print("=" * 78)
    print(f"{args.platform.upper()} raw JSON -> enriched CSV")
    print("=" * 78)
    records = flat.load_records(paths)
    if not records:
        raise SystemExit(f"no {args.platform} records in the given archive(s)")

    filters = {
        "exclude_damage": flat.csv_list(args.exclude_damage),
        "include_damage": flat.csv_list(args.include_damage),
        "body_styles": flat.style_set(args.body_style),
        "exclude_body_styles": flat.style_set(args.exclude_body_style),
        "seller_classes": set(args.seller_class),
        "min_photos": args.min_photos,
        "sold_only": args.sold_only,
        "markets": {m.strip().lower() for m in args.market},
        "max_odometer": args.max_odometer,
        "max_distance": args.max_distance,
    }
    active = {k: v for k, v in filters.items() if v}
    print(f"\n  filters: {active or 'none (keeping every record)'}")
    print(f"  tier:    {args.tier or 'auto (per-row via app/tier.py)'}")

    # History MUST be computed here, from every record across every snapshot —
    # the de-dupe below collapses a lot to one row and destroys exactly the
    # signal history exists to capture.
    history = None
    if args.history:
        # Apibara `ended` archives are loaded as CONTEXT ONLY — they carry the
        # sale price that iaai.com never publishes, but their lots must not
        # become rows or every sold Lexus lands in an Audi A5 CSV.
        ctx = HIST.sold_context(exclude=paths)
        hist_records = records + (flat.load_records(ctx) if ctx else [])
        history = HIST.build_history(hist_records, list(paths) + ctx)
        relisted = sum(1 for h in history.values() if h["relist_count"])
        priced = sum(1 for h in history.values() if h["exit_price_usd"])
        gone = sum(1 for h in history.values() if h["exit_state"] == "gone")
        print(f"  history:  {len(history)} lot(s) tracked   {relisted} relisted   "
              f"{gone} concluded   {priced} with a sale price")
        if ctx:
            print(f"            (+{len(ctx)} ended archive(s) as price context)")

    # De-dupe across overlapping archives: richest first, then newest.
    #
    # Straight newest-wins is wrong once the cheap search-only cadence exists —
    # a 1-request search pull carries no ACV, no repair estimate and no damage
    # codes, so being newest it would shadow a full record and blank those
    # columns. Static fields do not change between pulls anyway; the fields
    # that DO move (state, auction date, buy-now) are captured by --history.
    def rank(v):
        return (1 if v.get("_detail_level") == "search" else 0,
                # negate by comparing later; tuple sorts ascending
                v.get("_pulled_at") or "")

    by_key, dupes = {}, 0
    for v in records:
        key = (v.get("platform"), v.get("lot_number"), v.get("vin"))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = v
            continue
        dupes += 1
        thin_new, thin_old = rank(v)[0], rank(prev)[0]
        if thin_new < thin_old:
            by_key[key] = v                      # richer wins outright
        elif thin_new == thin_old and \
                (v.get("_pulled_at") or "") >= (prev.get("_pulled_at") or ""):
            by_key[key] = v                      # same richness -> newest wins

    kept, dropped = [], []
    for v in by_key.values():
        ok, why = flat.keep(v, filters)
        (kept if ok else dropped).append((v, why))

    print(f"  unique lots: {len(by_key)}   (dropped {dupes} duplicate row(s))")
    print(f"  kept {len(kept)}   filtered out {len(dropped)}")
    if dropped:
        reasons = {}
        for _, why in dropped:
            reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {why}")

    rows = [enrich(v, flat.flatten(v), flat, args.tier, history) for v, _ in kept]

    if args.history and args.history_cache:
        for cohort in {HIST.snapshot_meta(p)["cohort"] for p in paths}:
            sub = [p for p in paths if HIST.snapshot_meta(p)["cohort"] == cohort]
            # Per cohort, from that cohort's records only — see the same guard
            # in lot_history_01.main().
            files = {HIST.snapshot_meta(p)["file"] for p in sub}
            recs = [r for r in records if r.get("_source_file") in files]
            lots = HIST.build_history(recs, sub)
            out = HIST.write_cache(lots, sub, cohort,
                                   records[-1].get("_mode", "open"))
            print(f"  history cache -> {out}  ({len(lots)} lots)")

    # what the enrichment actually produced — a silent 'unknown' bucket or an
    # empty tier column is the failure mode worth surfacing every run
    def tally(col):
        out = {}
        for r in rows:
            out[r[col] or "(none)"] = out.get(r[col] or "(none)", 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    print(f"\n  tier:            {tally('tier')}")
    print(f"  tier_source:     {tally('tier_source')}")
    print(f"  sold_period:     {tally('sold_period')}")
    print(f"  distance_bucket: {tally('distance_bucket')}   (from the flattener)")

    # Stage-3 output lands in the csv-cut/ of the bucket the archive belongs to
    # (sold or open), whatever path it arrived by. A relative --out resolves
    # there too; an absolute one wins.
    out_dir = flat.layer_dir(records[-1].get("_mode", "ended"), "csv-cut",
                             records[-1].get("_platform", args.platform))
    # A csv-cut is a filtered VIEW at a moment, and the same filters re-run
    # tomorrow give a different answer — so the run time is part of the
    # filename. Without it each re-run silently overwrote the previous cut and
    # there was no way to tell two vintages apart.
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    if args.out:
        out_path = Path(args.out)
        if not _HAS_STAMP.search(out_path.stem):
            out_path = out_path.with_name(f"{out_path.stem}_{stamp}{out_path.suffix or '.csv'}")
        if not out_path.is_absolute():
            out_path = out_dir / out_path
    else:
        out_path = out_dir / f"{paths[-1].stem}_data_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

    print("\n" + "=" * 78)
    print(f"Done. {len(rows)} row(s) x {len(columns)} column(s)")
    print(f"  CSV -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
