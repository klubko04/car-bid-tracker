"""
Stage 2 of the analytics pipeline — Copart APIBara JSON -> csv-raw.

    pull_apibara_01.py copart ...
        -> data/{sold|open}/json-raw/copart/apibara_*.json
    copart_vpic_adapt_01.py
        -> data/{sold|open}/json-adapted/copart/vpic_*.json
    apibara_json2csv_copart_01.py
        -> data/{sold|open}/csv-raw/copart/*_copart.csv
    data_pull_01.py copart
        -> data/{sold|open}/csv-cut/copart/*_data_*.csv

Both raw and vPIC-adapted archives are accepted.  The adapted archive is the
preferred input because Copart's APIBara records have no ``details`` block and
therefore omit body style, trim, cylinders, horsepower, manufacturer, and plant
data.  Existing APIBara values remain authoritative; vPIC is fill-only upstream.

This stage is offline.  It flattens and derives values but makes no API calls.
Unfiltered output is the canonical csv-raw extract.  Filters are supported for
exploration, but filtered output gets a distinct filename; persistent filtering
belongs in data_pull_01.py and its csv-cut layer.

Examples:

    python analytics/scripts/apibara_json2csv_copart_01.py
    python analytics/scripts/apibara_json2csv_copart_01.py FILE.json
    python analytics/scripts/apibara_json2csv_copart_01.py --all
    python analytics/scripts/apibara_json2csv_copart_01.py --schema
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
MODE_BUCKET = {"ended": "sold", "open": "open", "live": "open"}
PLATFORM = "copart"
ARRAY_SEP = "|"


def layer_dir(mode, layer, platform=PLATFORM):
    bucket = MODE_BUCKET.get(str(mode or "").lower(), "sold")
    return DATA_DIR / bucket / layer / platform


SEARCH_DIRS = [DATA_DIR / b / "json-raw" / PLATFORM for b in BUCKETS] + [
    DATA_DIR / b / "json-adapted" / PLATFORM for b in BUCKETS
]

sys.path.insert(0, str(ROOT))
from app.branch_geo import (  # noqa: E402
    BUCKET_STEP,
    ROAD_FACTOR,
    coords_for_location,
    distance_miles,
    haversine_mi,
)
import copart_seller  # noqa: E402
from copart_market import branch_state, is_us, market  # noqa: E402


# ---------------------------------------------------------------------------
# safe access and scalar helpers
# ---------------------------------------------------------------------------
def g(data, *path, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def clean(value):
    text = str(value).strip() if value is not None else ""
    if not text or text.lower() in {"unknown", "none", "n/a", "null"}:
        return None
    return value


def money_num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    return float(match.group(0).replace(",", "")) if match else None


def money_num_with_zero(value):
    """Numeric money that preserves an observed zero (not valid for Buy Now)."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    result = float(match.group(0).replace(",", ""))
    return result if result >= 0 else None


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if numerator and denominator else None


def as_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text == "true" else False if text == "false" else None


def vpic(v):
    return g(v, "enrichment", "nhtsa_vpic", default={}) or {}


def attrs(v):
    """vPIC raw values; retained name satisfies data_pull_01's flattener API."""
    return g(v, "enrichment", "nhtsa_vpic", "raw_nonempty", default={}) or {}


def copart_csv(v):
    """Future Copart-sales-CSV enrichment block, empty until that stage exists."""
    return g(v, "enrichment", "copart_sales_csv", default={}) or {}


def copart_web(v):
    """Auditable public-web values retained by copart_web_adapt_01."""
    return g(v, "enrichment", "copart_web", default={}) or {}


def first(data, *keys):
    for key in keys:
        value = clean(data.get(key)) if isinstance(data, dict) else None
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# market and currency
# ---------------------------------------------------------------------------
def currency(v):
    return "CAD" if market(v) == "Canada" else "USD" if market(v) else None


def usd(v, value):
    """Return a number only for positively identified US lots."""
    return money_num(value) if currency(v) == "USD" else None


def usd_with_zero(v, value):
    return money_num_with_zero(value) if currency(v) == "USD" else None


# ---------------------------------------------------------------------------
# vehicle, damage, seller, listing state
# ---------------------------------------------------------------------------
_DAMAGE_GROUPS = {
    "REAR-SIDE": (
        "rear", "rear end", "side", "left side", "right side", "hail",
        "minor dent/scratches", "normal wear",
    ),
    "FRONT": ("front", "front end", "front & rear", "top/roof"),
}


