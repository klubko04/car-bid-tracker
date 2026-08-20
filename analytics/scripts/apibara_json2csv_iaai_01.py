"""
Stage 2 of the analytics pipeline — IAAI raw JSON -> analysis-ready CSV.

    pull_apibara_01.py  ->  raw .json  ->  THIS SCRIPT  ->  csv-raw
                                  |
                                  +----> data_pull_01.py -> csv-cut
                                        (+ tier, tier_source, sold_period)

This script owns everything derivable from the record alone, distance included.
`tier` and `sold_period` are NOT emitted here — tier takes an operator decision
per archive, so it belongs to data_pull_01.py.

Reads the archives written by pull_apibara_01.py, flattens each IAAI record into
one row, and applies whatever filtering you ask for. No network, no API calls —
run it as often as you like, with different filters, against the same archive.

KEY FIELDS ONLY — 69 columns, one per fact
------------------------------------------
Duplicates were removed after measuring them across a 70-lot pull, not by eye:
`vehicle_class` was identical to `body_style` on every row, `key_fob` to
`has_key`, `location_display` to `selling_branch`; `primary_damage_code`,
`acv_raw`, `est_repair_raw`, `body_style_name` and `runs_and_drives` were each
1:1 with a column that survived. Where a code and a description were 1:1 the
description won (codes like "AO"/"LR" mean nothing in a spreadsheet); where raw
text and a parsed number were 1:1 the number won ("$29,240 USD" does not sort);
where a boolean and a multi-valued field were 1:1 the multi-valued one won
(`run_condition` keeps IAAI's three run states, which a boolean flattens).

`secondary_damage_code` is the exception that proves the rule, and the reason
every pair was measured rather than assumed. It looks like the twin of
`primary_damage_code` and is not: the primary code is strictly 1:1 with its
description (14 values, both 70/70), while the secondary code is populated on
40/70 against the description's 30/70. Those extra 10 rows carry "UK" — IAAI
inspected the car and recorded secondary damage as UNKNOWN, a state a blank
description cannot be distinguished from "nothing recorded at all". Dropping it
would have silently merged two different facts.

Nothing is lost: every dropped field is still in the raw JSON archive, and
adding a column back is one line in SCHEMA plus a re-run — no API calls.

    python analytics/scripts/apibara_json2csv_iaai_01.py                # newest archive
    python analytics/scripts/apibara_json2csv_iaai_01.py FILE.json ...  # specific ones
    python analytics/scripts/apibara_json2csv_iaai_01.py --all          # every IAAI archive

    --exclude-damage water,flood,fire   drop lots whose damage matches (OFF by default)
    --include-damage front,rear         keep ONLY lots whose damage matches
    --body-style coupe                  keep only these (repeatable/comma-separated)
    --exclude-body-style coupe          drop these
    --seller-class insurance            keep only these seller classes
    --min-photos 8                      drop thin listings
    --sold-only                         drop lots with no realised sale price
    --market unitedstates               keep only these markets (US / Canada)
    --max-odometer 100000               drop high-mileage and unknown-odometer lots
    --max-distance 3000                 drop far and unknown-location lots
    --schema                            print the column mapping table and exit

IAAI ONLY. A Copart converter will be a separate script, because the two payload
shapes are not variations of one schema — they are different schemas. Copart
records carry `details: None`, which removes ActualCashValue, EstimatedRepairCost,
body style, storage coordinates and the entire attributes block in one go. Nearly
half the columns below simply do not exist on a Copart lot, so a shared converter
would be mostly null-handling.

SELLER CLASSIFICATION — READ THIS BEFORE TRUSTING THE COLUMN
------------------------------------------------------------
IAAI masks seller identity on a meaningful slice of lots. Observed on a 68-lot
insurance-filtered pull: 12 records came back with

    sale_information.SellerType = "******"    (literally asterisks)
    seller.name                 = "unknown"
    seller.type                 = "unknown"

...while `attributes.Origin` said "Insurance" and `attributes.ProviderType` said
"INS" on all 68. The masking hides WHICH carrier, not WHETHER it is a carrier.
Reading SellerType naively classifies those 12 as non-insurance, which is simply
wrong — every one of them came from a `seller_type=insurance` server-side query.

So `seller_class` cascades, most reliable source first. Which rule fired is not
a CSV column — it would be constant across a single-carrier pull — but every run
prints the full audit table, so the classification stays checkable.
`seller_name_masked` flags the anonymised ones so they can be excluded from
carrier-level analysis without being miscounted as dealers.

IMAGE / VIDEO COLUMNS
---------------------
IAAI thumb URLs look like:

    https://vis.iaai.com/resizer?imageKeys=46139013~SID~I1&width=400&height=300
                                           ^^^^^^^^        ^
                                           SalvageId       image key

`iaai_image_url_prefix` keeps everything up to and including the trailing "I",
and `iaai_image_keys` collects the numeric suffixes as a pipe-joined array
(1|2|...|115|116) — the keys are NOT contiguous, they jump from ~11 to ~115, so
they cannot be regenerated from a count and must be stored. Rebuild any URL as:

    f"{iaai_image_url_prefix}{key}&width=845&height=633"      # large
    f"{iaai_image_url_prefix}{key}&width=400&height=300"      # thumb

Note the image key is `attributes.SalvageId`, which is NOT the lot number
(lot 45640490 -> SalvageId 46139013). Do not build image URLs from lot_number.

vr360 is deliberately absent from the CSV — it carries no analytic signal and
one fixed URL shape per lot. It remains in the raw JSON if ever needed.
"""
import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# analytics/data/{sold|open}/<layer>/{iaai|copart}/
#   json-raw/   <- pull_apibara_01.py: untouched API responses (this script's input)
#   csv-raw/    <- THIS SCRIPT: flattened, unfiltered
#   csv-cut/    <- data_pull_01.py: filtered + tier/sold_period
DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
MODE_BUCKET = {"ended": "sold", "open": "open", "live": "open"}
PLATFORM = "iaai"          # this converter's platform, and its folder name


def layer_dir(mode, layer, platform=PLATFORM):
    """analytics/data/<bucket>/<layer>/<platform>/ for an archive's pull mode."""
    return DATA_DIR / MODE_BUCKET.get(mode, "sold") / layer / platform


