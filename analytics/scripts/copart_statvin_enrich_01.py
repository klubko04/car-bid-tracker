"""P5 — enrich adapted Copart records with stat.vin seller class and full VIN.

    copart_web_adapt_01.py
        -> data/open/json-adapted/copart/adapted_copartweb_*.json
    pull_statvin_web_01.py
        -> data/open/json-raw/copart/statvin_*.json
    copart_statvin_enrich_01.py ADAPTED.json --statvin STATVIN.json
        -> data/open/json-adapted/copart/statvin_adapted_copartweb_*.json

Offline: this stage makes no HTTP requests. All network work happened in the
puller.

SELLER PRECEDENCE
-----------------
Copart's own name stays senior; stat.vin is the next source down:

    1. copart.com ``scn``     a NAMED carrier -- GEICO, USAA, CSAA. Identifies
                              the specific company, so it also survives a
                              carrier-level analysis. Present on 25-46% of lots.
    2. stat.vin               a seller TYPE -- Insurance or Dealer. Present on
                              ~97% of lots but never names the company.
    3. APIBara ``seller.type``  100% present where APIBara covers the lot, but
                              wrong often enough on named companies that
                              copart_seller resolves the name first.

stat.vin therefore never overwrites a Copart name; it fills the lots where
Copart published nothing, which is exactly the 55-75% gap. Because it supplies
a bin and not an identity, the enriched record carries
``seller.identity_withheld = true`` when stat.vin is the source: usable for
insurance-vs-repo analysis, useless for carrier-level analysis, and flagged so
the two never get mixed.

FULL VINs
---------
Copart masks the VIN on its public surface (``WAUSAAF56NA******``), which is
why the web branch could not feed ``copart_vpic_adapt_01.py``. stat.vin
publishes the complete VIN, so this stage can fill it -- and that reopens vPIC
decoding for web-only lots.

A VIN is only ever accepted when it is consistent with what Copart already
published: same lot number, same year, and the visible prefix must match the
mask. On the first validation cohort that check passed 19/19 with zero
conflicts. A conflict is never silently resolved -- the record keeps Copart's
masked value and the disagreement is recorded under ``audit``.

Examples:

    python analytics/scripts/copart_statvin_enrich_01.py ADAPTED.json \
        --statvin statvin_copart_open_audi_a5_2018_2023_*.json
    python analytics/scripts/copart_statvin_enrich_01.py ADAPTED.json \
        --statvin STATVIN.json --audit
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import copart_seller  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "copart"
STAGE = "copart_statvin_enrich_01"
SOURCE = "statvin-search"
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# stat.vin badge -> the vocabulary copart_seller already speaks.
#
# "Dealer" maps to `dealer`, not to `non_insurance`. stat.vin's binary is
# Insurance vs Dealer and its own descriptor pairs them ("Dealer /
# Non-insurance"), so flattening it to non_insurance would be defensible --
# except that it destroys the one label the pipeline needs to act on. Copart
# dealer consignments are trade-in dross a retailer already declined to retail;
# they are excluded from the final cut and from gallery capture, and that
# exclusion can only be expressed if the class survives the mapping.
STATVIN_CLASS = {"insurance": "insurance", "dealer": "dealer"}


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_lot(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.lstrip("0") or None


def records(document):
    """Yield lot records wherever this pipeline's archives keep them."""
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("lot_number") is not None and "seller" in node:
                if id(node) not in seen:
                    seen.add(id(node))
                    yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    yield from walk(document)


def visible_prefix(vin):
    """'WAUS...NA******' -> 'WAUS...NA'; a full or absent VIN yields ''."""
    text = str(vin or "").strip().upper()
    if not text or VIN_RE.match(text):
        return ""
    return text.split("*")[0]


def identity_conflicts(record, feed):
    conflicts = []
    left, right = record.get("year"), feed.get("year")
    try:
        if left and right and int(left) != int(right):
            conflicts.append({"field": "year", "record": left, "feed": right})
    except (TypeError, ValueError):
        pass
    prefix = visible_prefix(record.get("vin"))
    full = str(feed.get("vin") or "").strip().upper()
    if prefix and VIN_RE.match(full) and not full.startswith(prefix):
        conflicts.append({"field": "vin_prefix", "record": prefix, "feed": full})
    return conflicts


