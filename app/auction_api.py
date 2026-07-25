"""
Auction data lookup — pluggable provider adapter.

Providers:
  demo  — built-in sample lots, works with no key (default).
  live  — HTTP aggregator API (e.g. auction-api.app, free 10-day trial).
          Their docs are behind login, so base URL / endpoint paths / auth
          header are all configurable via .env. After you get your key and
          docs, adjust AUCTION_API_* values — no code changes needed unless
          field names differ (then tweak _normalize below).

.env keys (see .env.example):
  AUCTION_API_PROVIDER=demo|live
  AUCTION_API_BASE_URL=https://auction-api.app/api/v2
  AUCTION_API_KEY=...
  AUCTION_API_KEY_HEADER=X-Api-Key        (or "Authorization: Bearer" style)
  AUCTION_API_VIN_PATH=/cars/{vin}
  AUCTION_API_LOT_PATH=/lots/{source}/{lot}
"""
import os

import httpx

PROVIDER = os.getenv("AUCTION_API_PROVIDER", "demo").lower()
BASE_URL = os.getenv("AUCTION_API_BASE_URL", "https://auction-api.app/api/v2").rstrip("/")
API_KEY = os.getenv("AUCTION_API_KEY", "")
KEY_HEADER = os.getenv("AUCTION_API_KEY_HEADER", "X-Api-Key")
VIN_PATH = os.getenv("AUCTION_API_VIN_PATH", "/cars/{vin}")
LOT_PATH = os.getenv("AUCTION_API_LOT_PATH", "/lots/{source}/{lot}")

DEMO_LOTS = [
    {
        "lot_number": "58214036", "vin": "WAUFGAFC5FN012345", "source": "copart",
        "year": 2019, "make": "Audi", "model": "A6", "trim": "3.0T Premium Plus",
        "title_type": "salvage", "damage_type": "minor_collision", "run_drive": True,
        "auction_date": "2026-07-10T14:00:00", "location": "Sacramento, CA",
        "listing_url": "https://www.copart.com/lot/58214036",
        "current_bid": 4200, "clean_value": 21500,
    },
    {
        "lot_number": "31877421", "vin": "4T1BF1FK5HU678901", "source": "iaai",
        "year": 2021, "make": "Toyota", "model": "Camry", "trim": "SE",
        "title_type": "salvage", "damage_type": "theft_recovery", "run_drive": True,
        "auction_date": "2026-07-08T10:30:00", "location": "Phoenix, AZ",
        "listing_url": "https://www.iaai.com/vehicledetail/31877421",
        "current_bid": 6800, "clean_value": 19800,
    },
    {
        "lot_number": "77120988", "vin": "1HGCV1F34LA045678", "source": "copart",
        "year": 2020, "make": "Honda", "model": "Accord", "trim": "Sport",
        "title_type": "salvage", "damage_type": "hail", "run_drive": True,
        "auction_date": "2026-07-15T13:00:00", "location": "Dallas, TX",
        "listing_url": "https://www.copart.com/lot/77120988",
        "current_bid": 3100, "clean_value": 17400,
    },
]

# Field-name candidates seen across auction aggregators, tried in order.
_FIELD_MAP = {
    "lot_number": ["lot_number", "lot", "lot_id", "lotNumber", "stock", "stock_number"],
    "vin": ["vin", "VIN"],
    "source": ["source", "auction", "auction_name", "site", "base_site"],
    "year": ["year", "vehicle_year"],
    "make": ["make", "brand", "manufacturer"],
    "model": ["model"],
    "trim": ["trim", "series", "version"],
    "title_type": ["title_type", "title", "document", "sale_document"],
    "damage_type": ["damage_type", "damage", "primary_damage", "damage_pr"],
    "run_drive": ["run_drive", "runs_drives", "engine_starts", "status"],
    "auction_date": ["auction_date", "sale_date", "auction_dt", "date"],
    "location": ["location", "yard", "branch", "city", "location_name"],
    "listing_url": ["listing_url", "url", "link", "lot_url"],
    "current_bid": ["current_bid", "bid", "current_bid_amount", "pre_bid"],
}