# Where a bare filename is looked up, in order: this platform's json-raw in each
# bucket first, then the parent layers and the pre-reorganisation trees, so
# older paths and commands still resolve.
SEARCH_DIRS = [DATA_DIR / b / "json-raw" / PLATFORM for b in BUCKETS] + [
    # iaai_web_adapt_01.py output: iaai.com pulls reshaped into this record
    # shape. Derived, so it lives beside json-raw rather than in it.
    DATA_DIR / b / "json-adapted" / PLATFORM for b in BUCKETS] + [
    DATA_DIR / b / "json-raw" for b in BUCKETS] + [
    DATA_DIR,
    ROOT / "analytics" / "sold-data" / "json-raw",
    ROOT / "analytics" / "sold-data",
    ROOT / "analytics" / "sold-data-archive",
]

sys.path.insert(0, str(ROOT))
from app.branch_geo import (BUCKET_STEP, ROAD_FACTOR,  # noqa: E402
                            haversine_mi)

ARRAY_SEP = "|"          # pipe, so the CSV stays readable without nested quoting

# Insurance-carrier name fragments, used only as the LAST resort in the seller
# cascade (attributes.Origin and ProviderType are checked first).
_INSURER_NEEDLES = [
    "insurance", "insur", "casualty", "assurance", "indemnity", "underwrit",
    "mutual", "geico", "usaa", "state farm", "progressive", "allstate",
    "farmers", "nationwide", "esurance", "safeco", "travelers", "hartford",
    "amica", "erie", "aaa ", "auto club", "root ", "lemonade", "mapfre",
    "mercury", "arbella", "bluefire",
]

# A value IAAI redacted rather than omitted. Treated as "present but hidden",
# never as a real value.
_MASKED = re.compile(r"^\*+$")


# --------------------------------------------------------------------------
# accessors — every one tolerates a missing/None parent block
# --------------------------------------------------------------------------
def g(d, *path, default=None):
    """Nested get: g(rec, 'details', 'attributes', 'Origin')."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def attrs(v):
    return g(v, "details", "attributes", default={}) or {}


def sale_info(v):
    return g(v, "details", "sale_information", default={}) or {}


def veh_desc(v):
    return g(v, "details", "vehicle_description", default={}) or {}


def is_masked(x):
    return bool(_MASKED.match(str(x or "").strip()))


def clean(x):
    """Normalise API sentinels to None: '', 'unknown', 'None', '******'."""
    s = str(x).strip() if x is not None else ""
    if not s or s.lower() in ("unknown", "none", "n/a") or is_masked(s):
        return None
    return x


def money_num(v):
    """'$29,240 USD' / 29240 / '' -> 29240.0 / None."""
    if isinstance(v, (int, float)):
        return float(v) or None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


_TENANT = {"US": ("UnitedStates", "USD"), "CA": ("Canada", "CAD")}


def _tenant(v):
    """Market/currency from the item id suffix — 'Imp_3069335~CA' -> CA.

    Needed because a search-only pull has no `attributes` at all, and a
    Canadian lot's buy-now price is still scraped from the row. Without this
    the currency guard would pass CAD through on exactly the cheap cadence that
    runs most often.
    """
    ident = str(clean(attrs(v).get("Id")) or clean(v.get("_web_item_id")) or "")
    return _TENANT.get(ident.rsplit("~", 1)[-1].upper()) if "~" in ident else None



# --------------------------------------------------------------------------
# derived: IAA vehicle score band
# --------------------------------------------------------------------------
# IAA's own automated computer-vision assessment of visible damage severity,
# from the check-in photos. Two things about it are easy to get backwards:
# HIGHER IS BETTER (50 is the least damaged, 0 the worst), and it is NOT a
# percentage of anything. Band names and boundaries are IAA's, from their score
# flyer — kept verbatim rather than paraphrased so a row can be checked against
# the official document.
_SCORE_BANDS = (
    (0, 9, "non-repairable"),
    (10, 19, "severe damage"),
    (20, 29, "major damage"),
    (30, 39, "moderate damage"),
    (40, 49, "minor damage"),
    (50, 50, "little damage"),
)


def vehicle_score(v):
    """IAA's 0-50 score as a number, or None when not assessed."""
    raw = clean(attrs(v).get("VehicleGrade"))
    if raw is None:
        return None
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def vehicle_score_band(v):
    """-> IAA's band name, or '' when the lot carries no score.

    Blank stays blank rather than collapsing into the worst band: "not assessed"
    and "assessed as non-repairable" are opposite facts, and 209 of 2,679 rows
    have no score at all.
    """
    n = vehicle_score(v)
    if n is None:
        return ""
    for lo, hi, name in _SCORE_BANDS:
        if lo <= n <= hi:
            return name
    return ""


# --------------------------------------------------------------------------
# derived: damage grouping
# --------------------------------------------------------------------------
# Operator-defined buckets, collapsing IAAI's ~50 damage strings into the three
# that change a rebuild decision. Matching is case- and spacing-insensitive
# because the two fields do not agree with each other: primary says "Front end"
# and "Left side", secondary says "Front End" and "Left Side", and secondary
# carries values primary never does ("Left & Right Side").
#
# Note two placements that look surprising and are deliberate: `Roof` groups
# with FRONT, and `Theft` / `Normal wear & tear` with REAR-SIDE.
_DAMAGE_GROUPS = {
    "REAR-SIDE": (
        "left & right side", "left rear", "left side", "rear",
        "right rear", "right side", "normal wear & tear", "theft",
        "hail",
    ),
    "FRONT": (
        "front & rear", "front end", "front", "left front", "right front",
        "roof",
    ),
}
_DAMAGE_GROUP_BY_VALUE = {val: grp
                          for grp, vals in _DAMAGE_GROUPS.items()
                          for val in vals}


def _norm_damage(s):
    """Normalise a damage string for lookup: LOWERCASE, then tidy spacing.

    Both sides of the comparison go through this — the table above is written in
    lowercase and the incoming value is lowercased here — so casing can never
    decide a grouping. That is not cosmetic: `primary_damage` writes "Front end"
    and "Left side" while `secondary_damage` writes "Front End" and "Left Side",
    so a case-sensitive lookup would group one field correctly and silently drop
    the other into OTHER.
    """
    t = re.sub(r"\s+", " ", str(s or "").strip().lower())
    return re.sub(r"\s*&\s*", " & ", t)