# Only a classification that came from Copart's own search row counts as a
# name. copart_web_adapt_01 also copies APIBara's seller into this field, and
# APIBara's "Insurance Company" / "Non-insurance Company" are placeholders --
# a class assertion with the identity stripped out, flagged by
# identity_withheld. Treating those as names would lock stat.vin out of exactly
# the lots it exists to fill.
COPART_NAME_SOURCES = {"search.scn"}


def seller_is_named(record):
    """True when COPART itself published an identifying company name."""
    seller = record.get("seller") or {}
    classification = seller.get("classification") or seller
    if not isinstance(classification, dict):
        return False
    if not classification.get("name"):
        return False
    if classification.get("identity_withheld"):
        return False
    return str(classification.get("source") or "") in COPART_NAME_SOURCES


def apply_feed(record, feed):
    """-> outcome string. Mutates the record only on a clean identity match."""
    conflicts = identity_conflicts(record, feed)
    if conflicts:
        return "identity_conflict", conflicts

    outcome = []
    seller_class = STATVIN_CLASS.get(feed.get("seller_class") or "")
    if seller_class and not seller_is_named(record):
        classification = copart_seller.classify(
            None, seller_class, source="statvin.search"
        )
        # stat.vin bins the lot without naming the company, so this is usable
        # for insurance-vs-repo analysis and useless for carrier-level work.
        classification["identity_withheld"] = True
        classification["statvin_label"] = feed.get("seller_label")
        seller = record.setdefault("seller", {})
        if isinstance(seller.get("classification"), dict):
            seller["classification"] = classification
        else:
            record["seller"] = classification
        outcome.append("seller")
    elif seller_class:
        outcome.append("seller_kept_copart_name")

    full = str(feed.get("vin") or "").strip().upper()
    if VIN_RE.match(full) and not VIN_RE.match(str(record.get("vin") or "").strip().upper()):
        record["vin_masked_source"] = record.get("vin")
        record["vin"] = full
        outcome.append("vin")

    record.setdefault("enrichment", {})["statvin_search"] = {
        "source": SOURCE, "stage": STAGE, "retrieved_at": now_iso(),
        "lot_number": feed.get("lot_number"), "seller_label": feed.get("seller_label"),
        "seller_class": feed.get("seller_class"), "page_url": feed.get("page_url"),
        "filled": list(outcome),
    }
    return ("+".join(outcome) if outcome else "no_change"), []


def load_feed(paths):
    feed = {}
    for path in paths:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        for record in document.get("records") or []:
            lot = normalize_lot(record.get("lot_number"))
            if lot:
                feed[lot] = record
    return feed


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Fill Copart seller class and full VIN from a stat.vin archive."
    )
    parser.add_argument("file", help="adapted Copart json-adapted archive")
    parser.add_argument("--statvin", action="append", required=True, metavar="JSON",
                        help="stat.vin archive from pull_statvin_web_01.py")
    parser.add_argument("--audit", action="store_true",
                        help="print the per-lot outcome table")
    parser.add_argument("--out", help="output JSON (default: statvin_INPUT.json)")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    source = Path(args.file).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        feed = load_feed(args.statvin)
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from None
    if str(document.get("platform") or "").casefold() != PLATFORM:
        raise SystemExit(f"{source.name}: expected platform='copart'")

    output = copy.deepcopy(document)
    counts = Counter()
    audit = []
    for record in records(output):
        lot = normalize_lot(record.get("lot_number"))
        match = feed.get(lot)
        if not match:
            counts["not_in_statvin"] += 1
            continue
        outcome, conflicts = apply_feed(record, match)
        counts[outcome] += 1
        if args.audit or conflicts:
            audit.append({"lot_number": lot, "outcome": outcome,
                          "conflicts": conflicts or None})

    output["adapted_at"] = now_iso()
    output["statvin_enrichment"] = {
        "stage": STAGE, "source": SOURCE,
        "policy": "copart_name_senior_statvin_type_next_identity_validated",
        "input": source.name,
        "statvin_archives": [Path(p).name for p in args.statvin],
        "feed_lots": len(feed), "counts": dict(counts), "audit": audit,
    }
    destination = (Path(args.out).expanduser().resolve() if args.out
                   else source.parent / f"statvin_{source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    print(f"stat.vin enrichment — {len(feed)} feed lot(s)")
    print(f"  counts: {dict(counts)}")
    if args.audit:
        for row in audit[:40]:
            print(f"    {row['lot_number']:<11} {row['outcome']}"
                  + (f"  CONFLICT {row['conflicts']}" if row["conflicts"] else ""))
    print(f"  JSON -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
