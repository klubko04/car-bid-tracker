"""
Copart + NHTSA vPIC adapter — stage 1.5 of the analytics pipeline.

    pull_apibara_01.py copart {ended|open|live}
        -> data/{sold|open}/json-raw/copart/apibara_*.json
    copart_vpic_adapt_01.py
        -> data/{sold|open}/json-adapted/copart/vpic_apibara_*.json

The APIBara archive is immutable input and retains every market.  The adapted
copy is intentionally US-only: Canadian and unclassified-market lots are
removed before VIN decoding, with counts and lot numbers recorded under
``adapter.market_scope``.  The script then fills only vehicle-spec fields that
are absent on the Copart record. Existing APIBara values are never overwritten.
The complete set of non-empty vPIC values is retained under
``enrichment.nhtsa_vpic.raw_nonempty`` so a later schema change can be
regenerated without another NHTSA request.

vPIC is a public NHTSA service: no API key or APIBara quota is used.  VINs are
batched at the documented maximum of 50 and cached by VIN so sold/open pulls
reuse a decode.  vPIC recommends supplying model year; when it reports error
12 (VIN/model-year mismatch), the VIN is decoded again without the asserted
year.  The source year is preserved and the disagreement is made explicit.

Examples (run from anywhere):

    python analytics/scripts/copart_vpic_adapt_01.py FILE.json
    python analytics/scripts/copart_vpic_adapt_01.py --all
    python analytics/scripts/copart_vpic_adapt_01.py FILE.json --dry-run
    python analytics/scripts/copart_vpic_adapt_01.py FILE.json --cache-only
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from copart_market import is_us, market

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
PLATFORM = "copart"
RAW_LAYER = "json-raw"
OUT_LAYER = "json-adapted"
MODE_BUCKET = {"ended": "sold", "open": "open", "live": "open"}

VPIC_ENDPOINT = (
    "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"
)
VPIC_HOME = "https://vpic.nhtsa.dot.gov/api/Home/Index"
MAX_BATCH_SIZE = 50
DEFAULT_CACHE = DATA_DIR / "cache" / "nhtsa-vpic" / "vin_decodes.json"
ADAPTER_NAME = "copart_vpic_adapt_01"
ADAPTER_VERSION = 2
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


# vPIC field -> adapted Copart path -> scalar conversion.  These are all
# fill-only. Identity fields (VIN/year/make/model) are validation signals and
# deliberately do not appear here.
SPEC_MAP = (
    ("Trim", ("vehicle_specs", "trim"), "text"),
    ("Series", ("vehicle_specs", "series"), "text"),
    ("BodyClass", ("vehicle_specs", "body_style"), "text"),
    ("Doors", ("vehicle_specs", "doors"), "int"),
    ("Seats", ("vehicle_specs", "seats"), "int"),
    ("SeatRows", ("vehicle_specs", "seat_rows"), "int"),
    ("EngineCylinders", ("vehicle_specs", "engine", "cylinders"), "int"),
    ("DisplacementL", ("vehicle_specs", "engine", "size_l"), "float"),
    ("EngineHP", ("vehicle_specs", "engine", "hp"), "number"),
    ("EngineConfiguration", ("vehicle_specs", "engine", "configuration"), "text"),
    ("EngineModel", ("vehicle_specs", "engine", "model"), "text"),
    ("Turbo", ("vehicle_specs", "engine", "turbo"), "text"),
    ("FuelTypePrimary", ("vehicle_specs", "fuel_type"), "text"),
    ("DriveType", ("vehicle_specs", "drive_type"), "text"),
    ("TransmissionStyle", ("vehicle_specs", "transmission"), "text"),
    ("PlantCountry", ("vehicle_specs", "country_of_origin"), "text"),
    ("Manufacturer", ("vehicle_specs", "manufacturer"), "text"),
    ("VehicleType", ("vehicle_specs", "vehicle_type"), "text"),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def compact_result(result: dict) -> dict:
    """Keep every meaningful vPIC value without hundreds of blank keys."""
    out = {}
    for key, value in (result or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in {"not applicable", "null"}:
                continue
        out[key] = value
    return out


def clean_vin(value) -> str:
    return str(value or "").strip().upper()


def valid_vin(vin: str) -> bool:
    return bool(VIN_RE.fullmatch(vin))


def as_year(value) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 1886 <= year <= 2100 else None


def error_codes(result: dict) -> list[str]:
    raw = str((result or {}).get("ErrorCode") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def decode_ok(result: dict) -> bool:
    # NHTSA can return "0,14" for a partial decode. A decoded VIN/year still
    # has useful data, so only a completely empty result is a hard failure.
    return bool(result and (result.get("VIN") or result.get("ModelYear")))


def scalar(value, kind: str):
    if value is None or value == "":
        return None
    if kind == "text":
        text = str(value).strip()
        return text or None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if kind == "int":
        return int(number) if number.is_integer() else None
    if kind == "number":
        return int(number) if number.is_integer() else number
    return number


def is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def fill_path(record: dict, path: tuple[str, ...], value) -> bool:
    """Set path only when its leaf is absent/empty. Return True if filled."""
    if value is None:
        return False
    node = record
    for part in path[:-1]:
        current = node.get(part)
        if not isinstance(current, dict):
            if not is_empty(current):
                return False
            current = {}
            node[part] = current
        node = current
    leaf = path[-1]
    if not is_empty(node.get(leaf)):
        return False
    node[leaf] = value
    return True


def normalized_text(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def identity_conflicts(record: dict, decoded: dict) -> list[dict]:
    conflicts = []
    pairs = (
        ("vin", clean_vin(record.get("vin")), clean_vin(decoded.get("VIN"))),
        ("year", as_year(record.get("year")), as_year(decoded.get("ModelYear"))),
        ("make", record.get("make"), decoded.get("Make")),
        ("model", record.get("model"), decoded.get("Model")),
    )
    for field, source, vpic in pairs:
        if source in (None, "") or vpic in (None, ""):
            continue
        equal = source == vpic if field == "year" else (
            normalized_text(source) == normalized_text(vpic)
        )
        if not equal:
            conflicts.append({
                "field": field,
                "apibara": source,
                "nhtsa_vpic": vpic,
                "resolution": "kept_apibara",
            })
    return conflicts


def adapt_record(record: dict, cache_entry: dict | None) -> dict:
    """Enrich one copied APIBara record. Exposed for zero-network tests."""
    vin = clean_vin(record.get("vin"))
    source_year = as_year(record.get("year"))
    existing = record.get("enrichment")
    enrichment = existing if isinstance(existing, dict) else {}

    if not valid_vin(vin):
        enrichment["nhtsa_vpic"] = {
            "provider": "NHTSA vPIC",
            "status": "not_decoded",
            "reason": "VIN is missing, masked, or not a valid 17-character VIN",
            "source_vin": vin or None,
            "source_year": source_year,
            "filled_paths": [],
            "conflicts": [],
        }
        record["enrichment"] = enrichment
        return record

    result = (cache_entry or {}).get("result") or {}
    conflicts = identity_conflicts(record, result)
    filled = []
    for field, path, kind in SPEC_MAP:
        value = scalar(result.get(field), kind)
        if fill_path(record, path, value):
            filled.append(".".join(path))

    decoded_year = as_year(result.get("ModelYear"))
    retry = bool((cache_entry or {}).get("retried_without_year"))
    status = "decoded" if decode_ok(result) else "decode_error"
    payload = {
        "provider": "NHTSA vPIC",
        "provider_url": VPIC_HOME,
        "status": status,
        "decoded_at": (cache_entry or {}).get("fetched_at"),
        "source_vin": vin,
        "source_year": source_year,
        "request_model_year": (cache_entry or {}).get("request_model_year"),
        "decoded_year": decoded_year,
        "year_mismatch": bool(
            source_year and decoded_year and source_year != decoded_year
        ),
        "retried_without_year": retry,
        "error_codes": error_codes(result),
        "error_text": result.get("ErrorText"),
        "filled_paths": filled,
        "conflicts": conflicts,
        "raw_nonempty": result,
    }
    validation = (cache_entry or {}).get("validation_result")
    if validation and validation != result:
        payload["year_validation"] = {
            "request_model_year": (cache_entry or {}).get("request_model_year"),
            "error_codes": error_codes(validation),
            "error_text": validation.get("ErrorText"),
            "raw_nonempty": validation,
        }
    enrichment["nhtsa_vpic"] = payload
    record["enrichment"] = enrichment
    return record


def archive_records(data: dict):
    for page in data.get("pages") or []:
        raw = page.get("raw") or {}
        for record in raw.get("data") or []:
            if isinstance(record, dict):
                yield record


def market_scope_summary(data: dict) -> dict:
    counts = Counter()
    lots = {"Canada": [], "unknown": []}
    for record in archive_records(data):
        label = market(record) or "unknown"
        counts[label] += 1
        if label != "UnitedStates":
            lots.setdefault(label, []).append(str(record.get("lot_number") or "?"))
    return {
        "policy": "us_only",
        "source_records": sum(counts.values()),
        "kept_records": counts["UnitedStates"],
        "excluded_records": sum(value for key, value in counts.items()
                                if key != "UnitedStates"),
        "excluded_by_market": {
            key: value for key, value in counts.items() if key != "UnitedStates"
        },
        "excluded_lot_numbers": {key: value for key, value in lots.items() if value},
    }


def apply_us_market_scope(data: dict) -> dict:
    """Mutate a copied archive to US rows only and return auditable counts."""
    summary = market_scope_summary(data)
    for page in data.get("pages") or []:
        raw = page.get("raw") or {}
        records = raw.get("data") or []
        kept = [record for record in records
                if isinstance(record, dict) and is_us(record)]
        page["adapted_counts"] = {
            "source_records": len(records),
            "records": len(kept),
            "excluded_non_us": len(records) - len(kept),
        }
        raw["data"] = kept
    return summary


def validate_archive(path: Path, data: dict) -> None:
    platform = str(data.get("platform") or "").lower()
    mode = str(data.get("mode") or "").lower()
    if str(data.get("source") or "").lower() in {
        "copart-web", "copart-web-adapted",
    }:
        raise ValueError(
            f"{path.name}: this is a Copart web archive, not an APIBara "
            "archive; vPIC needs APIBara's full VIN. Enrich the APIBara archive "
            "first, then pass that vPIC copy to copart_web_adapt_01.py "
            "--enrich-from"
        )
    if platform != PLATFORM:
        raise ValueError(f"{path.name}: platform is {platform!r}, expected 'copart'")
    if mode not in MODE_BUCKET:
        raise ValueError(
            f"{path.name}: mode is {mode!r}, expected ended/open/live"
        )
    if not isinstance(data.get("pages"), list):
        raise ValueError(f"{path.name}: missing APIBara pages list")


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "decodes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read vPIC cache {path}: {exc}") from exc
    if data.get("schema_version") != 1 or not isinstance(data.get("decodes"), dict):
        raise ValueError(f"unsupported vPIC cache shape in {path}")
    return data


def atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def post_batch(items: list[tuple[str, int | None]], timeout: int = 60) -> list[dict]:
    """Decode up to 50 (VIN, optional model year) inputs in one request."""
    if not 1 <= len(items) <= MAX_BATCH_SIZE:
        raise ValueError(f"vPIC batch size must be 1..{MAX_BATCH_SIZE}")
    rows = ";".join(f"{vin},{year or ''}" for vin, year in items)
    body = urllib.parse.urlencode({"format": "json", "data": rows}).encode()
    request = urllib.request.Request(
        VPIC_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "car-bid-tracker/1.0 (NHTSA-vPIC enrichment)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"vPIC HTTP {exc.code}: {detail}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"vPIC request failed: {exc}") from exc
    results = payload.get("Results")
    if not isinstance(results, list) or len(results) != len(items):
        raise RuntimeError(
            f"vPIC returned {len(results) if isinstance(results, list) else 'no'} "
            f"results for {len(items)} inputs"
        )
    return results


def chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def choose_request_years(archives: list[tuple[Path, dict]]) -> dict[str, int | None]:
    """One request year per VIN; conflicting source years intentionally omit it."""
    years: dict[str, list[int]] = {}
    for _, data in archives:
        for record in archive_records(data):
            if not is_us(record):
                continue
            vin = clean_vin(record.get("vin"))
            if not valid_vin(vin):
                continue
            year = as_year(record.get("year"))
            years.setdefault(vin, [])
            if year:
                years[vin].append(year)
    selected = {}
    for vin, values in years.items():
        distinct = set(values)
        selected[vin] = values[0] if len(distinct) == 1 else None
    return selected


def fetch_missing(
    request_years: dict[str, int | None],
    cache: dict,
    *,
    refresh: bool = False,
    batch_size: int = MAX_BATCH_SIZE,
) -> dict:
    """Populate cache; retry year-mismatch code 12 without a model year."""
    decodes = cache["decodes"]
    pending = [
        (vin, year) for vin, year in sorted(request_years.items())
        if refresh or vin not in decodes
    ]
    stats = {
        "network_vins": len(pending),
        "batch_calls": 0,
        "year_retries": 0,
    }
    retry_vins = []
    for batch in chunks(pending, batch_size):
        results = post_batch(batch)
        stats["batch_calls"] += 1
        stamp = utc_now()
        for (vin, year), raw_result in zip(batch, results):
            result = compact_result(raw_result)
            entry = {
                "fetched_at": stamp,
                "request_model_year": year,
                "retried_without_year": False,
                "result": result,
            }
            decodes[vin] = entry
            if "12" in error_codes(result):
                retry_vins.append(vin)

    # Retry in batches too: one malformed source year must not poison the VIN
    # decode. The first response is retained as validation evidence.
    retry_items = [(vin, None) for vin in retry_vins]
    for batch in chunks(retry_items, batch_size):
        results = post_batch(batch)
        stats["batch_calls"] += 1
        stats["year_retries"] += len(batch)
        stamp = utc_now()
        for (vin, _), raw_result in zip(batch, results):
            entry = decodes[vin]
            entry["validation_result"] = entry["result"]
            entry["result"] = compact_result(raw_result)
            entry["retried_without_year"] = True
            entry["fetched_at"] = stamp

    if pending:
        cache["updated_at"] = utc_now()
    return stats


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def output_path(source: Path, data: dict, explicit: str | None = None) -> Path:
    bucket = MODE_BUCKET[str(data.get("mode")).lower()]
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        return path
    return DATA_DIR / bucket / OUT_LAYER / PLATFORM / f"vpic_{source.name}"


def adapt_archive(
    source: Path,
    original: dict,
    cache: dict,
    network_vins: set[str],
    fetch_stats: dict,
) -> tuple[dict, dict]:
    data = copy.deepcopy(original)
    market_scope = apply_us_market_scope(data)
    records = list(archive_records(data))
    valid = {clean_vin(r.get("vin")) for r in records if valid_vin(clean_vin(r.get("vin")))}
    # Empty Open/Live slices are valid snapshots. Seed the summary keys so an
    # empty archive writes a complete zero-valued audit block and the CLI does
    # not crash while printing it.
    stats = Counter({
        "filled_values": 0,
        "identity_conflicts": 0,
        "year_mismatches": 0,
        "decode_errors": 0,
    })
    for record in records:
        vin = clean_vin(record.get("vin"))
        entry = cache["decodes"].get(vin) if valid_vin(vin) else None
        adapt_record(record, entry)
        vp = (record.get("enrichment") or {}).get("nhtsa_vpic") or {}
        stats["filled_values"] += len(vp.get("filled_paths") or [])
        stats["identity_conflicts"] += len(vp.get("conflicts") or [])
        stats["year_mismatches"] += int(bool(vp.get("year_mismatch")))
        stats["decode_errors"] += int(vp.get("status") != "decoded")

    adapted_at = utc_now()
    data["adapted_at"] = adapted_at
    source_counts = copy.deepcopy(original.get("counts") or {})
    data["counts"] = {
        **source_counts,
        "source_records": market_scope["source_records"],
        "records": market_scope["kept_records"],
        "excluded_non_us": market_scope["excluded_records"],
        "excluded_by_market": market_scope["excluded_by_market"],
    }
    data["adapter"] = {
        "name": ADAPTER_NAME,
        "version": ADAPTER_VERSION,
        "source": {
            "path": relpath(source),
            "sha256": file_sha256(source),
            "generated_at": original.get("generated_at"),
        },
        "policy": "fill_missing_only",
        "market_scope": market_scope,
        "nhtsa_vpic": {
            "provider": "NHTSA vPIC",
            "provider_url": VPIC_HOME,
            "endpoint": VPIC_ENDPOINT,
            "cache_path": relpath(DEFAULT_CACHE),
            "records": len(records),
            "valid_vin_records": sum(valid_vin(clean_vin(r.get("vin"))) for r in records),
            "unique_valid_vins": len(valid),
            "cache_hits": len(valid - network_vins),
            "network_vins": len(valid & network_vins),
            "batch_calls_this_run": fetch_stats.get("batch_calls", 0),
            "year_retries_this_run": fetch_stats.get("year_retries", 0),
            **dict(stats),
        },
    }
    return data, data["adapter"]["nhtsa_vpic"]


def raw_candidates() -> list[Path]:
    found = []
    for bucket in BUCKETS:
        # Web pulls have a separate raw contract and require their own adapter.
        # --all must never interpret those archives as empty APIBara pages.
        found.extend(
            (DATA_DIR / bucket / RAW_LAYER / PLATFORM).glob("apibara_*.json")
        )
    return sorted(found, key=lambda p: p.stat().st_mtime)


def resolve_input(value: str) -> Path:
    path = Path(value).expanduser()
    probes = [path]
    if not path.is_absolute():
        probes.append(ROOT / path)
        for bucket in BUCKETS:
            probes.append(DATA_DIR / bucket / RAW_LAYER / PLATFORM / path.name)
    for probe in probes:
        if probe.is_file():
            return probe.resolve()
    raise FileNotFoundError(f"Copart raw archive not found: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich Copart APIBara JSON with free NHTSA vPIC VIN data."
    )
    parser.add_argument("files", nargs="*", metavar="FILE.json")
    parser.add_argument(
        "--all", action="store_true", help="adapt every sold/open Copart raw archive"
    )
    parser.add_argument("--out", help="output path (only with one input archive)")
    parser.add_argument(
        "--cache", default=str(DEFAULT_CACHE), help="shared VIN-decode cache path"
    )
    parser.add_argument(
        "--cache-only", action="store_true",
        help="make no HTTP requests; fail if any valid VIN is not cached",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-decode all VINs instead of using cache"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show inputs/cache misses/outputs; make no request and write nothing",
    )
    parser.add_argument(
        "--batch-size", type=int, default=MAX_BATCH_SIZE, metavar="N",
        help=f"vPIC VINs per request, 1..{MAX_BATCH_SIZE} (default {MAX_BATCH_SIZE})",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"--batch-size must be 1..{MAX_BATCH_SIZE}")
    if args.all and args.files:
        raise SystemExit("use either FILE arguments or --all, not both")

    if args.all:
        paths = raw_candidates()
    elif args.files:
        try:
            paths = [resolve_input(value) for value in args.files]
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from None
    else:
        candidates = raw_candidates()
        paths = candidates[-1:] if candidates else []
    if not paths:
        raise SystemExit("No Copart raw JSON archives found")
    if args.out and len(paths) != 1:
        raise SystemExit("--out is only valid with one input archive")

    archives = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            validate_archive(path, data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(str(exc)) from None
        archives.append((path, data))

    cache_path = Path(args.cache).expanduser()
    if not cache_path.is_absolute():
        cache_path = ROOT / cache_path
    try:
        cache = load_cache(cache_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    initial_cache_vins = set(cache["decodes"])
    request_years = choose_request_years(archives)
    wanted = set(request_years)
    missing = wanted if args.refresh else wanted - initial_cache_vins

    print(f"Copart vPIC adapter: {len(archives)} archive(s)")
    for path, data in archives:
        scope = market_scope_summary(data)
        print(
            f"  {relpath(path)}  mode={data.get('mode')}  "
            f"source={scope['source_records']}  US={scope['kept_records']}  "
            f"excluded={scope['excluded_records']}\n"
            f"    -> {relpath(output_path(path, data, args.out))}"
        )
    print(
        f"  unique valid VINs: {len(wanted)}  cache hits: "
        f"{len(wanted & initial_cache_vins)}  network needed: {len(missing)}"
    )
    if args.dry_run:
        print("Dry run: no network requests and no files written.")
        return 0
    if args.cache_only and missing:
        sample = ", ".join(sorted(missing)[:5])
        raise SystemExit(
            f"--cache-only: {len(missing)} VIN(s) are not cached; first: {sample}"
        )

    if missing:
        estimated_calls = (len(missing) + args.batch_size - 1) // args.batch_size
        print(f"  NHTSA vPIC: decoding {len(missing)} VINs in ~{estimated_calls} batch(es)")
    try:
        fetch_stats = fetch_missing(
            request_years, cache, refresh=args.refresh, batch_size=args.batch_size
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if missing:
        atomic_json_write(cache_path, cache)
        print(
            f"  cache -> {relpath(cache_path)} "
            f"({fetch_stats['batch_calls']} HTTP call(s), "
            f"{fetch_stats['year_retries']} year retry)"
        )

    for path, original in archives:
        adapted, stats = adapt_archive(
            path, original, cache, missing, fetch_stats
        )
        destination = output_path(path, original, args.out)
        # Record a custom cache location correctly when --cache is used.
        adapted["adapter"]["nhtsa_vpic"]["cache_path"] = relpath(cache_path)
        atomic_json_write(destination, adapted)
        print(
            f"  JSON -> {relpath(destination)}\n"
            f"    filled={stats['filled_values']}  "
            f"year_mismatches={stats['year_mismatches']}  "
            f"decode_errors={stats['decode_errors']}"
        )
        print(
            "    next: python analytics/scripts/apibara_json2csv_copart_01.py "
            f"{destination.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