def damage_group(s):
    """-> REAR-SIDE | FRONT | OTHER, or '' when no damage is recorded.

    Empty stays empty rather than becoming OTHER: "no damage recorded" and
    "damage recorded that is not front or rear/side" are different facts, and
    secondary_damage is blank on 1,218 rows — folding those into OTHER would
    make the column read as if most lots had exotic damage.
    """
    t = _norm_damage(s)
    if not t:
        return ""
    return _DAMAGE_GROUP_BY_VALUE.get(t, "OTHER")


def market(v):
    """'UnitedStates' / 'Canada'. IAA runs both under one site and one search."""
    return clean(attrs(v).get("Market")) or ((_tenant(v) or (None, None))[0])


def currency(v):
    """'USD' / 'CAD'. Read this before trusting any *_usd column."""
    return clean(attrs(v).get("Currency")) or ((_tenant(v) or (None, None))[1])


def usd(v, value):
    """A money figure, but ONLY when the lot is actually priced in USD.

    IAA Canada lots come back through the same search with amounts in CAD:
    lot 12666581 (Toronto North) reports ActualCashValue '$16,721 CAD'. Left
    alone, money_num() strips the symbol and 16,721 CAD lands in `acv_usd`,
    where the max-bid maths treats it as dollars — a ~35% overstatement that
    nothing downstream could detect.

    Rather than convert at a rate this pipeline has no business inventing, the
    *_usd columns stay strictly USD and non-USD lots leave them empty. The
    native amounts remain in the archive, and `currency` says what to expect.
    """
    cur = currency(v)
    return money_num(value) if cur in (None, "USD") else None


def as_bool(x):
    if isinstance(x, bool):
        return x
    s = str(x or "").strip().lower()
    return True if s == "true" else False if s == "false" else None


def ratio(num, den):
    return round(num / den, 4) if num and den else None


# --------------------------------------------------------------------------
# derived: seller
# --------------------------------------------------------------------------
def seller_class(v):
    """-> (class, source). class in {insurance, dealer, other, unknown}.

    Ordered by how much the field can be trusted. attributes.Origin /
    ProviderType survive IAAI's seller masking; SellerType and seller.name do
    not (see module docstring).
    """
    origin = str(g(v, "details", "attributes", "Origin", default="") or "").lower()
    ptype = str(g(v, "details", "attributes", "ProviderType", default="") or "").lower()
    if "ins" in ptype or "insurance" in origin:
        return "insurance", "attributes.Origin/ProviderType"
    if ptype == "dlr" or origin in ("dealer", "dealership"):
        return "dealer", "attributes.Origin/ProviderType"
    # IAAI's own non-insurance origins. Worth reading before falling through to
    # name inference, because the name is the one field its masking removes —
    # an iaai.com row has Origin but never a seller name, and without this a
    # Turo/fleet lot lands in `unknown` there while an Apibara row for the SAME
    # lot lands in `other`.
    if origin in ("remarketing vehicles", "repossession", "fleet lease",
                  "charity", "rental"):
        return "other", "attributes.Origin"

    stype = str(g(v, "seller", "type", default="") or "").strip().lower()
    if stype == "insurance":
        return "insurance", "seller.type"
    if stype in ("non_insurance", "non-insurance", "dealer"):
        return "dealer", "seller.type"

    itype = str(sale_info(v).get("SellerType") or "").strip()
    if itype and not is_masked(itype):
        return (("insurance", "sale_information.SellerType")
                if "insur" in itype.lower()
                else ("dealer", "sale_information.SellerType"))

    name = str(clean(g(v, "seller", "name")) or clean(sale_info(v).get("Seller"))
               or "").lower()
    if not name:
        return "unknown", "none"
    if any(n in name for n in _INSURER_NEEDLES):
        return "insurance", "seller.name"
    return "other", "seller.name~inferred"


# --------------------------------------------------------------------------
# derived: distance from Federal Way, WA 98003
# --------------------------------------------------------------------------
def branch_coords(v):
    """(lat, lng) from IAAI's own StorageLocation fields, or (None, None)."""
    a = attrs(v)
    try:
        return (float(a["StorageLocationLatitude"]),
                float(a["StorageLocationLongitude"]))
    except (KeyError, TypeError, ValueError):
        return None, None


def distance_mi(v):
    """Road-ish miles to the transport destination, or None.

    No city-table fallback, unlike app/branch_geo.py. That module resolves
    `location.display` through a hand-curated lookup because `facility.lat` was
    populated on only 3 of 75 records when it was written — still true of
    `facility.lat`, but IAAI's own StorageLocationLatitude/Longitude is a
    different field and is populated on 154/154 records across four independent
    pulls, all inside the continental US. So this uses the real coordinates and
    nothing else; a lot missing them yields an empty distance and is counted in
    the run summary rather than being quietly approximated from a state
    centroid.
    """
    lat, lng = branch_coords(v)
    if lat is None:
        return None
    return round(haversine_mi(lat, lng) * ROAD_FACTOR)


def distance_bucket(v):
    """'250mi' / '500mi' / ... rounded UP to the next BUCKET_STEP, or None."""
    miles = distance_mi(v)
    if miles is None:
        return None
    return f"{max(BUCKET_STEP, int(math.ceil(miles / BUCKET_STEP) * BUCKET_STEP))}mi"


# --------------------------------------------------------------------------
# derived: media
# --------------------------------------------------------------------------
_IMG_RE = re.compile(r"^(?P<prefix>.*imageKeys=[^&]*?~SID~I)(?P<key>\d+)")


def image_fields(v):
    """-> (prefix, [keys], count). See module docstring for the URL anatomy."""
    thumbs = g(v, "media", "thumbs", default=[]) or []
    prefix, keys = None, []
    for url in thumbs:
        m = _IMG_RE.match(str(url))
        if not m:
            continue
        prefix = prefix or m.group("prefix")
        keys.append(int(m.group("key")))
    return prefix, keys, len(thumbs)


def video_fields(v):
    """-> (count, first_url). vr360 items are ignored on purpose."""
    items = g(v, "media", "items", default=[]) or []
    urls = [it.get("url") for it in items
            if isinstance(it, dict) and it.get("type") == "video" and it.get("url")]
    return len(urls), (urls[0] if urls else None)


# --------------------------------------------------------------------------
# derived: listing state
# --------------------------------------------------------------------------
_DAY2 = {"mon": "Mo", "tue": "Tu", "wed": "We", "thu": "Th",
         "fri": "Fr", "sat": "Sa", "sun": "Su"}