_DAMAGE_ALIASES = {
    "front end": "moderate_collision", "rear end": "moderate_collision",
    "side": "moderate_collision", "minor dent/scratches": "minor_collision",
    "minor dents": "minor_collision", "hail": "hail",
    "theft": "theft_recovery", "stolen": "theft_recovery",
    "flood": "flood", "water": "flood", "burn": "fire", "fire": "fire",
    "frame": "frame", "undercarriage": "frame", "mechanical": "other",
    "all over": "moderate_collision", "normal wear": "minor_collision",
}


def _pick(raw: dict, keys: list):
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _normalize(raw: dict) -> dict:
    """Map a provider payload onto our car schema. Defensive by design."""
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return {}
    # unwrap common envelope keys
    for wrap in ("data", "result", "lot", "vehicle", "car"):
        if isinstance(raw.get(wrap), dict):
            raw = raw[wrap]

    out = {}
    for field, keys in _FIELD_MAP.items():
        val = _pick(raw, keys)
        if val is None:
            continue
        out[field] = val

    # normalize a few values
    if "source" in out:
        s = str(out["source"]).lower()
        out["source"] = "iaai" if "iaa" in s else "copart"
    if "title_type" in out:
        out["title_type"] = "clean" if "clean" in str(out["title_type"]).lower() else "salvage"
    if "damage_type" in out:
        d = str(out["damage_type"]).lower()
        out["damage_type"] = next((v for k, v in _DAMAGE_ALIASES.items() if k in d), "other")
    if "run_drive" in out and not isinstance(out["run_drive"], bool):
        out["run_drive"] = "run" in str(out["run_drive"]).lower()
    if "year" in out:
        try:
            out["year"] = int(out["year"])
        except (ValueError, TypeError):
            out.pop("year")
    if "current_bid" in out:
        try:
            out["current_bid"] = float(str(out["current_bid"]).replace("$", "").replace(",", ""))
        except (ValueError, TypeError):
            out.pop("current_bid")
    return out


async def lookup(vin: str | None = None, lot: str | None = None,
                 source: str = "copart") -> dict:
    """Returns {"found": bool, "car": {...}, "provider": str, "error": str|None}"""
    if PROVIDER == "demo" or not API_KEY:
        q_vin = (vin or "").strip().upper()
        q_lot = (lot or "").strip()
        for demo in DEMO_LOTS:
            if (q_vin and demo["vin"].upper() == q_vin) or \
               (q_lot and demo["lot_number"] == q_lot):
                return {"found": True, "car": dict(demo), "provider": "demo", "error": None}
        return {"found": False, "car": None, "provider": "demo",
                "error": "Not in demo data. Demo lots: 58214036, 31877421, 77120988. "
                         "Set AUCTION_API_KEY in .env for live lookups."}

    if vin:
        path = VIN_PATH.format(vin=vin.strip())
    elif lot:
        path = LOT_PATH.format(source=source, lot=lot.strip())
    else:
        return {"found": False, "car": None, "provider": "live",
                "error": "Provide a VIN or lot number."}

    headers = {}
    if KEY_HEADER.lower() == "authorization":
        headers["Authorization"] = f"Bearer {API_KEY}"
    else:
        headers[KEY_HEADER] = API_KEY

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(BASE_URL + path, headers=headers)
        if resp.status_code == 404:
            return {"found": False, "car": None, "provider": "live",
                    "error": "Lot/VIN not found at provider."}
        resp.raise_for_status()
        car = _normalize(resp.json())
        if not car:
            return {"found": False, "car": None, "provider": "live",
                    "error": "Provider responded but no fields could be mapped — "
                             "check _normalize() against the provider docs."}
        return {"found": True, "car": car, "provider": "live", "error": None}
    except httpx.HTTPStatusError as e:
        return {"found": False, "car": None, "provider": "live",
                "error": f"Provider returned HTTP {e.response.status_code}. "
                         "Check AUCTION_API_* settings in .env."}
    except httpx.HTTPError as e:
        return {"found": False, "car": None, "provider": "live",
                "error": f"Request failed: {e}"}
