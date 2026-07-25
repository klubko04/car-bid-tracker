"""
Apibara.tech adapter — PRIMARY discovery source (real Copart + IAAI).

Docs: https://apibara.tech/llms-full.txt
Base:  https://apibara.tech/api/v1/vehicle-auction
Auth:  X-API-Key header
Search: GET /vehicles   (cursor pagination, per_page max 20)
Lookup: GET /vehicles/{vin-or-lot}

Unlike MarketCheck, Apibara returns the fields that matter for salvage:
current bid, primary/secondary damage, title/sale-document, odometer,
run condition, seller type, sale date, location, photos.

Env:
  APIBARA_API_KEY     required for live discovery/lookup
  APIBARA_BASE_URL    default https://apibara.tech/api/v1/vehicle-auction
  APIBARA_MAX_PAGES   default 1 (20 lots/page; keep low on the free plan)
  APIBARA_MOCK=1      testing only: canned response, no HTTP
"""
import asyncio
import os

import httpx

API_KEY = os.getenv("APIBARA_API_KEY", "")
BASE = os.getenv("APIBARA_BASE_URL", "https://apibara.tech/api/v1/vehicle-auction").rstrip("/")
MAX_PAGES = int(os.getenv("APIBARA_MAX_PAGES", "1"))
MOCK = os.getenv("APIBARA_MOCK", "") == "1"
RATE_DELAY = 1.1  # seconds between calls (Test plan = 1 req/sec)

# Copart/IAAI primary_damage -> our damage_type codes
_DAMAGE_MAP = [
    (("water", "flood"), "flood"),
    (("burn", "fire"), "fire"),
    (("theft", "stripped", "stolen", "missing"), "theft_recovery"),
    (("hail",), "hail"),
    (("frame", "undercarriage", "rollover", "roll over"), "frame"),
    (("minor dent", "scratch", "normal wear", "vandalism", "cosmetic"), "minor_collision"),
    (("front", "rear", "side", "all over", "top", "roof"), "moderate_collision"),
]


def is_configured() -> bool:
    return bool(API_KEY) or MOCK


def _headers() -> dict:
    return {"Accept": "application/json", "X-API-Key": API_KEY}


def _map_damage(primary: str, secondary: str = "") -> str:
    text = f"{primary} {secondary}".lower()
    for needles, code in _DAMAGE_MAP:
        if any(n in text for n in needles):
            return code
    return "other"


def _map_title(rec: dict) -> str:
    sd = rec.get("sale_document") or {}
    name = str(sd.get("name") or "").lower()
    if "clean" in name or "clear" in name:
        return "clean"
    # salvage/rebuilt/flood/junk/parts/non-repairable all use the salvage fee table
    return "salvage"


def _map_damage_full(rec: dict) -> str:
    """Damage code, preferring the sale-document group (catches flood/fire even
    when primary_damage is a collision code)."""
    cond = rec.get("condition") or {}
    sd = rec.get("sale_document") or {}
    group = str(sd.get("sale_document_group") or "").lower()
    if "water" in group or "flood" in group:
        return "flood"
    if "fire" in group or "burn" in group:
        return "fire"
    return _map_damage(cond.get("primary_damage") or "", cond.get("secondary_damage") or "")


def _platform_to_source(platform: str) -> str:
    return "iaai" if "iaa" in (platform or "").lower() else "copart"


def _lot_url(platform: str, lot: str, slug: str = "") -> str:
    if not lot:
        return ""
    if "iaa" in (platform or "").lower():
        return f"https://www.iaai.com/VehicleDetail/{lot}"
    return f"https://www.copart.com/lot/{lot}"