def local_tag(v):
    """-> 'MMDD-Dy' from IAAI's local sale date, or '' when unscheduled."""
    loc = g(v, "auction", "local") or {}
    day, mon, date = loc.get("day"), loc.get("month"), loc.get("date")
    if not (day and mon and date):
        return ""
    d2 = _DAY2.get(str(day)[:3].lower())
    return f"{int(mon):02d}{int(date):02d}-{d2}" if d2 else ""


def listing_state(v):
    """IAAI's own listing state, in IAAI's vocabulary, from either source.

    A web row carries the label IAAI printed on the search page (`_web_state`),
    which is authoritative. An Apibara row has no such field — `auction.state`
    is Apibara's own open/ended vocabulary, not IAAI's — so it is derived from
    the same underlying flags the site uses:

        InventoryStatus  RS = assigned to a sale, WC = in inventory, no sale

    `PreBidIndicator` is deliberately NOT consulted: it is true on all three
    states and discriminates nothing.

    Worth knowing: Apibara returns WC lots essentially never (1 of 1,349 in the
    corpus, and 0 of 29 open lots), while they were 56 of 65 on a web pull of
    the same search. `Auction Not Assigned` is the coverage gap.
    """
    native = clean(v.get("_web_state"))
    if native:
        return native
    a = attrs(v)
    has_date = bool(g(v, "auction", "auction_at") or v.get("ad"))
    if str(a.get("InventoryStatus") or "").upper() == "WC" or not has_date:
        return "Auction Not Assigned"
    if g(v, "auction", "last_sold_day"):
        return "Ended"
    # A timed (online-only) sale outranks the buy-now label, matching how the
    # site labels it — observed on lot 45662018 flipping Prebid -> TimedAuction.
    if as_bool(a.get("TimedAuctionIndicator")) or g(v, "auction", "is_timed"):
        return "TimedAuction"
    if money_num(g(v, "pricing", "buy_now_usd")):
        return "Prebid/BuyNow"
    return "Prebid"