def damage_group(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return ""
    for group, names in _DAMAGE_GROUPS.items():
        if text in names:
            return group
    return "OTHER"


def seller_detail(v):
    """Shared-taxonomy classification for one record.

    Name-first, because APIBara's ``seller.type`` is wrong for named companies:
    on the 2018-2023 Audi S5 ended cohort it called *Csaa* non_insurance/unknown
    and *Santander*, *Bridgecrest Acceptance* and *Gmfinancials* non_insurance.
    See copart_seller for the evidence and the registry.

    An upstream stage that already resolved the seller wins outright. Without
    this the flattener silently re-derives from seller.name/seller.type and
    discards the whole enrichment: a stat.vin `dealer` verdict, which exists
    precisely so those lots can be excluded, would never reach the filter.
    """
    resolved = g(v, "seller", "classification")
    if isinstance(resolved, dict) and resolved.get("class") in copart_seller.CLASSES:
        return resolved
    return copart_seller.classify(
        clean(g(v, "seller", "name")), clean(g(v, "seller", "type")), source="seller.name"
    )


def seller_class(v):
    """-> insurance | finance | dealer | non_insurance | unknown.

    ``finance`` and ``non_insurance`` replace the old catch-all ``other``: a
    repossession is a fundamentally different vehicle from a fleet or salvage
    reseller consignment, and merging them hid every lender lot in the cohort.
    """
    return seller_detail(v)["class"]


def listing_state(v):
    mode = str(v.get("_mode") or "").lower()
    state = str(g(v, "auction", "state") or "").strip()
    if mode == "ended" or g(v, "auction", "last_sold_day") or state == "finished":
        return "Ended"
    if mode == "live" or state.lower() == "live":
        return "Live"
    if g(v, "auction", "is_buy_now"):
        return "Open/BuyNow"
    if mode == "open":
        return "Open"
    return state or None


def bid_condition(v):
    """Human-readable Copart bid condition without losing the raw fields."""
    bid_type = clean(g(v, "auction", "bid_type"))
    reserve_met = as_bool(g(v, "auction", "seller_reserve_met"))
    normalized = re.sub(r"[^a-z]+", " ", str(bid_type or "").casefold()).strip()
    if normalized == "minimum bid":
        if reserve_met is True:
            return "Minimum Bid: Seller reserve met"
        if reserve_met is False:
            return "Minimum Bid: Seller reserve not yet met"
    return bid_type


# ---------------------------------------------------------------------------
# distance — exact facility coordinates first, city/state approximation second
# ---------------------------------------------------------------------------
def branch_coords(v):
    try:
        lat = float(g(v, "facility", "lat"))
        lng = float(g(v, "facility", "lng"))
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None, None
    return lat, lng


def distance_source(v):
    lat, _ = branch_coords(v)
    if lat is not None:
        return "facility_coordinates"
    display = g(v, "location", "display")
    return "location_display_approx" if coords_for_location(display) else None


def distance_mi(v):
    lat, lng = branch_coords(v)
    if lat is not None:
        return round(haversine_mi(lat, lng) * ROAD_FACTOR)
    approximate = distance_miles(g(v, "location", "display"))
    return round(approximate) if approximate is not None else None


def distance_bucket(v):
    miles = distance_mi(v)
    if miles is None:
        return None
    bucket = max(BUCKET_STEP, int(math.ceil(miles / BUCKET_STEP) * BUCKET_STEP))
    return f"{bucket}mi"


# ---------------------------------------------------------------------------
# media and vPIC audit fields
# ---------------------------------------------------------------------------
def image_urls(v):
    urls = []
    for item in g(v, "media", "items", default=[]) or []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        url = item.get("large") or item.get("full") or item.get("thumb")
        if url and url not in urls:
            urls.append(url)
    if not urls:
        urls = list(g(v, "media", "thumbs", default=[]) or [])
    return urls


def video_urls(v):
    return [
        item.get("url")
        for item in g(v, "media", "items", default=[]) or []
        if isinstance(item, dict) and item.get("type") == "video" and item.get("url")
    ]


def vpic_conflict_fields(v):
    fields = [
        str(item.get("field"))
        for item in vpic(v).get("conflicts") or []
        if isinstance(item, dict) and item.get("field")
    ]
    return ARRAY_SEP.join(fields) or None


def vpic_error_codes(v):
    return ARRAY_SEP.join(str(x) for x in vpic(v).get("error_codes") or []) or None


def csv_acv(v):
    data = copart_csv(v)
    # Estimated Retail Value is not ACV. Only an explicitly ACV-labelled
    # member-feed field may populate acv_usd.
    return money_num(first(data, "acv_usd", "acv"))


def estimated_retail(v):
    direct = money_num(g(v, "pricing", "estimated_retail_value_usd"))
    if direct is not None:
        return direct
    data = copart_csv(v)
    return money_num(first(data, "estimated_retail_value", "retail_value"))


def csv_repair(v):
    data = copart_csv(v)
    return money_num(first(data, "estimated_repair_cost", "repair_cost", "est_repair_usd"))


# ---------------------------------------------------------------------------
# CSV schema — one row per lot
# ---------------------------------------------------------------------------
SCHEMA = [
    # identity
    ("lot_number", lambda v: v.get("lot_number"), "raw"),
    ("vin", lambda v: v.get("vin"), "raw"),
    ("year", lambda v: v.get("year"), "raw"),
    ("make", lambda v: v.get("make"), "raw"),
    ("model", lambda v: v.get("model"), "raw"),
    ("trim", lambda v: g(v, "vehicle_specs", "trim"), "raw"),
    ("series", lambda v: g(v, "vehicle_specs", "series"), "raw"),
    ("listing_title", lambda v: v.get("title"), "raw"),
    ("lot_url", lambda v: f"https://www.copart.com/lot/{v.get('lot_number')}"
     if v.get("lot_number") else None, "calc"),
    ("market", market, "calc"),
    ("currency", currency, "calc"),

    # body and specs
    ("body_style", lambda v: g(v, "vehicle_specs", "body_style"), "raw"),
    ("doors", lambda v: g(v, "vehicle_specs", "doors"), "raw"),
    ("vehicle_type", lambda v: g(v, "vehicle_specs", "vehicle_type"), "raw"),
    ("manufacturer", lambda v: g(v, "vehicle_specs", "manufacturer"), "raw"),
    ("engine_raw", lambda v: g(v, "vehicle_specs", "engine", "raw"), "raw"),
    ("engine_size_l", lambda v: g(v, "vehicle_specs", "engine", "size_l"), "raw"),
    ("engine_hp", lambda v: g(v, "vehicle_specs", "engine", "hp"), "raw"),
    ("cylinders", lambda v: g(v, "vehicle_specs", "engine", "cylinders"), "raw"),
    ("engine_configuration", lambda v: g(v, "vehicle_specs", "engine", "configuration"), "raw"),
    ("engine_model", lambda v: g(v, "vehicle_specs", "engine", "model"), "raw"),
    ("engine_turbo", lambda v: g(v, "vehicle_specs", "engine", "turbo"), "raw"),
    ("transmission", lambda v: g(v, "vehicle_specs", "transmission"), "raw"),
    ("drive_type", lambda v: g(v, "vehicle_specs", "drive_type"), "raw"),
    ("fuel_type", lambda v: g(v, "vehicle_specs", "fuel_type"), "raw"),
    ("exterior_color", lambda v: g(v, "vehicle_specs", "exterior_color"), "raw"),
    ("seats", lambda v: g(v, "vehicle_specs", "seats"), "raw"),
    ("seat_rows", lambda v: g(v, "vehicle_specs", "seat_rows"), "raw"),
    ("country_of_origin", lambda v: g(v, "vehicle_specs", "country_of_origin"), "raw"),

    # condition and title
    ("primary_damage", lambda v: g(v, "condition", "primary_damage"), "raw"),
    ("secondary_damage", lambda v: g(v, "condition", "secondary_damage"), "raw"),
    ("primary_damage_group", lambda v: damage_group(g(v, "condition", "primary_damage")), "calc"),
    ("secondary_damage_group",
     lambda v: damage_group(g(v, "condition", "secondary_damage")), "calc"),
    ("run_condition", lambda v: g(v, "condition", "run_condition", "value"), "raw"),
    ("has_key", lambda v: g(v, "condition", "has_key"), "raw"),
    ("odometer_mi", lambda v: g(v, "odometer", "mi"), "raw"),
    ("sale_document", lambda v: g(v, "sale_document", "name"), "raw"),
    ("sale_document_group", lambda v: g(v, "sale_document", "sale_document_group"), "raw"),
    ("sale_document_pending", lambda v: g(v, "sale_document", "is_pending"), "raw"),
    ("export_allowed", lambda v: g(v, "sale_document", "export"), "raw"),
    ("registration_allowed", lambda v: g(v, "sale_document", "registration"), "raw"),

    # money. Native values are retained for Canada; *_usd is strictly US.
    ("last_sold_price_native", lambda v: money_num(g(v, "pricing", "last_sold_price_usd")), "calc"),
    ("current_bid_native",
     lambda v: money_num_with_zero(g(v, "pricing", "current_bid_usd")), "calc"),
    ("buy_now_native", lambda v: money_num(g(v, "pricing", "buy_now_usd")), "calc"),
    ("estimated_retail_value_native",
     lambda v: estimated_retail(v), "calc"),
    ("last_sold_price_usd", lambda v: usd(v, g(v, "pricing", "last_sold_price_usd")), "calc"),
    ("current_bid_usd",
     lambda v: usd_with_zero(v, g(v, "pricing", "current_bid_usd")), "calc"),
    ("buy_now_usd", lambda v: usd(v, g(v, "pricing", "buy_now_usd")), "calc"),
    ("estimated_retail_value_usd",
     lambda v: usd(v, estimated_retail(v)), "calc"),
    ("acv_usd", lambda v: usd(v, csv_acv(v)), "calc"),
    ("est_repair_usd", lambda v: usd(v, csv_repair(v)), "calc"),
    # Raw candidate fields remain visible without asserting unverified
    # semantics. `la` is mapped above only because the Copart UI/CSV labels it
    # ERV; lotPlugAcv and rc still need a first-party/member-feed contract.
    ("copart_lot_plug_acv_raw", lambda v: money_num(copart_web(v).get("lotPlugAcv")), "raw"),
    ("copart_rc_raw", lambda v: money_num(copart_web(v).get("rc")), "raw"),
    ("repair_to_acv", lambda v: ratio(csv_repair(v), csv_acv(v)), "calc"),
    ("sold_to_acv", lambda v: ratio(
        money_num(g(v, "pricing", "last_sold_price_usd")), csv_acv(v)), "calc"),
    ("apibara_estimated_cost_from", lambda v: g(v, "pricing", "estimated_cost", "from"), "raw"),
    ("apibara_estimated_cost_to", lambda v: g(v, "pricing", "estimated_cost", "to"), "raw"),
    ("apibara_estimated_cost_text", lambda v: g(v, "pricing", "estimated_cost", "text"), "raw"),

    # auction
    ("auction_at", lambda v: g(v, "auction", "auction_at") or v.get("ad"), "raw"),
    ("last_sold_day", lambda v: g(v, "auction", "last_sold_day"), "raw"),
    ("last_sold_status", lambda v: g(v, "auction", "last_sold_status"), "raw"),
    ("listing_state", listing_state, "calc"),
    ("bid_type", lambda v: clean(g(v, "auction", "bid_type")), "raw"),
    ("sale_status_raw", lambda v: clean(g(v, "auction", "sale_status")), "raw"),
    ("seller_reserve_met",
     lambda v: as_bool(g(v, "auction", "seller_reserve_met")), "raw"),
    ("bid_condition", bid_condition, "calc"),
    ("is_timed", lambda v: as_bool(g(v, "auction", "is_timed")), "raw"),
    ("is_buy_now", lambda v: as_bool(g(v, "auction", "is_buy_now")), "raw"),
    ("buy_now_sold", lambda v: as_bool(g(v, "auction", "sold_buy_now")), "raw"),
    ("sold_timed", lambda v: as_bool(g(v, "auction", "sold_timed")), "raw"),
    ("sublot", lambda v: as_bool(v.get("subLot")), "raw"),
    ("auction_item_number", lambda v: g(v, "auction", "item_number"), "raw"),

    # seller and location
    ("seller_name", lambda v: clean(g(v, "seller", "name")), "raw"),
    ("seller_class", seller_class, "calc"),
    ("seller_class_basis", lambda v: seller_detail(v)["basis"], "calc"),
    ("seller_identity_withheld",
     lambda v: as_bool(seller_detail(v)["identity_withheld"]), "calc"),
    ("seller_type", lambda v: clean(g(v, "seller", "type")), "raw"),
    ("selling_branch", lambda v: clean(g(v, "location", "display")), "raw"),
    ("branch_state", branch_state, "calc"),
    ("branch_zip", lambda v: clean(g(v, "facility", "zip")), "raw"),
    ("branch_lat", lambda v: branch_coords(v)[0], "raw"),
    ("branch_lng", lambda v: branch_coords(v)[1], "raw"),
    ("send_from", lambda v: clean(g(v, "location", "send_from")), "raw"),
    ("distance_mi", distance_mi, "calc"),
    ("distance_bucket", distance_bucket, "calc"),
    ("distance_source", distance_source, "calc"),

    # media
    ("image_count", lambda v: g(v, "media", "thumbs_count") or len(image_urls(v)), "raw"),
    ("copart_image_urls", lambda v: ARRAY_SEP.join(image_urls(v)) or None, "calc"),
    ("video_count", lambda v: len(video_urls(v)), "calc"),
    ("copart_video_url", lambda v: video_urls(v)[0] if video_urls(v) else None, "calc"),

    # vPIC audit — blank on raw, non-adapted input
    ("vpic_status", lambda v: clean(vpic(v).get("status")), "raw"),
    ("vpic_decoded_at", lambda v: clean(vpic(v).get("decoded_at")), "raw"),
    ("vpic_decoded_year", lambda v: clean(vpic(v).get("decoded_year")), "raw"),
    ("vpic_year_mismatch", lambda v: vpic(v).get("year_mismatch"), "raw"),
    ("vpic_error_codes", vpic_error_codes, "calc"),
    ("vpic_conflict_fields", vpic_conflict_fields, "calc"),

    # provenance
    ("source_file", lambda v: v.get("_source_file"), "calc"),
    ("raw_source_file", lambda v: v.get("_raw_source_file"), "calc"),
    ("pulled_at", lambda v: v.get("_pulled_at"), "calc"),
    ("adapted_at", lambda v: v.get("_adapted_at"), "calc"),
]

COLUMNS = [column for column, _, _ in SCHEMA]


def flatten(v):
    return {column: function(v) for column, function, _ in SCHEMA}


SOURCE_HINTS = {
    "lot_number": "lot_number", "vin": "vin", "year": "year",
    "make": "make", "model": "model", "trim": "vehicle_specs.trim (vPIC fill)",
    "series": "vehicle_specs.series (vPIC fill)", "listing_title": "title",
    "lot_url": "https://www.copart.com/lot/{lot_number}",
    "bid_type": "auction.bid_type (Copart ess)",
    "sale_status_raw": "auction.sale_status (dynamicLotDetails.saleStatus)",
    "seller_reserve_met": "auction.seller_reserve_met",
    "bid_condition": "derived from bid_type + seller_reserve_met",
    "market": "derived from location.display region", "currency": "USD/CAD from market",
    "body_style": "vehicle_specs.body_style", "doors": "vehicle_specs.doors",
    "vehicle_type": "vehicle_specs.vehicle_type", "manufacturer": "vehicle_specs.manufacturer",
    "engine_raw": "vehicle_specs.engine.raw", "engine_size_l": "vehicle_specs.engine.size_l",
    "engine_hp": "vehicle_specs.engine.hp", "cylinders": "vehicle_specs.engine.cylinders",
    "engine_configuration": "vehicle_specs.engine.configuration",
    "engine_model": "vehicle_specs.engine.model", "engine_turbo": "vehicle_specs.engine.turbo",
    "transmission": "vehicle_specs.transmission", "drive_type": "vehicle_specs.drive_type",
    "fuel_type": "vehicle_specs.fuel_type", "exterior_color": "vehicle_specs.exterior_color",
    "seats": "vehicle_specs.seats", "seat_rows": "vehicle_specs.seat_rows",
    "country_of_origin": "vehicle_specs.country_of_origin",
    "primary_damage": "condition.primary_damage", "secondary_damage": "condition.secondary_damage",
    "primary_damage_group": "REAR-SIDE | FRONT | OTHER",
    "secondary_damage_group": "same grouping applied to secondary_damage",
    "run_condition": "condition.run_condition.value", "has_key": "condition.has_key",
    "odometer_mi": "odometer.mi", "sale_document": "sale_document.name",
    "sale_document_group": "sale_document.sale_document_group",
    "sale_document_pending": "sale_document.is_pending", "export_allowed": "sale_document.export",
    "registration_allowed": "sale_document.registration",
    "last_sold_price_native": "pricing.last_sold_price_usd as supplied; currency column applies",
    "current_bid_native": "pricing.current_bid_usd as supplied; currency column applies",
    "buy_now_native": "pricing.buy_now_usd as supplied; currency column applies",
    "estimated_retail_value_native":
        "pricing.estimated_retail_value_usd (`la`) as supplied; currency applies",
    "last_sold_price_usd": "native price only when market=UnitedStates",
    "current_bid_usd": "native bid only when market=UnitedStates",
    "buy_now_usd": "native buy-now only when market=UnitedStates",
    "estimated_retail_value_usd":
        "Copart web `la`, verified as seller-submitted Estimated Retail Value; US only",
    "acv_usd": "future enrichment.copart_sales_csv explicit ACV only (never ERV)",
    "est_repair_usd": "future enrichment.copart_sales_csv repair estimate",
    "copart_lot_plug_acv_raw":
        "enrichment.copart_web.lotPlugAcv; semantics intentionally unasserted",
    "copart_rc_raw": "enrichment.copart_web.rc; semantics intentionally unasserted",
    "repair_to_acv": "est_repair_usd / acv_usd", "sold_to_acv": "sale / acv",
    "apibara_estimated_cost_from": "pricing.estimated_cost.from (APIBara label retained)",
    "apibara_estimated_cost_to": "pricing.estimated_cost.to (APIBara label retained)",
    "apibara_estimated_cost_text": "pricing.estimated_cost.text",
    "auction_at": "auction.auction_at", "last_sold_day": "auction.last_sold_day",
    "last_sold_status": "auction.last_sold_status", "listing_state": "derived from mode/auction",
    "is_timed": "auction.is_timed", "is_buy_now": "auction.is_buy_now",
    "buy_now_sold": "auction.sold_buy_now", "sold_timed": "auction.sold_timed",
    "sublot": "subLot", "auction_item_number": "auction.item_number (`aan`)",
    "seller_name": "seller.name",
    "seller_class": "copart_seller.classify: registry/name > seller.type",
    "seller_class_basis": "which classifier rule fired",
    "seller_identity_withheld": "company identity absent (APIBara placeholder name)",
    "seller_type": "seller.type (raw; unreliable — see seller_class)",
    "selling_branch": "location.display",
    "branch_state": "region parsed from location.display", "branch_zip": "facility.zip",
    "branch_lat": "facility.lat", "branch_lng": "facility.lng", "send_from": "location.send_from",
    "distance_mi": "facility coordinates, else app.branch_geo location approximation",
    "distance_bucket": "distance_mi rounded up to 250mi", "distance_source": "exact vs approximate",
    "image_count": "media.thumbs_count", "copart_image_urls": "media.items image URLs, pipe-joined",
    "video_count": "media.items[type=video] count", "copart_video_url": "first video URL",
    "vpic_status": "enrichment.nhtsa_vpic.status", "vpic_decoded_at": "vPIC decoded_at",
    "vpic_decoded_year": "vPIC decoded_year", "vpic_year_mismatch": "vPIC year_mismatch",
    "vpic_error_codes": "vPIC error_codes, pipe-joined",
    "vpic_conflict_fields": "vPIC conflict fields",
    "source_file": "archive containing the selected observation",
    "raw_source_file": "adapter.source.path when input is vPIC-adapted",
    "pulled_at": "archive generated_at", "adapted_at": "archive adapted_at",
}


# ---------------------------------------------------------------------------
# filtering and observation merge
# ---------------------------------------------------------------------------
def damage_text(v):
    return " ".join(str(g(v, "condition", key) or "")
                    for key in ("primary_damage", "secondary_damage")).lower()


def norm_style(value):
    return re.sub(r"[\s_-]+", "/", str(value or "").strip().lower())


def style_matches(value, selector):
    """Match exact styles plus the body families used by final cuts.

    vPIC may report ``Convertible/Cabriolet`` while Copart web reports
    ``CONVERTIBLE``; vPIC also emits compound four-door values such as
    ``Hatchback/Liftback/Notchback`` and ``Sedan/Saloon``. Treat those
    representations as families without making unrelated filters fuzzy.
    """
    style = norm_style(value)
    wanted = norm_style(selector)
    if style == wanted:
        return True
    tokens = {token for token in re.split(r"[^a-z0-9]+", style) if token}
    if wanted == "coupe":
        return "coupe" in tokens
    if wanted in {"convertible", "cabriolet"}:
        return bool(tokens & {"convertible", "cabriolet"})
    if wanted in {"hatchback", "liftback", "notchback"}:
        return bool(tokens & {"hatchback", "liftback", "notchback"})
    if wanted in {"sedan", "saloon"}:
        return bool(tokens & {"sedan", "saloon"})
    return False


def market_key(value):
    text = re.sub(r"[^a-z]", "", str(value or "").lower())
    if text in {"us", "usa", "unitedstates", "unitedstatesofamerica"}:
        return "unitedstates"
    if text in {"ca", "can", "canada"}:
        return "canada"
    return text


def keep(v, filters):
    if filters["exclude_damage"]:
        hit = next((x for x in filters["exclude_damage"] if x in damage_text(v)), None)
        if hit:
            return False, f"damage~{hit}"
    if filters["include_damage"] and not any(
        x in damage_text(v) for x in filters["include_damage"]
    ):
        return False, f"damage!~{'/'.join(filters['include_damage'])}"

    raw_style = g(v, "vehicle_specs", "body_style")
    style = norm_style(raw_style)
    if filters["body_styles"] and not any(
        style_matches(raw_style, candidate) for candidate in filters["body_styles"]
    ):
        return False, f"body_style={style or 'null'}"
    excluded_style = next((
        candidate for candidate in filters["exclude_body_styles"]
        if style_matches(raw_style, candidate)
    ), None)
    if excluded_style:
        return False, f"body_style={style} excluded"

    cls = seller_class(v)
    if filters["seller_classes"] and cls not in filters["seller_classes"]:
        return False, f"seller_class={cls}"
    if cls in filters["exclude_seller_classes"]:
        return False, f"seller_class={cls} excluded"
    if filters["markets"] and market_key(market(v)) not in {
        market_key(x) for x in filters["markets"]
    }:
        return False, f"market={market(v) or 'unknown'}"
    if filters["min_photos"] and (g(v, "media", "thumbs_count") or 0) < filters["min_photos"]:
        return False, f"photos={g(v, 'media', 'thumbs_count') or 0}"
    if filters["sold_only"] and not money_num(g(v, "pricing", "last_sold_price_usd")):
        return False, "no sold price"
    if filters["max_odometer"]:
        odometer = g(v, "odometer", "mi")
        if odometer is None:
            return False, "odometer unknown"
        if odometer > filters["max_odometer"]:
            return False, f"odometer={int(odometer)}"
    if filters["max_distance"]:
        miles = distance_mi(v)
        if miles is None:
            return False, "distance unknown"
        if miles > filters["max_distance"]:
            return False, f"distance={miles}mi"
    return True, ""


def _fill_missing(target, source):
    """Recursively fill blank static values without replacing newer values."""
    for key, value in (source or {}).items():
        current = target.get(key)
        if isinstance(value, dict):
            if not isinstance(current, dict):
                if current not in (None, "", [], {}):
                    continue
                current = {}
                target[key] = current
            _fill_missing(current, value)
        elif current in (None, "", [], {}) and value not in (None, "", [], {}):
            target[key] = copy.deepcopy(value)


def observation_key(record):
    """Cross-source identity: Copart lot number, never the VIN spelling.

    The public web row masks the VIN while APIBara exposes all 17 characters.
    Including VIN in this key splits one real lot into two rows. The web adapter
    validates year/make/model/VIN-prefix before it accepts enrichment; stage 2
    can therefore use Copart's own lot number as the listing identity.
    """
    platform = str(record.get("platform") or PLATFORM).casefold()
    lot = str(record.get("lot_number") or "").strip()
    if lot.endswith(".0"):
        lot = lot[:-2]
    if lot:
        return platform, "lot", lot
    vin = str(record.get("vin") or "").strip().upper()
    return platform, "vin", vin


def _full_vin(value):
    return bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", str(value or "").strip().upper()))


def merge_observations(observations):
    """Newest volatile state plus best available cross-source static facts."""
    if len(observations) == 1:
        return observations[0]
    ordered = sorted(observations, key=lambda v: v.get("_pulled_at") or "")
    newest = ordered[-1]
    merged = copy.deepcopy(newest)

    # Static blocks only fill gaps. This lets Copart web contribute secondary
    # damage/coordinates/trim while vPIC contributes doors/HP/manufacturer.
    for source in ordered:
        for block in ("vehicle_specs", "condition", "odometer", "sale_document",
                      "location", "facility"):
            _fill_missing(merged.setdefault(block, {}), source.get(block) or {})

    # Full VIN strictly improves a masked public VIN; never downgrade it on a
    # newer web-only observation.
    full = [v for v in ordered if _full_vin(v.get("vin"))]
    if full:
        merged["vin"] = full[-1]["vin"]

    # Prefer a classified/named seller over an absent seller. Name-first
    # classification remains centralized in copart_seller.
    def seller_rank(record):
        detail = seller_detail(record)
        return (int(detail["class"] != "unknown"),
                int(bool(detail.get("name"))),
                int(not detail.get("identity_withheld")))

    seller_source = max(ordered, key=seller_rank)
    if seller_rank(seller_source) > (0, 0, 0):
        merged["seller"] = copy.deepcopy(seller_source.get("seller") or {})

    # A matched APIBara observation carries every lot image; a web-only record
    # carries one thumbnail. Keep the richer list without touching live prices.
    media_source = max(
        ordered,
        key=lambda v: len((v.get("media") or {}).get("items") or []),
    )
    if (media_source.get("media") or {}).get("items"):
        merged["media"] = copy.deepcopy(media_source["media"])

    enriched = [v for v in ordered if vpic(v)]
    if enriched:
        richest = enriched[-1]
        _fill_missing(merged.setdefault("vehicle_specs", {}),
                      richest.get("vehicle_specs") or {})
        merged.setdefault("enrichment", {})["nhtsa_vpic"] = copy.deepcopy(vpic(richest))
        merged["_adapted_at"] = richest.get("_adapted_at")
        merged["_raw_source_file"] = richest.get("_raw_source_file")
    merged["_merged_from"] = sorted({
        item.get("_source_file") for item in observations if item.get("_source_file")
    })
    return merged


# ---------------------------------------------------------------------------
# input resolution and provenance
# ---------------------------------------------------------------------------
def load_records(paths):
    output = []
    for path in paths:
        path = Path(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        platform = str(document.get("platform") or "").lower()
        if platform and platform != PLATFORM:
            print(f"  !! skipping {path.name}: platform={platform}, converter is Copart-only")
            continue
        pulled_at = document.get("generated_at")
        adapted_at = document.get("adapted_at")
        raw_source = g(document, "adapter", "source", "path")
        mode = str(document.get("mode") or "ended").lower()
        count = 0
        excluded = {"Canada": [], "unknown": []}
        for page in document.get("pages") or []:
            if page.get("status") != 200:
                continue
            for record in g(page, "raw", "data", default=[]) or []:
                if not isinstance(record, dict):
                    continue
                record_market = market(record)
                if not is_us(record):
                    excluded[record_market or "unknown"].append(
                        str(record.get("lot_number") or "?")
                    )
                    continue
                record["_source_file"] = path.name
                record["_raw_source_file"] = raw_source
                record["_pulled_at"] = pulled_at
                record["_adapted_at"] = adapted_at
                record["_mode"] = mode
                record["_platform"] = platform or PLATFORM
                output.append(record)
                count += 1
        kind = "vPIC-adapted" if adapted_at else "raw"
        print(f"  loaded {count:>4} record(s) from {path.name}  [{mode}, {kind}]")
        for excluded_market, lots in excluded.items():
            if lots:
                print(
                    f"      excluded {len(lots)} {excluded_market} lot(s) "
                    f"(US-only guard): {', '.join(lots[:8])}"
                )
    return output


def resolve_one(filename):
    path = Path(filename).expanduser()
    if path.is_absolute() or path.exists():
        return path
    for directory in SEARCH_DIRS:
        candidate = directory / filename
        if candidate.exists():
            return candidate
    looked = "\n    ".join(str(directory / filename) for directory in SEARCH_DIRS)
    raise SystemExit(f"input not found: {filename}\n  looked in ./{filename}\n    {looked}")


def resolve_inputs(args):
    if args.files:
        return [resolve_one(filename) for filename in args.files]
    pool = sorted(
        (path for bucket in BUCKETS for layer in ("json-raw", "json-adapted")
         for path in (DATA_DIR / bucket / layer / PLATFORM).glob("*.json")),
        key=lambda path: path.stat().st_mtime,
    )
    if not pool:
        raise SystemExit(
            f"no Copart JSON archives under {DATA_DIR}/{{sold|open}}/"
            f"{{json-raw|json-adapted}}/{PLATFORM}"
        )
    return pool if args.all else [pool[-1]]


# ---------------------------------------------------------------------------
# CLI and output
# ---------------------------------------------------------------------------
def multiword(value):
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else " ".join(map(str, value)).strip()


def csv_list(value):
    return [item.strip().lower() for item in multiword(value).split(",") if item.strip()]


def style_set(values):
    output = set()
    for occurrence in values or []:
        for token in multiword(occurrence).split(","):
            if token.strip():
                output.add(norm_style(token))
    return output


def print_schema():
    print(f"{'csv column':<31} {'kind':<5} source")
    print("-" * 112)
    for column, _, kind in SCHEMA:
        print(f"{column:<31} {kind:<5} {SOURCE_HINTS.get(column, '')}")
    calculated = sum(kind == "calc" for _, _, kind in SCHEMA)
    print(
        f"\n{len(COLUMNS)} columns — {len(COLUMNS) - calculated} raw/fill values, "
        f"{calculated} derived here."
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="apibara_json2csv_copart_01.py",
        description="Flatten raw or vPIC-adapted Copart JSON to csv-raw. Offline.",
    )
    parser.add_argument("files", nargs="*", help="archive JSON (default: newest)")
    parser.add_argument("--all", action="store_true", help="use all raw/adapted Copart archives")
    parser.add_argument("--exclude-damage", nargs="+", metavar="a,b,c")
    parser.add_argument("--include-damage", nargs="+", metavar="a,b,c")
    parser.add_argument("--body-style", action="append", nargs="+", default=[], metavar="STYLE")
    parser.add_argument(
        "--exclude-body-style", action="append", nargs="+", default=[],
        metavar="STYLE",
    )
    parser.add_argument(
        "--seller-class", action="append", default=[],
        choices=["insurance", "finance", "dealer", "non_insurance", "unknown"],
        help="keep only these seller classes; 'other' split into "
             "finance/non_insurance in this version",
    )
    parser.add_argument(
        "--exclude-seller-class", action="append", default=[],
        choices=["insurance", "finance", "dealer", "non_insurance", "unknown"],
        help="drop these seller classes. Applied AFTER --seller-class, so an "
             "exclusion always wins. Unlike --seller-class this does not turn "
             "into a whitelist, so `unknown` lots survive unless named here",
    )
    parser.add_argument("--min-photos", type=int, default=0)
    parser.add_argument("--market", action="append", default=[], metavar="MARKET")
    parser.add_argument("--max-odometer", type=int, default=0, metavar="MILES")
    parser.add_argument("--max-distance", type=int, default=0, metavar="MILES")
    parser.add_argument("--sold-only", action="store_true")
    parser.add_argument("--out", help="output CSV path (relative -> csv-raw/copart)")
    parser.add_argument("--schema", action="store_true", help="print column mapping and exit")
    return parser


def filters_from_args(args):
    return {
        "exclude_damage": csv_list(args.exclude_damage),
        "include_damage": csv_list(args.include_damage),
        "body_styles": style_set(args.body_style),
        "exclude_body_styles": style_set(args.exclude_body_style),
        "seller_classes": set(args.seller_class),
        "exclude_seller_classes": set(args.exclude_seller_class),
        "min_photos": args.min_photos,
        "sold_only": args.sold_only,
        "markets": set(args.market),
        "max_odometer": args.max_odometer,
        "max_distance": args.max_distance,
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    if args.schema:
        print_schema()
        return 0

    paths = resolve_inputs(args)
    print("=" * 78)
    print("COPART JSON -> CSV")
    print("=" * 78)
    records = load_records(paths)
    if not records:
        raise SystemExit("no Copart records found in the given archive(s)")

    filters = filters_from_args(args)
    active = {key: value for key, value in filters.items() if value}
    print(f"\n  filters: {active or 'none (keeping every record)'}")

    groups = {}
    for record in records:
        key = observation_key(record)
        groups.setdefault(key, []).append(record)
    duplicates = sum(len(group) - 1 for group in groups.values())
    merged = [merge_observations(group) for group in groups.values()]

    kept, dropped = [], []
    for record in merged:
        accepted, reason = keep(record, filters)
        (kept if accepted else dropped).append((record, reason))
    print(f"  unique lots: {len(merged)}   (dropped {duplicates} duplicate row(s))")
    print(f"  kept {len(kept)}   filtered out {len(dropped)}")
    if dropped:
        reasons = {}
        for _, reason in dropped:
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
            print(f"      {count:>4}  {reason}")

    rows = [flatten(record) for record, _ in kept]
    market_counts = {}
    vpic_counts = {}
    distance_counts = {}
    for row in rows:
        market_counts[row["market"] or "(unknown)"] = market_counts.get(
            row["market"] or "(unknown)", 0) + 1
        vpic_counts[row["vpic_status"] or "(none)"] = vpic_counts.get(
            row["vpic_status"] or "(none)", 0) + 1
        distance_counts[row["distance_source"] or "(none)"] = distance_counts.get(
            row["distance_source"] or "(none)", 0) + 1
    print(f"\n  market:          {market_counts}")
    print(f"  vPIC:            {vpic_counts}")
    print(f"  distance_source: {distance_counts}")
    mismatch = [row["lot_number"] for row in rows if row["vpic_year_mismatch"]]
    if mismatch:
        print(f"  *** vPIC year mismatch: {len(mismatch)} lot(s): {mismatch[:10]} ***")

    out_dir = layer_dir(
        records[-1].get("_mode", "ended"), "csv-raw",
        records[-1].get("_platform", PLATFORM),
    )
    if args.out:
        output_path = Path(args.out)
        if not output_path.is_absolute():
            output_path = out_dir / output_path
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")
    else:
        suffix = "_copart_filtered" if active else "_copart"
        output_path = out_dir / f"{paths[-1].stem}{suffix}.csv"
        if active:
            print(
                "\n  note: filters are active; writing a non-canonical "
                "*_copart_filtered.csv. Persistent filtering belongs in csv-cut/."
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print("\n" + "=" * 78)
    print(f"Done. {len(rows)} row(s) x {len(COLUMNS)} column(s)")
    print(f"  CSV -> {output_path}")
    print(f"  next: python analytics/scripts/data_pull_01.py copart {paths[-1].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