def normalize_record(rec: dict, search_id: int = 0) -> dict:
    auction = rec.get("auction") or {}
    pricing = rec.get("pricing") or {}
    location = rec.get("location") or {}
    condition = rec.get("condition") or {}
    odometer = rec.get("odometer") or {}
    media = rec.get("media") or {}
    specs = rec.get("vehicle_specs") or {}
    seller = rec.get("seller") or {}

    platform = rec.get("platform") or "copart"
    lot = str(rec.get("lot_number") or "")

    # run condition is a nested object: {"value": "RUNS AND DRIVES", "label": ...}
    rc = condition.get("run_condition")
    if isinstance(rc, dict):
        run_cond = str(rc.get("value") or rc.get("label") or "")
    else:
        run_cond = str(rc or condition.get("run_cond") or "")

    primary_dmg = condition.get("primary_damage") or ""
    secondary_dmg = condition.get("secondary_damage") or ""
    loss = condition.get("loss") or condition.get("loss_type") or ""
    sd = rec.get("sale_document") or {}
    title_name = sd.get("name") or ""

    auction_at = auction.get("auction_at") or auction.get("full_date") or ""
    auction_at = str(auction_at).replace("Z", "").split("+")[0][:19] if auction_at else ""

    bid = pricing.get("current_bid_usd")
    if bid is None:
        bid = pricing.get("current_bid") or pricing.get("high_bid") or 0

    # trim: type -> body_style -> parse from title tail ("2018 AUDI A4 PREMIUM" -> PREMIUM)
    trim = rec.get("type") or specs.get("body_style") or ""
    if not trim and rec.get("title"):
        tail = str(rec["title"]).upper().split(str(rec.get("model") or "").upper())
        if len(tail) > 1 and tail[-1].strip():
            trim = tail[-1].strip().title()

    notes_bits = [b for b in (
        f"Damage: {primary_dmg}" + (f" / {secondary_dmg}" if secondary_dmg else "") if primary_dmg else "",
        f"Title: {title_name}" if title_name else "",
        f"Loss: {loss}" if loss else "",
        f"Run cond: {run_cond}" if run_cond else "",
        f"Seller: {seller.get('name') or seller.get('type')}" if seller else "",
        f"{media.get('thumbs_count') or media.get('image_count') or 0} photos" if media else "",
        "Imported from Apibara (Copart/IAAI live).",
    ) if b]

    return {
        "mc_listing_id": f"apibara:{platform}:{lot or rec.get('vin') or rec.get('slug_vin')}",
        "search_id": search_id,
        "lot_number": lot,
        "vin": rec.get("vin") or "",
        "source": _platform_to_source(platform),
        "platform": "iaai_public" if _platform_to_source(platform) == "iaai" else "copart_public",
        "year": rec.get("year"),
        "make": (rec.get("make") or "").title(),
        "model": rec.get("model") or "",
        "trim": trim,
        "miles": odometer.get("mi") or None,
        "title_type": _map_title(rec),
        "damage_type": _map_damage_full(rec),
        "run_drive": "run" in run_cond.lower(),
        "auction_date": auction_at,
        "location": location.get("display") or "",
        "listing_url": _lot_url(platform, lot, rec.get("slug_vin", "")),
        "current_bid": float(bid or 0),
        "status": "watching",
        "is_new": 1,
        "notes": " | ".join(notes_bits),
    }


def _build_params(s: dict) -> dict:
    p = {"per_page": 20, "lot_status": "All"}
    if s.get("make"):
        p["make"] = s["make"]
    if s.get("model"):
        p["model"] = s["model"]
    if s.get("year_min"):
        p["year_from"] = s["year_min"]
    if s.get("year_max"):
        p["year_to"] = s["year_max"]
    if s.get("price_max"):
        p["price_max"] = int(s["price_max"])
    if s.get("miles_max"):
        p["odometer_to"] = int(s["miles_max"])
    if s.get("zip"):
        p["zip"] = s["zip"]
        p["radius"] = int(s.get("radius") or 100)
        p["units"] = "mi"
    elif s.get("state"):
        p["loc_state"] = s["state"]
    return p