# --------------------------------------------------------------------------
# THE SCHEMA — single source of truth for the CSV
#   (column, source, kind)   kind: "raw" = copied as-is, "calc" = derived here
# Adding a column means adding one line here and nothing else.
# --------------------------------------------------------------------------
SCHEMA = [
    # --- identity -------------------------------------------------------
    ("lot_number",          lambda v: v.get("lot_number"),                    "raw"),
    ("vin",                 lambda v: v.get("vin"),                           "raw"),
    ("year",                lambda v: v.get("year"),                          "raw"),
    ("make",                lambda v: v.get("make"),                          "raw"),
    ("model",               lambda v: v.get("model"),                         "raw"),
    ("series",              lambda v: clean(attrs(v).get("Series")),          "raw"),
    # The URL needs IAA's ITEM id ('46203349~US'), not the stock number.
    # Built from lot_number it 200s into a DetailsNotFoundView shell — verified
    # dead for US and Canadian lots alike. attributes.Id carries the right value
    # on both sources; _web_item_id is the fallback for search-only web rows.
    ("lot_url",             lambda v: (lambda i: f"https://www.iaai.com/VehicleDetail/{i}"
                                       if i else None)(
                                clean(attrs(v).get("Id")) or clean(v.get("_web_item_id"))), "calc"),
    ("market",              market,                                           "calc"),
    # The iaai.com search that returned this lot, e.g. "2019 Audi A5". Blank on
    # Apibara rows, which have no keyword concept. Worth a column because IAAI's
    # keyword search matches loosely — a "2018 Audi RS 5" search returns RS 3
    # lots too — so the per-lot `model` field cannot recover what was searched.
    ("search_keyword",      lambda v: clean(v.get("_web_keyword")),           "raw"),
    ("currency",            currency,                                         "calc"),

    # --- body / specs ---------------------------------------------------
    ("body_style",          lambda v: g(v, "vehicle_specs", "body_style"),    "raw"),
    ("engine_raw",          lambda v: g(v, "vehicle_specs", "engine", "raw"), "raw"),
    ("engine_size_l",       lambda v: g(v, "vehicle_specs", "engine", "size_l"), "raw"),
    ("engine_hp",           lambda v: g(v, "vehicle_specs", "engine", "hp"),  "raw"),
    ("cylinders",           lambda v: attrs(v).get("Cylinders"),              "raw"),
    ("transmission",        lambda v: g(v, "vehicle_specs", "transmission"),  "raw"),
    ("drive_type",          lambda v: g(v, "vehicle_specs", "drive_type"),    "raw"),
    ("fuel_type",           lambda v: g(v, "vehicle_specs", "fuel_type"),     "raw"),
    ("exterior_color",      lambda v: g(v, "vehicle_specs", "exterior_color"), "raw"),
    ("options",             lambda v: clean(veh_desc(v).get("Options")),      "raw"),
    ("country_of_origin",   lambda v: clean(attrs(v).get("CountryOfOrigin")), "raw"),
    ("primary_damage",      lambda v: g(v, "condition", "primary_damage"),    "raw"),
    ("secondary_damage",    lambda v: g(v, "condition", "secondary_damage"),  "raw"),
    ("primary_damage_group", lambda v: damage_group(g(v, "condition", "primary_damage")), "calc"),
    ("secondary_damage_group", lambda v: damage_group(g(v, "condition", "secondary_damage")), "calc"),
    # Kept even though primary_damage_code was dropped as a duplicate: this one
    # is populated on 40/70 where the description is only 30/70. The extra 10
    # are "UK" (= SecondaryDamageDesc "UNKNOWN") — IAAI inspected and recorded
    # the secondary damage as unknown, which a blank description cannot tell
    # apart from "no secondary damage recorded at all".
    ("secondary_damage_code", lambda v: clean(attrs(v).get("SecondaryDamageCode")), "raw"),
    ("loss_type",           lambda v: clean(attrs(v).get("LossTypeDesc")),    "raw"),
    ("run_condition",       lambda v: g(v, "condition", "run_condition", "value"), "raw"),
    ("has_key",             lambda v: g(v, "condition", "has_key"),           "raw"),
    ("airbags",             lambda v: g(v, "vehicle_specs", "airbags"),       "raw"),
    # IAA's own photo-based damage score. Renamed from `vehicle_grade` because
    # "grade" reads like a letter grade or a percentage and this is neither.
    ("iaa_vehicle_score",   vehicle_score,                                    "raw"),
    ("iaa_vehicle_score_band", vehicle_score_band,                            "calc"),
    ("odometer_mi",         lambda v: g(v, "odometer", "mi"),                 "raw"),
    ("sale_document",       lambda v: g(v, "sale_document", "name"),          "raw"),
    ("sale_document_group", lambda v: g(v, "sale_document", "sale_document_group"), "raw"),
    ("title_state",         lambda v: clean(attrs(v).get("TitleState")),      "raw"),
    # Every *_usd column is guarded by usd(): a CAD lot leaves them empty rather
    # than silently reporting Canadian dollars as US ones. The ratios below are
    # currency-neutral (same-currency numerator and denominator) and stay.
    ("last_sold_price_usd", lambda v: usd(v, g(v, "pricing", "last_sold_price_usd")), "calc"),
    ("current_bid_usd",     lambda v: usd(v, g(v, "pricing", "current_bid_usd")), "calc"),
    ("buy_now_usd",         lambda v: usd(v, g(v, "pricing", "buy_now_usd")), "calc"),
    ("acv_usd",             lambda v: usd(v, sale_info(v).get("ActualCashValue")), "calc"),
    ("est_repair_usd",      lambda v: usd(v, attrs(v).get("EstRepairCost")
                                          or sale_info(v).get("EstimatedRepairCost")), "calc"),
    ("repair_to_acv",       lambda v: ratio(money_num(attrs(v).get("EstRepairCost")),
                                            money_num(sale_info(v).get("ActualCashValue"))), "calc"),
    ("sold_to_acv",         lambda v: ratio(money_num(g(v, "pricing", "last_sold_price_usd")),
                                            money_num(sale_info(v).get("ActualCashValue"))), "calc"),

    # --- auction --------------------------------------------------------
    ("auction_at",          lambda v: g(v, "auction", "auction_at") or v.get("ad"), "raw"),
    ("last_sold_day",       lambda v: g(v, "auction", "last_sold_day"),       "raw"),
    ("last_sold_status",    lambda v: g(v, "auction", "last_sold_status"),    "raw"),
    ("listing_state",       listing_state,                                    "calc"),
    # Hard close of a timed/online-only sale, which can fall DAYS before
    # auction_at. Empty on ordinary live-lane lots.
    # IAAI's branch-LOCAL sale date as "MMDD-Dy" (0820-Th). The site prints
    # local time; auction_at is UTC. Blank on Apibara rows and on unscheduled lots.
    ("auction_local_tag",   lambda v: local_tag(v),                           "calc"),
    ("timed_close_at",      lambda v: g(v, "auction", "timed_end_at")
                            or clean(attrs(v).get("TimedAuctionCloseDateTime")), "raw"),
    # Buy Now expiry, and IAAI's own "this went via Buy Now" flag. The flag is
    # set while the lot is still listed, so a buy-now sale is catchable on the
    # pull before the lot disappears.
    ("buy_now_close_at",    lambda v: g(v, "auction", "buy_now_close_at")
                            or clean(attrs(v).get("BuyNowCloseDateTime")),     "raw"),
    ("buy_now_sold",        lambda v: bool(g(v, "auction", "sold_buy_now")),   "raw"),
    # Which buyer licence classes may bid. Populated only once a sale is
    # assigned, so it is empty on every Auction Not Assigned lot by design.
    ("who_can_buy",         lambda v: clean(attrs(v).get("WhoCanBuy")),       "raw"),
    ("seller_name",         lambda v: clean(g(v, "seller", "name"))
                            or clean(sale_info(v).get("Seller")),             "raw"),
    ("seller_name_masked",  lambda v: is_masked(sale_info(v).get("SellerType"))
                            or not clean(g(v, "seller", "name")),             "calc"),
    ("seller_class",        lambda v: seller_class(v)[0],                     "calc"),
    # The raw IAAI code behind seller_class. Carried so the codes whose meaning
    # is not yet established (COR/SDS/RCC, and DLR on a thin sample) can be
    # studied from the CSV instead of re-opening the JSON.
    ("seller_provider_type", lambda v: clean(attrs(v).get("ProviderType")),   "raw"),
    ("selling_branch",      lambda v: sale_info(v).get("SellingBranch"),      "raw"),
    ("branch_state",        lambda v: clean(attrs(v).get("BranchState")),     "raw"),
    ("branch_zip",          lambda v: clean(attrs(v).get("Zip")),             "raw"),
    ("branch_lat",          lambda v: attrs(v).get("StorageLocationLatitude"), "raw"),
    ("branch_lng",          lambda v: attrs(v).get("StorageLocationLongitude"), "raw"),
    ("distance_mi",         distance_mi,                                      "calc"),
    ("distance_bucket",     distance_bucket,                                  "calc"),
    ("image_count",         lambda v: image_fields(v)[2],                     "calc"),
    ("iaai_image_url_prefix", lambda v: image_fields(v)[0],                   "calc"),
    ("iaai_image_keys",     lambda v: ARRAY_SEP.join(str(k) for k in image_fields(v)[1])
                            or None,                                          "calc"),
    ("video_count",         lambda v: video_fields(v)[0],                     "calc"),
    ("iaai_video_url",      lambda v: video_fields(v)[1],                     "calc"),

    # --- provenance -----------------------------------------------------
    ("source_file",         lambda v: v.get("_source_file"),                  "calc"),
    ("pulled_at",           lambda v: v.get("_pulled_at"),                    "calc"),
]

COLUMNS = [c for c, _, _ in SCHEMA]


def flatten(v):
    return {col: fn(v) for col, fn, _ in SCHEMA}


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------
def damage_text(v):
    c = v.get("condition") or {}
    return f"{c.get('primary_damage') or ''} {c.get('secondary_damage') or ''}".lower()


def norm_style(s):
    return re.sub(r"[\s_-]+", "/", str(s or "").strip().lower())


