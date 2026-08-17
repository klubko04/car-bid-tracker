"""
Stage 1.5 — reshape an iaai.com web archive into the Apibara record shape.

    pull_iaai_web_01.py  ->  json-raw/iaaiweb_*.json
                                  |
                             THIS SCRIPT  ->  json-adapted/adapted_*.json
                                  |
                        apibara_json2csv_iaai_01.py   (UNCHANGED)
                                  |
                             data_pull_01.py

WHY AN ADAPTER AND NOT A SECOND FLATTENER
------------------------------------------
Apibara does not hold an independent database: it proxies IAAI's own lot
view-model and unmasks a few fields. Measured on the 7 lots present in both a
web pull and an Apibara pull of the same 2018 A5s:

    details.attributes    208/208 keys shared, 0 web-only, 0 apibara-only
    vehicle_information   10/11 byte-identical
    vehicle_description   14/16 byte-identical
    sale_information       7/10 byte-identical

So a second 57-column flattener would be two copies of one mapping, drifting
apart on every edit. Instead this script rebuilds the handful of *derived*
blocks Apibara adds on top of the shared payload — vehicle_specs, condition,
odometer, pricing, sale_document, seller, auction, media — and hands the result
to the existing flattener untouched. One schema, one CSV, two sources.

The convenience layer is rebuilt from the WEB payload rather than taken from
Apibara because the web pull sees more lots: 65 vs 14 for the same 2018-2023 A5
search, including 56 in `Auction Not Assigned` that Apibara never returns.

    python analytics/scripts/iaai_web_adapt_01.py                    # newest web archive
    python analytics/scripts/iaai_web_adapt_01.py FILE.json ...      # specific ones
    python analytics/scripts/iaai_web_adapt_01.py --all
    python analytics/scripts/iaai_web_adapt_01.py --enrich-from apibara_*.json
    python analytics/scripts/iaai_web_adapt_01.py --audit            # per-column diff

WHAT THE WEB SOURCE CANNOT PROVIDE  (--enrich-from backfills these)
-------------------------------------------------------------------
Four columns, and only four:

    vin                 attributes.VIN is masked to 11 chars (WAUENCF5XJA******)
    seller_name         ProviderName/Seller blank; Apibara has "State Farm…"
    seller_name_masked  follows from seller_name
    current_bid_usd     live bid is loaded by an authenticated XHR, so it is
                        absent from the static page entirely. The lot HTML
                        literally ships "Bidding History … Error Loading Data".

Everything else reproduces byte-identically — including `buy_now_usd`, which is
`MinimumBidAmount` when `BuyNowIndicator` is set (verified: $3,100 on 45625127
matched Apibara exactly).

Seller CLASS is not in that list, and this is the useful part: `Origin` and
`ProviderType` survive IAAI's masking on 65/65 lots, and those are exactly what
the flattener's seller_class() reads. Insurance is identified on 60/65 with no
Apibara call at all. See PROVIDER_TYPE below for the dealer caveat.

WHAT IS APPROXIMATED  (documented, not silent)
----------------------------------------------
`sale_document`  Apibara expands the doc per state — web "SALVAGE (Nevada)"
                 becomes Apibara "SALVAGE - TOTAL LOSS", and "SALVAGE
                 (Virginia)" becomes "SALVAGE - BRANDED IF REBUILT". The web
                 short form is kept as-is; `title_state` already carries the
                 state in its own column, so no information is lost, but the
                 STRING differs from an Apibara row for the same lot.
`run_condition`  StartsCode maps CST -> RUNS AND DRIVES, WST -> STATIONARY / NO
                 INFORMATION. In the Apibara corpus 45/1009 CST lots are
                 "ENGINE START PROGRAM" instead, and nothing web-side
                 distinguishes them, so ~4% of CST rows will read RUNS AND
                 DRIVES where Apibara said ENGINE START PROGRAM.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
PLATFORM = "iaai"
RAW_LAYER = "json-raw"
OUT_LAYER = "json-adapted"          # derived, so NOT json-raw


def layer_dir(mode, layer, platform=PLATFORM):
    bucket = {"ended": "sold", "open": "open", "live": "open"}.get(mode, "open")
    return DATA_DIR / bucket / layer / platform


# --------------------------------------------------------------------------
# lookup tables — every entry derived from observed data, not invented
# --------------------------------------------------------------------------
# StartsCode -> Apibara's condition.run_condition.value. Counts are from the
# 1,349-lot Apibara IAAI corpus.
RUN_CONDITION = {
    "CST": ("RUNS AND DRIVES", "Runs and drives", "success"),      # 964 lots
    "WST": ("STATIONARY / NO INFORMATION",
            "Stationary / no information", "warning"),             # 332 lots
}

# Sale-document short form -> Apibara's sale_document_group. Keyed on the web
# short form ("SALVAGE (Nevada)" -> "SALVAGE"), which is coarser than Apibara's
# expanded name but lands in the same group for every observed value.
SALE_DOC_GROUP = {
    "SALVAGE": "approved",
    "SALVAGE CERTIFICATE": "approved",
    "ORIGINAL": "approved",
    "REBUILDABLE": "approved",
    "CLEAR": "approved",
    "MV-907A": "warning",
    "BILL OF SALE": "warning",
    "CERTIFICATE OF DESTRUCTION": "warning",
    "NON-REPAIRABLE": "warning",
    "JUNK": "warning",
}

# ProviderType -> seller class. INS and DLR are the codes whose meaning is
# unambiguous; the rest are recorded but left to seller_class()'s Origin
# fallback, which sends "Remarketing Vehicles" to unknown rather than guessing.
#   observed on the web pull: INS 59, COR 2, SDS 1, RCC 1, ADJ 1, DLR 1
PROVIDER_TYPE = {"INS": "insurance", "ADJ": "insurance", "DLR": "dealer"}

MI_PER_KM = 1.609344
IMG_TMPL = "https://vis.iaai.com/resizer?imageKeys={sid}~SID~I{n}&width={w}&height={h}"
VIDEO_TMPL = ("https://mediaretriever.iaai.com/api/EngineVideoRetriever"
              "?partitionKey={sid}&Tenant=iaai")
VR360_TMPL = ("https://vis.iaai.com/Home/ThreeSixtyView"
              "?keys=SID-{sid}~STP-1~INT-1&iframeview=true")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def num(x):
    """'$17,975 USD' / '76471' / 0 -> float, or None for absent/zero."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) or None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(x or ""))
    return float(m.group(0).replace(",", "")) if m else None