_MOCK = {
    "data": [
        {"platform": "copart", "lot_number": "54386186", "vin": "JTHCM1D26G5111111",
         "title": "2016 LEXUS IS 300", "year": 2016, "make": "LEXUS", "model": "IS 300",
         "type": "Sedan", "auction": {"state": "open", "auction_at": "2026-07-30T14:00:00+00:00"},
         "pricing": {"current_bid_usd": 4250, "buy_now_usd": None},
         "location": {"display": "Portland North (OR)", "send_from": "Portland"},
         "condition": {"primary_damage": "Front End", "secondary_damage": "", "has_key": True,
                       "run_cond": "Run and Drive", "loss_type": "Collision"},
         "odometer": {"mi": 88420}, "sale_document": {"type": "Salvage Certificate"},
         "media": {"thumbs_count": 11, "has_video": False}},
        {"platform": "iaai", "lot_number": "31990244", "vin": "JTHCM1D26H5222222",
         "title": "2017 LEXUS IS 300 F SPORT", "year": 2017, "make": "LEXUS", "model": "IS 300",
         "type": "Sedan", "auction": {"state": "open", "auction_at": "2026-07-28T17:30:00+00:00"},
         "pricing": {"current_bid_usd": 1800, "buy_now_usd": 9500},
         "location": {"display": "Seattle (WA)", "send_from": "Seattle"},
         "condition": {"primary_damage": "Water/Flood", "secondary_damage": "Electrical",
                       "has_key": False, "run_cond": "Stationary", "loss_type": "Water/Flood"},
         "odometer": {"mi": 96100}, "sale_document": {"type": "Certificate of Title - Salvage"},
         "media": {"thumbs_count": 8, "has_video": False}},
    ],
    "meta": {"per_page": 20, "next_cursor": None, "prev_cursor": None},
}


async def _get(path: str, params: dict) -> dict:
    if MOCK:
        return _MOCK
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE}{path}", params=params, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def search(s: dict) -> dict:
    """Run a saved search against Apibara. Returns {cars, num_found, error}."""
    params = _build_params(s)
    cars, cursor, pages = [], None, 0
    try:
        while pages < MAX_PAGES:
            if cursor:
                params = {**params, "cursor": cursor}
            data = await _get("/vehicles", params)
            for rec in data.get("data") or []:
                cars.append(normalize_record(rec, s["id"]))
            cursor = (data.get("meta") or {}).get("next_cursor")
            pages += 1
            if not cursor:
                break
            if not MOCK:
                await asyncio.sleep(RATE_DELAY)
        return {"cars": cars, "num_found": len(cars), "error": None}
    except httpx.HTTPStatusError as e:
        return {"cars": cars, "num_found": len(cars),
                "error": f"Apibara HTTP {e.response.status_code}: {e.response.text[:150]}"}
    except httpx.HTTPError as e:
        return {"cars": cars, "num_found": len(cars), "error": f"Request failed: {e}"}


async def lookup(vin: str = "", lot: str = "") -> dict:
    """Fetch one vehicle by VIN or lot number for the Add-Car form."""
    ident = (vin or lot or "").strip()
    if not ident:
        return {"found": False, "car": None, "provider": "apibara",
                "error": "Provide a VIN or lot number."}
    try:
        data = await _get(f"/vehicles/{ident}", {})
        inner = data.get("data", data)
        if isinstance(inner, list):
            inner = inner[0] if inner else {}
        rec = inner if isinstance(inner, dict) else {}
        if not rec or not (rec.get("lot_number") or rec.get("vin")):
            return {"found": False, "car": None, "provider": "apibara",
                    "error": "Not found on Copart/IAAI via Apibara."}
        car = normalize_record(rec, 0)
        for k in ("search_id", "is_new", "status", "notes"):
            car.pop(k, None)
        return {"found": True, "car": car, "provider": "apibara", "error": None}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        msg = "Not found." if code == 404 else f"Apibara HTTP {code}."
        return {"found": False, "car": None, "provider": "apibara", "error": msg}
    except httpx.HTTPError as e:
        return {"found": False, "car": None, "provider": "apibara", "error": f"Request failed: {e}"}


async def usage() -> dict:
    """Current quota usage (for the header badge). Best-effort."""
    if MOCK:
        return {"ok": True, "raw": {"used": 12, "limit": 100}}
    try:
        data = await _get("/usage", {})
        return {"ok": True, "raw": data}
    except httpx.HTTPError as e:
        return {"ok": False, "error": str(e)}