def keep(v, f):
    """-> (bool, reason). All filters are opt-in; default keeps everything."""
    if f["exclude_damage"]:
        hit = next((n for n in f["exclude_damage"] if n in damage_text(v)), None)
        if hit:
            return False, f"damage~{hit}"
    if f["include_damage"]:
        if not any(n in damage_text(v) for n in f["include_damage"]):
            return False, f"damage!~{'/'.join(f['include_damage'])}"

    if f["body_styles"] or f["exclude_body_styles"]:
        bs = norm_style(g(v, "vehicle_specs", "body_style"))
        if f["body_styles"] and bs not in f["body_styles"]:
            return False, f"body_style={bs or 'null'}"
        if bs in f["exclude_body_styles"]:
            return False, f"body_style={bs} excluded"

    if f["seller_classes"] and seller_class(v)[0] not in f["seller_classes"]:
        return False, f"seller_class={seller_class(v)[0]}"
    # data_pull_01 shares one filter dict across both platforms, so without
    # this an --exclude-seller-class on an IAAI cut would silently do nothing.
    if seller_class(v)[0] in f.get("exclude_seller_classes", ()):
        return False, f"seller_class={seller_class(v)[0]} excluded"

    # Market, filtered HERE and not only at pull time. `--market us` on
    # pull_iaai_web_01 cannot clean archives written before it existed, and
    # --history widens a cut across every archive sharing a keyword — so two
    # Canadian lots reappeared in a us-only analysis via a pre-flag snapshot.
    # The CSV layer must be able to state its own scope.
    if f["markets"]:
        mk = str(market(v) or "").strip().lower()
        if mk not in f["markets"]:
            return False, f"market={mk or 'unknown'}"

    if f["min_photos"] and (g(v, "media", "thumbs_count") or 0) < f["min_photos"]:
        return False, f"photos={g(v, 'media', 'thumbs_count') or 0}"

    if f["sold_only"] and not money_num(g(v, "pricing", "last_sold_price_usd")):
        return False, "no sold price"

    # Odometer and distance both drop a lot when the value is MISSING as well as
    # when it is over the limit. An unknown odometer is not evidence of low
    # mileage, and a lot with no branch coordinates cannot be shown to be within
    # range — silently keeping either would quietly widen the filter.
    if f["max_odometer"]:
        mi = g(v, "odometer", "mi")
        if mi is None:
            return False, "odometer unknown"
        if mi > f["max_odometer"]:
            return False, f"odometer={int(mi)}"

    if f["max_distance"]:
        d = distance_mi(v)
        if d is None:
            return False, "distance unknown"
        if d > f["max_distance"]:
            return False, f"distance={d}mi"

    return True, ""


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------
def load_records(paths):
    """-> [record], each tagged with its provenance."""
    out = []
    for p in paths:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        plat = doc.get("platform")
        if plat and plat != "iaai":
            print(f"  !! skipping {Path(p).name}: platform={plat}, this "
                  f"converter is IAAI-only")
            continue
        # An un-adapted web archive would otherwise load as 0 records with no
        # explanation — it stores lots under records[], not pages[].
        if doc.get("source") == "iaai-web":
            print(f"  !! skipping {Path(p).name}: raw iaai.com archive.\n"
                  f"     Run: python analytics/scripts/iaai_web_adapt_01.py "
                  f"{Path(p).name}")
            continue
        pulled = doc.get("generated_at")
        mode = doc.get("mode", "ended")
        n = 0
        for page in doc.get("pages", []):
            if page.get("status") != 200:
                continue
            for rec in (page.get("raw", {}).get("data") or []):
                rec["_source_file"] = Path(p).name
                rec["_pulled_at"] = pulled
                # The archive states its own lot_sub_status, so output routing
                # never has to be inferred from a filename.
                rec["_mode"] = mode
                rec["_platform"] = plat or PLATFORM
                out.append(rec)
                n += 1
        print(f"  loaded {n:>4} record(s) from {Path(p).name}  [{mode}]")
    return out


def resolve_one(f):
    """Accept an absolute path, a path relative to the shell's cwd, or a bare
    filename living in any SEARCH_DIRS entry (what pull_apibara_01 prints)."""
    p = Path(f)
    if p.is_absolute() or p.exists():
        return p
    for d in SEARCH_DIRS:
        if (d / f).exists():
            return d / f
    looked = "\n    ".join(str(d / f) for d in SEARCH_DIRS)
    raise SystemExit(f"input not found: {f}\n  looked in ./{f}\n    {looked}")


def resolve_inputs(args):
    """Explicit files, else the newest archive across both buckets.

    Auto-discovery spans sold/ and open/ so `--all` means every archive, not
    every archive of whichever bucket happened to be hardcoded.
    """
    if args.files:
        return [resolve_one(f) for f in args.files]
    # json-adapted/ counts as input; raw web archives do NOT. A pull_iaai_web
    # archive keys its lots under queries[]/records[] rather than pages[], so it
    # contributes zero rows AND, being the newest file, would name the combined
    # output after itself. Run iaai_web_adapt_01.py on it first.
    pool = sorted((p for b in BUCKETS
                   for layer in ("json-raw", "json-adapted")
                   for p in (DATA_DIR / b / layer / PLATFORM).glob("*.json")
                   if not p.name.startswith("iaaiweb_")),
                  key=lambda p: p.stat().st_mtime)
    if not pool:
        raise SystemExit(
            f"no {PLATFORM.upper()} archives found under "
            f"{DATA_DIR}/{{{'|'.join(BUCKETS)}}}/json-raw/{PLATFORM} — "
            f"run pull_apibara_01.py first")
    return pool if args.all else [pool[-1]]


def print_schema():
    print(f"{'csv column':<24} {'kind':<5} source (JSON path)")
    print("-" * 104)
    for col, _, kind in SCHEMA:
        print(f"{col:<24} {kind:<5} {SOURCE_HINTS.get(col, '')}")
    n_calc = sum(1 for _, _, k in SCHEMA if k == "calc")
    print(f"\n{len(COLUMNS)} columns — {len(COLUMNS) - n_calc} raw (copied "
          f"as-is), {n_calc} calc (derived here).")
    print("Full mapping with notes: analytics/schema/iaai_csv_schema.md")