def txt(x):
    """Trim, and treat IAAI's blanks/masks/sentinels as absent."""
    s = str(x).strip() if x is not None else ""
    if not s or s.upper() in ("NONE", "N/A", "UNKNOWN") or set(s) == {"*"}:
        return None
    return s


def as_bool(x):
    if isinstance(x, bool):
        return x
    s = str(x or "").strip().lower()
    return True if s == "true" else False if s == "false" else None


def parse_dt(s):
    """'8/14/2026 1:30:00 PM +00:00' -> '2026-08-14T13:30:00+00:00'."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*"
                 r"(AM|PM)?\s*([+-]\d{2}:?\d{2})?", s, re.I)
    if not m:
        return None
    mo, d, y, hh, mm, ss, ampm, tz = m.groups()
    hh = int(hh)
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hh != 12:
            hh += 12
        elif ampm == "AM" and hh == 12:
            hh = 0
    tz = (tz or "+00:00").replace(":", "")
    tz = f"{tz[:3]}:{tz[3:]}"
    return (f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            f"T{hh:02d}:{int(mm):02d}:{int(ss):02d}{tz}")


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------
def _fields(rec):
    return (rec.get("detail") or {}).get("fields") or {}


def _attrs(rec):
    """detail.fields.attributes, falling back to the archived view_model.

    The fallback exists so archives written before pull_iaai_web_01 lifted
    attributes into fields still adapt — the data was always in view_model.
    """
    a = _fields(rec).get("attributes")
    if a:
        return a
    view = ((rec.get("detail") or {}).get("view_model") or {}) \
        .get("inventoryView") or {}
    return {k: v for k, v in (view.get("attributes") or {}).items()
            if not str(k).startswith("$")}


def _images(rec):
    img = _fields(rec).get("images")
    if img:
        return img
    view = ((rec.get("detail") or {}).get("view_model") or {}) \
        .get("inventoryView") or {}
    dims = view.get("imageDimensions") or {}

    def unref(node):
        vals = (node or {}).get("$values") if isinstance(node, dict) else None
        return vals if isinstance(vals, list) else []
    return {"keys": unref(dims.get("keys")), "videos": unref(dims.get("videos")),
            "image_360_url": dims.get("image360Url") or None}


def build_media(rec, a):
    """Apibara-shaped media block, rebuilt from IAAI's own image keys.

    IAAI's per-image key is '46203349~SID~B647~S0~I1~RW2576~H1932~TH0'; the
    resizer URL Apibara stores uses the compact '<sid>~SID~I<n>' form, which is
    also what iaai.com itself puts on the search thumbnails. Extracting the
    I-component and rebuilding gives byte-identical URLs — verified 7/7 on
    image count against Apibara.
    """
    img = _images(rec)
    sid = txt(a.get("SalvageId")) or str(rec.get("item_id") or "").split("~")[0]
    nums = []
    for k in img.get("keys") or []:
        key = k.get("k") if isinstance(k, dict) else k
        m = re.search(r"~I(\d+)", str(key or ""))
        if m:
            nums.append(int(m.group(1)))
    nums.sort()

    thumbs = [IMG_TMPL.format(sid=sid, n=n, w=400, h=300) for n in nums]
    items = [{"type": "image",
              "thumb": IMG_TMPL.format(sid=sid, n=n, w=400, h=300),
              "large": IMG_TMPL.format(sid=sid, n=n, w=845, h=633)}
             for n in nums]

    has_video = bool(img.get("videos"))
    if has_video:
        items.append({"type": "video", "url": VIDEO_TMPL.format(sid=sid)})
    url360 = img.get("image_360_url") or (
        VR360_TMPL.format(sid=sid) if txt(a.get("Link360")) else None)
    if url360:
        items.append({"type": "vr360", "url": url360})

    return {"thumbs": thumbs, "thumbs_count": len(thumbs), "items": items,
            "has_360": bool(url360), "has_video": has_video}


_BUY_NOW_RE = re.compile(r"Buy Now\s*\$([\d,]+)", re.I)


def view_flag(rec, key):
    """One flag off inventoryView, which the fields lift does not cover."""
    view = ((rec.get("detail") or {}).get("view_model") or {}).get("inventoryView") or {}
    return view.get(key)


def who_can_buy(rec):
    """Licence classes eligible to bid, e.g. 'DEA,DIS,EXP,REB,SCR'.

    Apibara exposes this as attributes.WhoCanBuy. The web payload leaves that
    key null and puts the value under auctionInformation.biddingInformation
    instead, so without this it would be dropped in adaptation.

    Populated only once a lot has a sale assigned — 9/65 on the reference pull,
    exactly the 9 not in `Auction Not Assigned`.
    """
    ai = ((rec.get("detail") or {}).get("view_model") or {}).get("auctionInformation") or {}
    vals = ((ai.get("biddingInformation") or {}).get("whoCanBuy") or {}).get("$values") or []
    return txt(vals[0]) if vals else None


def buy_now_usd(rec, a, listing):
    """Buy-now price, taking the SEARCH ROW as the authority.

    The lot page is not reliable here: once a buy-now has been taken, IAAI sets
    buyNowSold=True and zeroes buyNowIndicator/buyNowAmount on the detail
    view-model, while the search row still prints "Buy Now $6,875 USD". Lot
    45250068 is exactly that case — Apibara reports 6875 and the lot page
    reports nothing. Reading the row first agrees with Apibara on both observed
    buy-now lots; MinimumBidAmount remains as a fallback.
    """
    m = _BUY_NOW_RE.search(" | ".join(rec.get("row_text") or []))
    if m:
        return num(m.group(1))
    if as_bool(a.get("BuyNowIndicator")) or listing.get("BuyNowIndicator"):
        return num(a.get("MinimumBidAmount"))
    return None


def adapt(rec):
    """One web record -> one Apibara-shaped record."""
    a = _attrs(rec)
    f = _fields(rec)
    vi = f.get("vehicle_information") or {}
    vd = f.get("vehicle_description") or {}
    si = f.get("sale_information") or {}
    listing = rec.get("listing") or {}
    ident = rec.get("identity") or {}

    # Fill the attribute Apibara populates but the web page leaves null, so the
    # adapted record is addressable by the same path from either source.
    wcb = who_can_buy(rec)
    if wcb and not txt(a.get("WhoCanBuy")):
        a = {**a, "WhoCanBuy": wcb}

    engine = txt(a.get("EngineInformation")) or txt(a.get("EngineSize")) or ""
    hp = re.search(r"(\d+)\s*HP", engine, re.I)
    layout = re.search(r"\b([IVWH])-\d", engine)

    starts = RUN_CONDITION.get(str(a.get("StartsCode") or "").upper())
    doc_name = (txt(vi.get("TitleSaleDoc")) or txt(si.get("TitleSaleDoc")) or "")
    doc_name = re.sub(r"\s*\(.*\)\s*$", "", doc_name).strip() or None

    odo_mi = num(a.get("ODOValue"))
    buy_now = buy_now_usd(rec, a, listing)

    auction_at = parse_dt(a.get("AuctionDateTime")) or listing.get("AuctionDate")

    return {
        # ---- identity ----
        "platform": PLATFORM,
        "platform_id": 2,
        "lot_number": txt(a.get("StockNumber")) or rec.get("stock_number"),
        "vin": txt(a.get("VIN")) or ident.get("vin_mask"),
        "year": int(a["Year"]) if str(a.get("Year") or "").isdigit()
        else ident.get("year"),
        "make": txt(a.get("Make")) or ident.get("make"),
        "model": txt(a.get("Model")) or ident.get("model"),
        "title": txt(a.get("YearMakeModelSeries")),
        "type": txt(a.get("InventoryType")),
        "subLot": False,
        "ad": auction_at,

        # ---- the shared payload, passed through untouched ----
        "details": {
            "attributes": a,
            "vehicle_information": vi,
            "vehicle_description": vd,
            "sale_information": si,
        },

        # ---- the convenience layer, rebuilt from the above ----
        "vehicle_specs": {
            "body_style": txt(a.get("Segment")) or txt(a.get("VehicleClass")),
            "engine": {"raw": engine or None,
                       "size_l": num(a.get("DisplLiters")),
                       "hp": int(hp.group(1)) if hp else None,
                       "layout": layout.group(1) if layout else None},
            "transmission": txt(a.get("Transmission")),
            "drive_type": txt(vd.get("DriveLineType")) or txt(a.get("DriveLineTypeDesc")),
            "fuel_type": txt(a.get("FuelTypeDesc")),
            "exterior_color": (txt(a.get("ExteriorColor")) or "").title() or None,
            "airbags": txt(a.get("AirbagState")),
            "restraint_system": txt(a.get("RestraintType")),
        },
        "condition": {
            # Apibara sentence-cases the SHOUTING attribute; secondary comes
            # from vehicle_information, which is already title-cased there.
            "primary_damage": (txt(a.get("PrimaryDamageDesc")) or "").capitalize() or None,
            "secondary_damage": txt(vi.get("SecondaryDamage")),
            "has_key": as_bool(a.get("Keys")),
            "run_condition": {"value": starts[0] if starts else None,
                              "label": starts[1] if starts else None,
                              "class_hint": starts[2] if starts else None},
        },
        "odometer": {"mi": int(odo_mi) if odo_mi else None,
                     "km": int(round(odo_mi * MI_PER_KM)) if odo_mi else None},
        "pricing": {
            # Live bid is XHR-loaded and absent from the static page — left
            # None rather than guessed. --enrich-from fills it from Apibara.
            "current_bid_usd": None,
            "current_bid2_usd": None,
            "buy_now_usd": buy_now,
            "last_sold_price_usd": None,
            "estimated_cost": {},
        },
        "sale_document": {"name": doc_name,
                          "sale_document_group": SALE_DOC_GROUP.get(
                              (doc_name or "").upper())},
        "seller": {"name": txt(a.get("ProviderName")),
                   "type": PROVIDER_TYPE.get(str(a.get("ProviderType") or "").upper())},
        "auction": {
            "auction_at": auction_at,
            "ad": auction_at,
            "full_date": auction_at,
            "state": (rec.get("state") or {}).get("state"),
            "last_sold_day": None,
            "last_sold_status": None,
            "is_buy_now": bool(buy_now),
            "is_timed": as_bool(a.get("TimedAuctionIndicator"))
            or bool(listing.get("TimedAuctionIndicator")),
            # Hard close of a timed (online-only) sale. Distinct from
            # auction_at: on lot 45662018 the timed sale closed 2026-08-15
            # 01:53 while the live sale date stayed 2026-08-21 11:30, so the
            # deadline to act was six days earlier than the headline date.
            "timed_end_at": (parse_dt(a.get("TimedAuctionCloseDateTime"))
                             or listing.get("TimedAuctionCloseDateTime")),
            # Expiry of the Buy Now offer — again distinct from auction_at, and
            # typically ~12h before it (lot 45704693: buy-now closes 08-21
            # 01:00, live sale 08-21 13:30).
            "buy_now_close_at": parse_dt(a.get("BuyNowCloseDateTime")),
            # IAAI sets this while the lot is STILL LISTED, so a buy-now sale is
            # observable before the lot leaves the site — lot 45250068 carried
            # it on 08-13 and was gone by 08-14. Far stronger than inferring a
            # sale from a disappearance, which cannot distinguish sold from
            # withdrawn.
            "sold_buy_now": bool(view_flag(rec, "buyNowSold")),
        },
        "location": {"display": txt(a.get("BranchName")),
                     "state": txt(a.get("BranchState"))},
        "media": build_media(rec, a),
        "_web_item_id": rec.get("item_id"),
        "_web_state": (rec.get("state") or {}).get("state"),
        # The search that found this lot; lot_history_01 uses it to decide which
        # later snapshots are entitled to call the lot absent.
        "_web_keyword": rec.get("keyword"),
        # "full"   = step 2 ran: attributes, ACV, repair estimate, damage codes
        # "search" = step 1 only: identity, state, auction date, buy-now
        # A search-only pull is the cheap twice-daily cadence (1 request), and
        # it carries exactly what lot_history_01 needs — so those records are
        # adapted, not discarded.
        "_detail_level": "full" if rec.get("detail") else "search",
    }


# --------------------------------------------------------------------------
# enrichment from an Apibara archive
# --------------------------------------------------------------------------
ENRICH_FIELDS = ("vin", "seller_name", "current_bid_usd")


def load_apibara(paths):
    """lot_number -> apibara record. Newest wins per FIELD, not per record.

    Whole-record newest-wins loses data: Apibara's seller.name is intermittently
    absent (26% of one observed pull reports seller.type='unknown' for lots IAAI
    itself labels Insurance), so a newer archive with a gap would erase a good
    name from an older one. Observed on lot 45704693 — the 08:52 pull named
    Progressive Casualty Insurance, the 14:13 pull did not.

    Archives are ordered by their own generated_at rather than argv order, so
    the merge does not depend on how the caller listed the files.
    """
    docs = []
    for p in paths:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        docs.append((doc.get("generated_at") or "", doc))
    docs.sort(key=lambda kv: kv[0])          # oldest first; newer overwrites

    out = {}
    for _, doc in docs:
        for page in doc.get("pages", []):
            if page.get("status") != 200:
                continue
            for v in (page.get("raw", {}).get("data") or []):
                key = str(v.get("lot_number") or "")
                if not key:
                    continue
                prev = out.get(key)
                if prev is None:
                    out[key] = v
                    continue
                # Newer record wins, but never by replacing a value with a blank.
                merged = dict(v)
                if not txt((v.get("seller") or {}).get("name")):
                    merged["seller"] = prev.get("seller") or v.get("seller")
                if not txt(v.get("vin")):
                    merged["vin"] = prev.get("vin")
                if (v.get("pricing") or {}).get("current_bid_usd") is None:
                    merged["pricing"] = {**(v.get("pricing") or {}),
                                         "current_bid_usd": (prev.get("pricing") or {})
                                         .get("current_bid_usd")}
                out[key] = merged
    return out


def enrich(adapted, apibara_by_lot):
    """Backfill ONLY what the web source cannot see. Never overwrites."""
    filled = dict.fromkeys(ENRICH_FIELDS, 0)
    matched = 0
    for rec in adapted:
        src = apibara_by_lot.get(str(rec.get("lot_number") or ""))
        if not src:
            continue
        matched += 1
        vin = txt(src.get("vin"))
        if vin and "*" in str(rec.get("vin") or ""):
            rec["vin"] = vin
            filled["vin"] += 1
        name = txt((src.get("seller") or {}).get("name"))
        if name and not rec["seller"].get("name"):
            rec["seller"]["name"] = name
            rec["seller"]["type"] = (src.get("seller") or {}).get("type") \
                or rec["seller"].get("type")
            # Unmask the passthrough copies too. seller_name_masked reads
            # sale_information.SellerType, so leaving "******" there would keep
            # flagging the row as masked after the name has been recovered.
            si = (rec.get("details") or {}).get("sale_information") or {}
            src_si = ((src.get("details") or {}).get("sale_information") or {})
            if txt(src_si.get("Seller")):
                si["Seller"] = src_si["Seller"]
            if txt(src_si.get("SellerType")):
                si["SellerType"] = src_si["SellerType"]
            filled["seller_name"] += 1
        bid = (src.get("pricing") or {}).get("current_bid_usd")
        if bid is not None and rec["pricing"].get("current_bid_usd") is None:
            rec["pricing"]["current_bid_usd"] = bid
            filled["current_bid_usd"] += 1
        rec["_enriched_from"] = "apibara"
    return matched, filled


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def web_archives():
    return sorted((p for b in BUCKETS
                   for p in (DATA_DIR / b / RAW_LAYER / PLATFORM).glob("iaaiweb_*.json")),
                  key=lambda p: p.stat().st_mtime)


def resolve_one(f):
    p = Path(f)
    if p.is_absolute() or p.exists():
        return p
    for b in BUCKETS:
        cand = DATA_DIR / b / RAW_LAYER / PLATFORM / f
        if cand.exists():
            return cand
    raise SystemExit(f"input not found: {f}")


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="iaai_web_adapt_01.py",
        description="Reshape iaai.com web archives into the Apibara record "
                    "shape so apibara_json2csv_iaai_01.py can flatten them. "
                    "Offline — no API calls, no HTTP.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="web archive .json (default: newest)")
    ap.add_argument("--all", action="store_true", help="every web archive")
    ap.add_argument("--enrich-from", nargs="+", default=[], metavar="APIBARA.json",
                    help="backfill vin / seller_name / current_bid_usd from "
                         "Apibara archives, joined on lot_number")
    ap.add_argument("--audit", action="store_true",
                    help="for lots present in BOTH sources, diff every adapted "
                         "field against Apibara's and report per-column agreement")
    ap.add_argument("--out", help="output path (default: auto-named)")
    return ap


def audit(adapted, apibara_by_lot):
    """Per-column agreement, computed through the real flattener."""
    import apibara_json2csv_iaai_01 as F
    both = [r for r in adapted if str(r.get("lot_number")) in apibara_by_lot]
    if not both:
        print("\n  --audit: no lots present in both sources; nothing to compare")
        return
    cols = list(F.COLUMNS)
    agree = dict.fromkeys(cols, 0)
    for r in both:
        a = apibara_by_lot[str(r["lot_number"])]
        wrow, arow = F.flatten(r), F.flatten(a)
        for c in cols:
            if str(wrow.get(c, "")).strip() == str(arow.get(c, "")).strip():
                agree[c] += 1
    n = len(both)
    same = [c for c in cols if agree[c] == n]
    diff = [c for c in cols if agree[c] != n]
    print(f"\n  --audit: {n} lot(s) in both sources")
    print(f"    identical on all {n}: {len(same)}/{len(cols)} columns")
    if diff:
        print(f"    differing:")
        for c in diff:
            print(f"      {agree[c]:>3}/{n}  {c}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)

    if args.files:
        paths = [resolve_one(f) for f in args.files]
    else:
        pool = web_archives()
        if not pool:
            raise SystemExit(
                f"no web archives under {DATA_DIR}/{{{'|'.join(BUCKETS)}}}/"
                f"{RAW_LAYER}/{PLATFORM}/iaaiweb_*.json — run pull_iaai_web_01.py first")
        paths = pool if args.all else [pool[-1]]

    print("=" * 78)
    print("IAAI web archive -> Apibara record shape")
    print("=" * 78)

    adapted, generated_at, mode, skipped = [], None, "open", 0
    search_params = {}
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("source") != "iaai-web":
            print(f"  !! skipping {p.name}: source={doc.get('source')!r}, "
                  f"this adapter is for pull_iaai_web_01 archives")
            continue
        generated_at = generated_at or doc.get("generated_at")
        mode = doc.get("mode", "open")
        search_params = doc.get("search_params") or search_params
        # Truncation makes absence meaningless: a lot missing from a capped
        # query may simply be past the 100-row ceiling, not gone from the site.
        if any(q.get("truncated") for q in doc.get("queries", [])):
            search_params = {**search_params, "truncated": True}
        # Archives written before pull_iaai_web_01 tagged records with their
        # keyword carry none. When such an archive ran exactly ONE search, every
        # record provably came from it, so the tag can be recovered rather than
        # left unknown — and unknown scope blocks absence detection entirely.
        doc_kws = (doc.get("search_params") or {}).get("keywords") or []
        only_kw = doc_kws[0] if len(doc_kws) == 1 else None

        n = 0
        for rec in doc.get("records", []):
            if rec.get("detail_gone"):
                skipped += 1          # lot left the site mid-pull
                continue
            if only_kw and not rec.get("keyword"):
                rec = {**rec, "keyword": only_kw}
            adapted.append(adapt(rec))
            n += 1
        thin = sum(1 for r in adapted if r["_detail_level"] == "search")
        print(f"  adapted {n:>4} of {len(doc.get('records', [])):>4} record(s) "
              f"from {p.name}")

    if not adapted:
        raise SystemExit("nothing to adapt — the archive has no records")
    thin = sum(1 for r in adapted if r["_detail_level"] == "search")
    if thin:
        print(f"  {thin}/{len(adapted)} record(s) are search-only (no --details): "
              f"identity, state, auction date and buy-now only.\n"
              f"  Enough for lot_history_01; ACV / repair / damage need a "
              f"--details pull.")
    if skipped:
        print(f"  !! {skipped} record(s) had left the site (DetailsNotFoundView)")

    apibara_by_lot = {}
    if args.enrich_from:
        apibara_by_lot = load_apibara([resolve_one(f) for f in args.enrich_from])
        matched, filled = enrich(adapted, apibara_by_lot)
        print(f"\n  enrichment: {matched}/{len(adapted)} lot(s) matched an "
              f"Apibara record by lot_number")
        for k, v in filled.items():
            print(f"      {v:>4}  {k} filled")

    # Coverage of the four web-blind columns, so a run always says where it stands.
    masked = sum(1 for r in adapted if "*" in str(r.get("vin") or ""))
    no_seller = sum(1 for r in adapted if not (r.get("seller") or {}).get("name"))
    no_bid = sum(1 for r in adapted
                 if (r.get("pricing") or {}).get("current_bid_usd") is None)
    print(f"\n  still web-blind: vin masked {masked}/{len(adapted)}   "
          f"seller unnamed {no_seller}/{len(adapted)}   "
          f"no current bid {no_bid}/{len(adapted)}")

    if args.audit:
        audit(adapted, apibara_by_lot or load_apibara(
            [p for b in BUCKETS
             for p in (DATA_DIR / b / RAW_LAYER / PLATFORM).glob("apibara_*.json")]))

    out = {
        "generated_at": generated_at or dt.datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "adapted_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": argv,
        "platform": PLATFORM,
        "source": "iaai-web-adapted",
        "adapted_from": [p.name for p in paths],
        "enriched_from": [Path(f).name for f in args.enrich_from],
        # Carried through so lot_history_01 can tell which search space this
        # snapshot covered. Absence of a lot only means "gone" within the same
        # search — see cohort_key().
        "search_params": search_params,
        "mode": mode,
        # pages[] is the envelope apibara_json2csv_iaai_01.load_records expects.
        "pages": [{"status": 200, "raw": {"data": adapted}}],
        "counts": {"records": len(adapted), "skipped_no_detail": skipped},
    }

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = layer_dir(mode, OUT_LAYER) / out_path
    else:
        out_path = layer_dir(mode, OUT_LAYER) / f"adapted_{paths[-1].stem}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"Done. {len(adapted)} record(s) in Apibara shape.")
    print(f"  JSON -> {out_path}")
    print(f"  next: python analytics/scripts/apibara_json2csv_iaai_01.py {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
