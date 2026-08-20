"""Stage 1.5 — Copart web raw archive -> canonical Copart JSON.

    pull_copart_web_01.py
        -> data/open/json-raw/copart/copartweb_*.json
    pull_apibara_01.py copart open|live
    copart_vpic_adapt_01.py apibara_copart_open_*.json
        -> data/open/json-adapted/copart/vpic_apibara_*.json
    copart_web_adapt_01.py WEB.json --enrich-from VPIC.json
        -> data/open/json-adapted/copart/adapted_copartweb_*.json
    apibara_json2csv_copart_01.py

The cross-source key is the Copart lot number. Copart web exposes it as ``ln``
and ``lotNumberStr``; APIBara exposes the same value as ``lot_number``. A lot
number match only selects an enrichment candidate. Before copying a full VIN or
vPIC data, this adapter also requires year/make/model and the visible VIN prefix
to agree. A conflict is retained as audit data and the web record stays
web-only.

The newest web observation remains authoritative for volatile auction fields:
current bid, Buy Now and auction date. APIBara/vPIC fill identity, seller type,
the full image list and missing static specifications. Unmatched web lots are
not dropped. Their masked VIN and missing vPIC status are honest source limits.

The raw web archive retains every market. This derived layer is US-only:
Canadian and unclassified rows are excluded with lot numbers and counts stored
under ``adapter.market_scope``.

Examples:

    python analytics/scripts/copart_web_adapt_01.py WEB.json
    python analytics/scripts/copart_web_adapt_01.py WEB.json \
        --enrich-from vpic_apibara_copart_open_*.json
    python analytics/scripts/copart_web_adapt_01.py WEB.json \
        --enrich-from apibara_copart_open_*.json vpic_apibara_copart_open_*.json \
        --audit

Offline: this script makes no HTTP requests.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT / "analytics" / "data"
PLATFORM = "copart"
SOURCE = "copart-web-adapted"
OUT_LAYER = "json-adapted"
ADAPTER_NAME = "copart_web_adapt_01"
ADAPTER_VERSION = 4
MI_TO_KM = 1.609344
FULL_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

sys.path.insert(0, str(SCRIPT_DIR))
import copart_seller  # noqa: E402
import pull_copart_web_01 as web_pull  # noqa: E402


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def layer_dir(mode="open"):
    bucket = "sold" if str(mode).lower() == "ended" else "open"
    return DATA_DIR / bucket / OUT_LAYER / PLATFORM


def clean(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def number(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def integer(value):
    value = number(value)
    return int(value) if value is not None else None


def positive_money(value):
    value = number(value)
    return value if value is not None and value > 0 else None


def nonnegative_money(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def as_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"yes", "y", "true", "1"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    return None


def epoch_ms_iso(value):
    value = number(value)
    if value is None or value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_lot(value):
    """Canonical lot key shared by web integers and APIBara strings."""
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def norm_identity(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def valid_full_vin(value):
    return bool(FULL_VIN_RE.fullmatch(str(value or "").strip().upper()))


def visible_vin_prefix(value):
    text = str(value or "").strip().upper()
    match = re.match(r"[A-HJ-NPR-Z0-9]+", text)
    return match.group(0) if match else ""


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relpath(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def engine_size(value):
    match = re.search(r"(\d+(?:\.\d+)?)\s*L\b", str(value or ""), re.I)
    return float(match.group(1)) if match else None


def location_display(row):
    city = clean(row.get("locCity"))
    state = clean(row.get("locState"))
    if city and state:
        return f"{city.title()} ({state.upper()})"
    return clean(row.get("yn") or row.get("syn"))


def run_condition(value):
    text = clean(value)
    folded = str(text or "").casefold()
    return {
        "value": text,
        "label": text.title() if text else None,
        "class_hint": "success" if folded == "runs and drives" else
                      ("warning" if text else None),
    }


def web_media(row):
    url = clean(row.get("tims"))
    if not url:
        return {"thumbs_count": 0, "has_video": False, "has_360": False,
                "thumbs": [], "items": []}
    return {
        "thumbs_count": 1,
        "has_video": False,
        "has_360": False,
        "thumbs": [url],
        # Search exposes one real thumbnail, not the complete lot image list.
        # Keep the URL verbatim; an APIBara match replaces this with full media.
        "items": [{"type": "image", "thumb": url, "full": None, "large": None}],
    }


def seller_from(wrapper, source_record=None):
    web_seller = wrapper.get("seller") or {}
    source_seller = (source_record or {}).get("seller") or {}
    name = clean(web_seller.get("name")) or clean(source_seller.get("name"))
    published_type = clean(source_seller.get("type")) or clean(
        web_seller.get("published_type")
    )
    source = "search.scn" if clean(web_seller.get("name")) else (
        "apibara.seller" if name or published_type else None
    )
    classification = copart_seller.classify(name, published_type, source=source)
    return {
        "name": name,
        "type": published_type,
        "classification": classification,
    }


def adapt_web_record(wrapper):
    """One exact web wrapper -> the canonical Copart record shape."""
    row = wrapper.get("search") or {}
    engine = clean(row.get("egn"))
    odometer = number(row.get("orr"))
    auction_at = epoch_ms_iso(row.get("ad"))
    buy_now = positive_money(
        (row.get("dynamicLotDetails") or {}).get("buyTodayBid") or row.get("bnp")
    )
    dynamic = row.get("dynamicLotDetails") or {}
    current_bid = nonnegative_money(
        dynamic.get("currentBid")
        if dynamic.get("currentBid") is not None else row.get("hb")
    )
    title = clean(row.get("ld"))
    doc_name = clean(row.get("td"))
    web_classification = seller_from(wrapper)

    vin = clean(row.get("fv"))
    vin_status = "full" if valid_full_vin(vin) else (
        "masked" if "*" in str(vin or "") else "missing"
    )

    return {
        "platform": PLATFORM,
        "platform_id": 1,
        "lot_number": normalize_lot(
            wrapper.get("lot_number") or row.get("ln") or row.get("lotNumberStr")
        ),
        "vin": vin,
        "year": integer(row.get("lcy")),
        "make": clean(row.get("mkn")),
        "model": clean(row.get("lm")),
        "title": title,
        "type": clean(row.get("memberVehicleType") or row.get("vehicleTypeCode")),
        "subLot": bool(row.get("sbf")),
        "ad": auction_at,
        "details": None,
        "vehicle_specs": {
            "body_style": clean(row.get("bstl")),
            "engine": {
                "raw": engine,
                "size_l": engine_size(engine),
                "hp": None,
                "layout": None,
                "cylinders": integer(row.get("cy")),
            },
            "transmission": clean(row.get("tsmn") or row.get("tmtp")),
            "fuel_type": clean(row.get("ft")),
            "drive_type": clean(row.get("drv")),
            "exterior_color": (clean(row.get("clr")) or "").title() or None,
            "body_style_source": "copart_web.bstl" if row.get("bstl") else None,
            "trim": clean(row.get("ltd")),
        },
        "condition": {
            "run_condition": run_condition(row.get("lcd")),
            "has_key": as_bool(row.get("hk")),
            "loss": None,
            "primary_damage": (clean(row.get("dd")) or "").title() or None,
            "secondary_damage": (clean(row.get("sdd") or row.get("sddr")) or "").title()
                                or None,
        },
        "odometer": {
            "mi": integer(odometer),
            "km": int(round(odometer * MI_TO_KM)) if odometer is not None else None,
            "status": clean(row.get("ord")),
        },
        "pricing": {
            "current_bid_usd": current_bid,
            "current_bid2_usd": current_bid,
            "buy_now_usd": buy_now,
            "last_sold_price_usd": None,
            # Copart calls `la` Estimated Retail Value (ERV), not ACV.  Keep
            # it distinct so downstream max-bid maths cannot accidentally
            # treat the seller-submitted retail estimate as insurer ACV.
            "estimated_retail_value_usd": positive_money(row.get("la")),
            "estimated_cost": {},
        },
        "auction": {
            "state": "open",
            "formatted": None,
            "full_date": auction_at,
            "ad": auction_at,
            "is_timed": None,
            "is_buy_now": bool(buy_now),
            "auction_at": auction_at,
            "timed_end_at": None,
            "last_sold_day": None,
            "last_sold_status": None,
            "sold_buy_now": False,
            "sold_timed": False,
            # `ess` is Copart's visible bid/sale condition: Pure Sale,
            # Minimum Bid, or On Approval. The dynamic flag is independently
            # useful because a minimum-bid lot can move from reserve-not-met
            # to reserve-met without changing identity or static detail.
            "bid_type": clean(row.get("ess")),
            "sale_status": clean(dynamic.get("saleStatus")),
            "seller_reserve_met": as_bool(dynamic.get("sellerReserveMet")),
            "item_number": integer(row.get("aan")),
        },
        "seller": web_classification,
        "sale_document": {
            "name": doc_name,
            "type": clean(row.get("tgc") or row.get("tgd")),
            "export": None,
            "registration": None,
            "is_pending": bool(doc_name and "(P)" in doc_name.upper()),
            "sale_document_group": None,
            "title_state": clean(row.get("ts")),
        },
        "location": {
            "display": location_display(row),
            "send_from": None,
            "state": clean(row.get("locState")),
            "country": clean(row.get("locCountry")),
        },
        "facility": {
            "id": row.get("ynumb"),
            "state": clean(row.get("locState")),
            "zip": clean(row.get("zip")),
            "lat": number(row.get("lat")),
            "lng": number(row.get("long")),
            "office_name": clean(row.get("yn") or row.get("syn")),
        },
        "media": web_media(row),
        "enrichment": {
            "copart_web": {
                # P0 contract candidates retained under their Copart codes.
                # They are intentionally not relabelled ACV/repair in canonical
                # pricing until checked against a visible/member source.
                "la": number(row.get("la")),
                "lotPlugAcv": number(row.get("lotPlugAcv")),
                "rc": number(row.get("rc")),
                "currency": clean(row.get("cuc")),
                "bid_type_raw": clean(row.get("ess")),
                "sale_status_raw": clean(dynamic.get("saleStatus")),
                "seller_reserve_met_raw": as_bool(dynamic.get("sellerReserveMet")),
                "seller": web_classification["classification"],
                "search_thumbnail_only": bool(row.get("tims")),
                "vin_status": vin_status,
                "vpic_eligible": valid_full_vin(vin),
            }
        },
        "_web_keyword": wrapper.get("keyword"),
        "_web_detail_url": wrapper.get("detail_url"),
        "_web_market": web_pull.market_label(row),
        "_web_vin_masked": bool(wrapper.get("vin_masked")),
        "_detail_level": "search",
    }


def fill_missing(target, source):
    for key, value in (source or {}).items():
        current = target.get(key)
        if isinstance(value, dict):
            if not isinstance(current, dict):
                if current not in (None, "", [], {}):
                    continue
                current = {}
                target[key] = current
            fill_missing(current, value)
        elif current in (None, "", [], {}) and value not in (None, "", [], {}):
            target[key] = copy.deepcopy(value)


def source_records(document):
    for page in document.get("pages") or []:
        if page.get("status") != 200:
            continue
        for record in (page.get("raw") or {}).get("data") or []:
            if isinstance(record, dict):
                yield record


def source_rank(record, document):
    vp = ((record.get("enrichment") or {}).get("nhtsa_vpic") or {})
    media_count = len((record.get("media") or {}).get("items") or [])
    return (
        int(vp.get("status") == "decoded"),
        int(valid_full_vin(record.get("vin"))),
        media_count,
        document.get("adapted_at") or document.get("generated_at") or "",
    )


def load_enrichment(paths):
    """Return lot -> richest APIBara/vPIC canonical record and provenance."""
    output = {}
    for path in paths:
        path = Path(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        if str(document.get("platform") or "").casefold() != PLATFORM:
            raise ValueError(f"{path.name}: expected platform='copart'")
        if str(document.get("source") or "").casefold() == "copart-web":
            raise ValueError(f"{path.name}: --enrich-from expects APIBara/vPIC JSON")
        for record in source_records(document):
            lot = normalize_lot(record.get("lot_number"))
            if not lot:
                continue
            candidate = {
                "record": record,
                "rank": source_rank(record, document),
                "path": path,
                "generated_at": document.get("generated_at"),
                "adapted_at": document.get("adapted_at"),
            }
            current = output.get(lot)
            if current is None or candidate["rank"] > current["rank"]:
                output[lot] = candidate
    return output


def body_style_family(value):
    tokens = {
        token for token in re.split(r"[^a-z0-9]+", str(value or "").casefold())
        if token
    }
    if "coupe" in tokens:
        return "Coupe"
    if tokens & {"convertible", "cabriolet"}:
        return "Convertible/Cabriolet"
    if tokens & {"hatchback", "liftback", "notchback", "sportback"}:
        return "Hatchback/Liftback/Notchback"
    if tokens & {"sedan", "saloon"}:
        return "Sedan/Saloon"
    return None


def vin_descriptor_key(record):
    """Body-relevant VIN descriptor plus explicit search identity.

    Copart masks only the six-character serial, leaving the eight-character
    vehicle descriptor visible.  We never submit that masked VIN to vPIC;
    instead this key can reuse a unanimous body class already decoded from
    full VINs in the same make/model/year cohort.
    """
    prefix = visible_vin_prefix(record.get("vin"))
    year = integer(record.get("year"))
    make = norm_identity(record.get("make"))
    model = norm_identity(record.get("model"))
    if len(prefix) < 8 or year is None or not make or not model:
        return None
    return make, model, year, prefix[:8]


def load_body_style_descriptors(paths, minimum_support=2):
    """Return unanimous vPIC body styles keyed by visible VIN descriptor."""
    observations = {}
    for path in paths:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        for record in source_records(document):
            if not valid_full_vin(record.get("vin")):
                continue
            vpic = ((record.get("enrichment") or {}).get("nhtsa_vpic") or {})
            if vpic.get("status") != "decoded":
                continue
            key = vin_descriptor_key(record)
            family = body_style_family(
                (record.get("vehicle_specs") or {}).get("body_style")
            )
            if key and family:
                observations.setdefault(key, {}).setdefault(family, set()).add(
                    str(record.get("vin")).upper()
                )
    output = {}
    for key, families in observations.items():
        if len(families) != 1:
            continue
        family, vins = next(iter(families.items()))
        if len(vins) >= minimum_support:
            output[key] = {"body_style": family, "supporting_full_vins": len(vins)}
    return output


def infer_body_style(record, descriptors):
    specs = record.setdefault("vehicle_specs", {})
    if clean(specs.get("body_style")):
        return False
    key = vin_descriptor_key(record)
    evidence = descriptors.get(key)
    if not evidence:
        return False
    specs["body_style"] = evidence["body_style"]
    specs["body_style_source"] = "vpic_full_vin_descriptor_consensus"
    record.setdefault("enrichment", {})["nhtsa_vpic_descriptor"] = {
        "status": "inferred_static_only",
        "field": "vehicle_specs.body_style",
        "value": evidence["body_style"],
        "visible_vin_descriptor": key[-1],
        "model_year": key[2],
        "supporting_full_vins": evidence["supporting_full_vins"],
        "masked_vin_submitted_to_vpic": False,
    }
    return True


def identity_conflicts(web_record, source_record):
    conflicts = []
    for field in ("year", "make", "model"):
        left, right = web_record.get(field), source_record.get(field)
        if left not in (None, "") and right not in (None, ""):
            matches = integer(left) == integer(right) if field == "year" else (
                norm_identity(left) == norm_identity(right)
            )
            if not matches:
                conflicts.append({"field": field, "web": left, "apibara": right})
    prefix = visible_vin_prefix(web_record.get("vin"))
    full_vin = str(source_record.get("vin") or "").strip().upper()
    if prefix and valid_full_vin(full_vin) and not full_vin.startswith(prefix):
        conflicts.append({"field": "vin_prefix", "web": prefix, "apibara": full_vin})
    return conflicts


def richer_media(web_record, source_record):
    web_media_block = web_record.get("media") or {}
    source_media_block = source_record.get("media") or {}
    web_count = len(web_media_block.get("items") or [])
    source_count = len(source_media_block.get("items") or [])
    return copy.deepcopy(source_media_block if source_count > web_count else web_media_block)


def enrich_record(web_record, wrapper, candidate):
    lot = normalize_lot(web_record.get("lot_number"))
    if candidate is None:
        web_record["_source_join"] = {
            "key": "lot_number", "lot_number": lot, "status": "not_found",
        }
        return "not_found", []

    source = candidate["record"]
    conflicts = identity_conflicts(web_record, source)
    provenance = {
        "key": "lot_number",
        "lot_number": lot,
        "status": "conflict" if conflicts else "matched",
        "source_file": candidate["path"].name,
        "source_generated_at": candidate.get("generated_at"),
        "source_adapted_at": candidate.get("adapted_at"),
        "identity_conflicts": conflicts,
        "web_vin_prefix": visible_vin_prefix(web_record.get("vin")) or None,
        "apibara_vin": source.get("vin") if valid_full_vin(source.get("vin")) else None,
    }
    web_record["_source_join"] = provenance
    if conflicts:
        return "conflict", conflicts

    if valid_full_vin(source.get("vin")):
        web_record["vin"] = str(source["vin"]).upper()

    # Web is the current observation. APIBara/vPIC fill only static gaps.
    for block in ("vehicle_specs", "condition", "odometer", "sale_document",
                  "location", "facility"):
        fill_missing(web_record.setdefault(block, {}), source.get(block) or {})

    # Preserve web's fresher bid/date, but retain APIBara's estimated-cost block.
    source_estimate = (source.get("pricing") or {}).get("estimated_cost")
    if source_estimate:
        web_record["pricing"]["estimated_cost"] = copy.deepcopy(source_estimate)

    web_record["media"] = richer_media(web_record, source)
    source_vpic = ((source.get("enrichment") or {}).get("nhtsa_vpic") or {})
    if source_vpic:
        web_record.setdefault("enrichment", {})["nhtsa_vpic"] = copy.deepcopy(source_vpic)

    web_record["seller"] = seller_from(wrapper, source)
    web_record["enrichment"]["copart_web"]["seller"] = \
        web_record["seller"]["classification"]
    web_record["_enriched_from"] = "apibara_by_lot_number"
    return "matched", []


def resolve_web(value=None):
    if value:
        path = Path(value).expanduser()
        probes = [path, ROOT / path,
                  DATA_DIR / "open" / "json-raw" / PLATFORM / path.name]
        for probe in probes:
            if probe.is_file():
                return probe.resolve()
        raise FileNotFoundError(f"Copart web archive not found: {value}")
    candidates = sorted(
        (DATA_DIR / "open" / "json-raw" / PLATFORM).glob("copartweb_*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise FileNotFoundError("No Copart web raw archives found")
    return candidates[-1].resolve()


def resolve_enrichment(value):
    path = Path(value).expanduser()
    probes = [path, ROOT / path]
    if not path.is_absolute():
        for bucket in ("open", "sold"):
            for layer in ("json-adapted", "json-raw"):
                probes.append(DATA_DIR / bucket / layer / PLATFORM / path.name)
    for probe in probes:
        if probe.is_file():
            return probe.resolve()
    raise FileNotFoundError(f"Copart enrichment archive not found: {value}")


def output_path(source, mode, explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = layer_dir(mode) / path
        return path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    return layer_dir(mode) / f"adapted_{source.name}"


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="copart_web_adapt_01.py",
        description="Adapt Copart web JSON and optionally enrich by APIBara lot number. Offline.",
    )
    parser.add_argument("file", nargs="?", help="Copart web raw JSON (default: newest)")
    parser.add_argument(
        "--enrich-from", nargs="+", default=[], metavar="APIBARA.json",
        help="raw or vPIC-adapted Copart APIBara archives, joined by lot_number",
    )
    parser.add_argument("--audit", action="store_true", help="print matched lot/VIN pairs")
    parser.add_argument("--out", help="output JSON path")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    try:
        source = resolve_web(args.file)
        enrich_paths = [resolve_enrichment(value) for value in args.enrich_from]
        document = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    if document.get("source") != "copart-web":
        raise SystemExit(f"{source.name}: source={document.get('source')!r}, expected copart-web")
    if str(document.get("platform") or "").casefold() != PLATFORM:
        raise SystemExit(f"{source.name}: platform must be copart")

    try:
        enrichment = load_enrichment(enrich_paths) if enrich_paths else {}
        body_descriptors = (
            load_body_style_descriptors(enrich_paths) if enrich_paths else {}
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    print("=" * 78)
    print("COPART web raw -> canonical Copart JSON")
    print(f"  web:        {source.name}")
    print(f"  enrichment:{' ' + ', '.join(path.name for path in enrich_paths) if enrich_paths else ' none'}")
    print("  join key:   normalized lot_number + identity validation")
    print("=" * 78)

    adapted = []
    excluded = {"Canada": [], "unknown": []}
    join_counts = Counter()
    descriptor_inferred = 0
    conflicts = []
    for wrapper in document.get("records") or []:
        row = wrapper.get("search") or {}
        market = web_pull.market_label(row)
        lot = normalize_lot(wrapper.get("lot_number") or row.get("ln"))
        if market != "UnitedStates":
            excluded.setdefault(market, []).append(lot or "?")
            continue
        record = adapt_web_record(wrapper)
        status, record_conflicts = enrich_record(record, wrapper, enrichment.get(lot))
        descriptor_inferred += infer_body_style(record, body_descriptors)
        join_counts[status] += 1
        if record_conflicts:
            conflicts.append({"lot_number": lot, "conflicts": record_conflicts})
        adapted.append(record)

    if not adapted:
        raise SystemExit("no positively identified US records to adapt")

    vpic_count = sum(
        bool(((record.get("enrichment") or {}).get("nhtsa_vpic") or {}))
        for record in adapted
    )
    full_vins = sum(valid_full_vin(record.get("vin")) for record in adapted)
    sellers = Counter(
        ((record.get("seller") or {}).get("classification") or {}).get("class", "unknown")
        for record in adapted
    )
    source_count = len(document.get("records") or [])
    market_scope = {
        "policy": "us_only",
        "source_records": source_count,
        "kept_records": len(adapted),
        "excluded_records": source_count - len(adapted),
        "excluded_by_market": {key: len(value) for key, value in excluded.items() if value},
        "excluded_lot_numbers": {key: value for key, value in excluded.items() if value},
    }
    out = {
        "generated_at": document.get("generated_at"),
        "adapted_at": now_iso(),
        "argv": argv,
        "platform": PLATFORM,
        "source": SOURCE,
        "mode": document.get("mode", "open"),
        "adapted_from": source.name,
        "enriched_from": [path.name for path in enrich_paths],
        "search_params": document.get("search_params") or {},
        "pages": [{"status": 200, "raw": {"data": adapted}}],
        "counts": {
            "source_records": source_count,
            "records": len(adapted),
            "excluded_non_us": source_count - len(adapted),
            "join": dict(join_counts),
            "full_vins": full_vins,
            "masked_or_missing_vins": len(adapted) - full_vins,
            "vpic_enriched": vpic_count,
            "body_style_descriptor_inferred": descriptor_inferred,
            "seller_class": dict(sellers),
            "truncated": bool((document.get("counts") or {}).get("truncated")),
        },
        "adapter": {
            "name": ADAPTER_NAME,
            "version": ADAPTER_VERSION,
            "source": {
                "path": relpath(source),
                "sha256": file_sha256(source),
                "generated_at": document.get("generated_at"),
            },
            "join": {
                "key": "normalized_lot_number",
                "identity_validation": ["year", "make", "model", "vin_prefix"],
                "matched": join_counts["matched"],
                "not_found": join_counts["not_found"],
                "conflicts": conflicts,
            },
            "market_scope": market_scope,
            "policy": "web_volatile_apibara_vpic_fill_missing_static",
            "masked_vin_body_style": {
                "policy": "unanimous_full_vpic_descriptor_consensus",
                "minimum_supporting_full_vins": 2,
                "inferred_records": descriptor_inferred,
                "masked_vin_submitted_to_vpic": False,
            },
        },
    }

    destination = output_path(source, out["mode"], args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"  market:  {source_count} raw -> {len(adapted)} US "
          f"(excluded {source_count - len(adapted)})")
    print(f"  join:    {dict(join_counts)}")
    print(f"  VIN/vPIC:{full_vins} full VIN(s), {vpic_count} vPIC-enriched")
    print(f"  body style: {descriptor_inferred} filled from unanimous full-vPIC "
          "VIN descriptors")
    print(f"  sellers: {dict(sellers)}")
    if args.audit:
        for record in adapted:
            join = record.get("_source_join") or {}
            if join.get("status") == "matched":
                print(f"      lot {record['lot_number']}  {join.get('web_vin_prefix')} -> "
                      f"{record.get('vin')}  [{join.get('source_file')}]")
        for item in conflicts:
            print(f"      !! lot {item['lot_number']} join conflict: {item['conflicts']}")
    print(f"  JSON -> {destination}")
    print("  next: python analytics/scripts/apibara_json2csv_copart_01.py "
          f"{destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