# JSON path per column, for --schema and the docs. Kept in one place so the
# markdown table and the CLI never drift apart.
SOURCE_HINTS = {
    "lot_number": "lot_number",
    "vin": "vin", "year": "year", "make": "make", "model": "model",
    "series": "details.attributes.Series",
    "lot_url": "built from details.attributes.Id (the ITEM id, not lot_number)",
    "market": "details.attributes.Market — UnitedStates | Canada",
    "search_keyword": "_web_keyword — the iaai.com search that returned the lot",
    "currency": "details.attributes.Currency — USD | CAD; *_usd columns are USD-only",
    "body_style": "vehicle_specs.body_style",
    "engine_raw": "vehicle_specs.engine.raw",
    "options": "details.vehicle_description.Options",
    "country_of_origin": "details.attributes.CountryOfOrigin",
    "secondary_damage_code": "details.attributes.SecondaryDamageCode ('UK' = inspected, unknown)",
    "engine_size_l": "vehicle_specs.engine.size_l",
    "engine_hp": "vehicle_specs.engine.hp",
    "cylinders": "details.attributes.Cylinders",
    "transmission": "vehicle_specs.transmission",
    "drive_type": "vehicle_specs.drive_type",
    "fuel_type": "vehicle_specs.fuel_type",
    "exterior_color": "vehicle_specs.exterior_color",
    "primary_damage": "condition.primary_damage",
    "secondary_damage": "condition.secondary_damage",
    "primary_damage_group": "REAR-SIDE | FRONT | OTHER (see _DAMAGE_GROUPS)",
    "secondary_damage_group": "same grouping applied to secondary_damage",
    "loss_type": "details.attributes.LossTypeDesc",
    "run_condition": "condition.run_condition.value",
    "has_key": "condition.has_key",
    "airbags": "vehicle_specs.airbags",
    "iaa_vehicle_score": "details.attributes.VehicleGrade — IAA photo score, 0-50, HIGHER IS BETTER",
    "iaa_vehicle_score_band": "IAA band name for the score (non-repairable .. little damage)",
    "odometer_mi": "odometer.mi",
    "sale_document": "sale_document.name",
    "sale_document_group": "sale_document.sale_document_group",
    "title_state": "details.attributes.TitleState",
    "last_sold_price_usd": "pricing.last_sold_price_usd -> number",
    "current_bid_usd": "pricing.current_bid_usd -> number",
    "buy_now_usd": "pricing.buy_now_usd -> number",
    "acv_usd": "details.sale_information.ActualCashValue -> number",
    "est_repair_usd": "details.attributes.EstRepairCost (fallback sale_information) -> number",
    "repair_to_acv": "est_repair_usd / acv_usd",
    "sold_to_acv": "last_sold_price_usd / acv_usd",
    "auction_at": "auction.auction_at (fallback ad)",
    "last_sold_day": "auction.last_sold_day",
    "last_sold_status": "auction.last_sold_status",
    "listing_state": "_web_state, else derived from InventoryStatus RS/WC + dates",
    "auction_local_tag": "auction.local -> MMDD-Dy in IAAI's branch-local time",
    "timed_close_at": "auction.timed_end_at (fallback attributes.TimedAuctionCloseDateTime)",
    "buy_now_close_at": "auction.buy_now_close_at (fallback attributes.BuyNowCloseDateTime)",
    "buy_now_sold": "auction.sold_buy_now — set while still listed",
    "who_can_buy": "details.attributes.WhoCanBuy (licence classes; assigned lots only)",
    "seller_name": "seller.name (fallback sale_information.Seller)",
    "seller_name_masked": "true when SellerType is masked or name is 'unknown'",
    "seller_class": "cascade: Origin/ProviderType > seller.type > SellerType > name",
    "seller_provider_type": "details.attributes.ProviderType (raw IAAI code)",
    "selling_branch": "details.sale_information.SellingBranch",
    "branch_state": "details.attributes.BranchState",
    "branch_zip": "details.attributes.Zip",
    "branch_lat": "details.attributes.StorageLocationLatitude",
    "branch_lng": "details.attributes.StorageLocationLongitude",
    "distance_mi": "haversine(branch_lat/lng -> 98003) x 1.2",
    "distance_bucket": "distance_mi rounded UP to the next 250mi",
    "image_count": "len(media.thumbs)",
    "iaai_image_url_prefix": "media.thumbs[0] up to '~SID~I'",
    "iaai_image_keys": "numeric suffixes of media.thumbs, pipe-joined",
    "video_count": "count of media.items[type=video]",
    "iaai_video_url": "first media.items[type=video].url",
    "source_file": "archive filename this row came from",
    "pulled_at": "archive generated_at",
}


# --------------------------------------------------------------------------
def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="apibara_json2csv_iaai_01.py",
        description="Flatten IAAI Apibara archives into an analysis CSV. "
                    "Offline — no API calls.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*",
                    help="archive .json files (default: newest IAAI archive)")
    ap.add_argument("--all", action="store_true",
                    help="use every IAAI archive in data/{sold,open}/json-raw/")
    ap.add_argument("--exclude-damage", nargs="+", metavar="a,b,c",
                    help="drop lots whose primary/secondary damage contains any "
                         "of these (case-insensitive substrings)")
    ap.add_argument("--include-damage", nargs="+", metavar="a,b,c",
                    help="keep ONLY lots whose damage contains one of these")
    ap.add_argument("--body-style", action="append", nargs="+", default=[], metavar="STYLE",
                    help="keep only these body styles (repeatable)")
    ap.add_argument("--exclude-body-style", action="append", nargs="+",
                    default=[], metavar="STYLE",
                    help="drop these body styles (repeatable)")
    ap.add_argument("--seller-class", action="append", default=[],
                    choices=["insurance", "dealer", "other", "unknown"],
                    help="keep only these seller classes (repeatable)")
    ap.add_argument("--exclude-seller-class", action="append", default=[],
                    choices=["insurance", "dealer", "other", "unknown"],
                    help="drop these seller classes (exclusion beats inclusion)")
    ap.add_argument("--min-photos", type=int, default=0,
                    help="drop lots with fewer thumbnails than this")
    ap.add_argument("--market", action="append", default=[], metavar="MARKET",
                    help="keep only these markets: unitedstates | canada. "
                         "Filters the CSV regardless of how the archive was "
                         "pulled — older archives predate pull-time scoping")
    ap.add_argument("--max-odometer", type=int, default=0, metavar="MILES",
                    help="drop lots over this mileage (and lots with no "
                         "odometer reading)")
    ap.add_argument("--max-distance", type=int, default=0, metavar="MILES",
                    help="drop lots farther than this from 98003 (and lots with "
                         "no branch coordinates)")
    ap.add_argument("--sold-only", action="store_true",
                    help="drop lots with no realised sale price")
    ap.add_argument("--out", help="output .csv path (default: alongside input)")
    ap.add_argument("--schema", action="store_true",
                    help="print the column mapping table and exit")
    return ap


def multiword(value):
    """argparse nargs='+' value -> one string, tolerating a plain str too."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return " ".join(str(x) for x in value).strip()


def csv_list(s):
    """Comma-separated needles. Spaces are part of a value, commas split them,
    so `--exclude-damage front & rear,water` is two needles, not four."""
    return [x.strip().lower() for x in multiword(s).split(",") if x.strip()]


def style_set(values):
    """Repeatable, multi-word, comma-separated body styles -> normalised set.

    Real IAAI values include "Compact Luxury Car" and "Sedan/Hatchback", so the
    same rule as everywhere else applies: spaces join a value, commas separate
    values. `--body-style Compact Luxury Car` is one style;
    `--body-style coupe,sedan` is two; repeating the flag adds more.
    """
    out = set()
    for occurrence in values or []:
        for token in multiword(occurrence).split(","):
            if token.strip():
                out.add(norm_style(token))
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)

    if args.schema:
        print_schema()
        return 0

    paths = resolve_inputs(args)
    print("=" * 78)
    print("IAAI raw JSON -> CSV")
    print("=" * 78)
    records = load_records(paths)
    if not records:
        raise SystemExit("no IAAI records found in the given archive(s)")

    filters = {
        "exclude_damage": csv_list(args.exclude_damage),
        "include_damage": csv_list(args.include_damage),
        "body_styles": style_set(args.body_style),
        "exclude_body_styles": style_set(args.exclude_body_style),
        "seller_classes": set(args.seller_class),
        "exclude_seller_classes": set(args.exclude_seller_class),
        "min_photos": args.min_photos,
        "sold_only": args.sold_only,
        "markets": {m.strip().lower() for m in args.market},
        "max_odometer": args.max_odometer,
        "max_distance": args.max_distance,
    }
    active = {k: v for k, v in filters.items() if v}
    print(f"\n  filters: {active or 'none (keeping every record)'}")

    # De-dupe across archives: overlapping pulls repeat lots (two pulls a day
    # apart shared 68 of 70). Keep the NEWEST copy — on `open`/`live` archives
    # the bid, status and photo count all move between pulls, so the freshest
    # record is the correct one. Ties fall back to file order.
    by_key = {}
    dupes = 0
    for v in records:
        key = (v.get("platform"), v.get("lot_number"), v.get("vin"))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = v
            continue
        dupes += 1
        if (v.get("_pulled_at") or "") >= (prev.get("_pulled_at") or ""):
            by_key[key] = v

    kept, dropped = [], []
    for v in by_key.values():
        ok, why = keep(v, filters)
        (kept if ok else dropped).append((v, why))
    seen = by_key

    print(f"  unique lots: {len(seen)}   (dropped {dupes} duplicate row(s) "
          f"across archives)")
    print(f"  kept {len(kept)}   filtered out {len(dropped)}")
    if dropped:
        reasons = {}
        for _, why in dropped:
            reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {why}")

    rows = [flatten(v) for v, _ in kept]

    # Distance has no fallback by design, so an empty one must be loud rather
    # than silently blank in a spreadsheet.
    no_dist = [r for r in rows if r["distance_mi"] is None]
    if no_dist:
        print(f"\n  *** {len(no_dist)} of {len(rows)} lot(s) have no "
              f"StorageLocation coordinates — distance_mi/bucket left empty. "
              f"Lots: {[r['lot_number'] for r in no_dist][:10]} ***")
    buckets = {}
    for r in rows:
        b = r["distance_bucket"] or "(none)"
        buckets[b] = buckets.get(b, 0) + 1
    print("\n  distance_bucket: "
          + str(dict(sorted(buckets.items(),
                            key=lambda kv: int(kv[0][:-2]) if kv[0].endswith("mi")
                            else 10 ** 9))))

    # Non-USD lots. IAA Canada comes back through the same search, so a run can
    # quietly contain lots whose money is CAD. Announce them: their *_usd
    # columns are blank by design, and their fee/import maths is not the US one.
    foreign = {}
    for v, _ in kept:
        cur = currency(v)
        if cur and cur != "USD":
            foreign.setdefault((market(v), cur), []).append(v.get("lot_number"))
    if foreign:
        print("\n  *** NON-USD LOTS — *_usd columns left EMPTY for these ***")
        for (mkt, cur), lots in sorted(foreign.items()):
            print(f"      {mkt or '?'} / {cur}: {len(lots)} lot(s)  {', '.join(map(str, lots[:8]))}")
        print("      Native amounts stay in the archive. app/fees.py is US-only, "
              "so these need their own fee treatment before any bid maths.")

    # Seller audit — seller_class is the column most likely to be wrong, so show
    # the evidence behind it. The supporting fields (Origin, SellerType, which
    # rule fired) are no longer CSV columns, so read them back off the record.
    audit = {}
    for v, _ in kept:
        cls, src = seller_class(v)
        k = (cls, src, clean(attrs(v).get("Origin")),
             sale_info(v).get("SellerType"),
             clean(g(v, "seller", "name")) or clean(sale_info(v).get("Seller")))
        audit[k] = audit.get(k, 0) + 1
    print("\n  --- seller classification audit ---")
    print(f"      {'n':>4}  {'class':<10} {'origin':<10} {'SellerType':<12} "
          f"{'source':<34} name")
    for (cls, src, origin, styp, name), n in sorted(audit.items(),
                                                    key=lambda kv: -kv[1])[:15]:
        print(f"      {n:>4}  {cls:<10} {str(origin or '—'):<10} "
              f"{str(styp or '—'):<12} {src:<34} {name or '—'}")

    # The default lands in csv-raw/ regardless of where the input came from, so
    # the layers stay separate even when the archive is passed by an odd path.
    # A relative --out is resolved against csv-raw/ too; an absolute one wins.
    #
    # A FILTERED run gets a distinct filename. csv-raw/ means "every lot in the
    # archive, flattened" — if a filtered result reused the canonical name, the
    # next unfiltered run would silently overwrite it (and vice versa), leaving
    # a file whose row count nobody can explain later.
    out_dir = layer_dir(records[-1].get("_mode", "ended"), "csv-raw",
                        records[-1].get("_platform", PLATFORM))
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = out_dir / out_path
    else:
        suffix = "_iaai_filtered" if active else "_iaai"
        out_path = out_dir / f"{paths[-1].stem}{suffix}.csv"
        if active:
            print(f"\n  note: filters are active, so this is NOT the canonical "
                  f"csv-raw extract — writing '{suffix}' to keep the unfiltered "
                  f"file intact. Persistent filtering belongs in csv-cut/ "
                  f"via data_pull_01.py.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print("\n" + "=" * 78)
    print(f"Done. {len(rows)} row(s) x {len(COLUMNS)} column(s)")
    print(f"  CSV -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
