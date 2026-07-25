"""
Copart member CSV importer.

Copart members can download official sales-list CSVs (first-party data:
lot #, sale date/time, damage, title, run/drive, est. retail value, repair
cost, current bid). This module parses those CSVs defensively (column names
drift over time), matches rows against active saved searches, and adds
matches to the watchlist.

No scraping — the user downloads the file from their own Copart account:
https://www.copart.com/content/us/en/buyer/sales/download-sales-data
"""
import csv
import io
import re

from . import db

# normalized header -> our field (header normalization: lowercase, alnum only)
_HEADER_CANDIDATES = {
    "lot_number": ["lotnumber", "lot"],
    "vin": ["vin"],
    "year": ["year", "lotyear"],
    "make": ["make", "lotmake"],
    "model": ["modeldetail", "model", "lotmodel", "modelgroup"],
    "body_style": ["bodystyle", "body"],
    "color": ["color"],
    "damage": ["damagedescription", "primarydamage", "damage"],
    "damage2": ["secondarydamage"],
    "title": ["saletitletype", "titletype", "saledocument", "title"],
    "runs": ["runsdrives", "runanddrive", "runsanddrives"],
    "sale_date": ["saledatemdcy", "saledate", "auctiondate"],
    "sale_time": ["saletimehhmm", "saletime"],
    "odometer": ["odometer", "odometerreading", "miles"],
    "retail_value": ["estretailvalue", "estimatedretailvalue", "acv"],
    "repair_cost": ["repaircost", "estrepaircost"],
    "high_bid": ["highbidnonvixsealedvix", "highbidnonvix", "highbid",
                 "currentbid", "currenthighbid"],
    "buy_it_now": ["buyitnowprice", "buyitnow"],
    "city": ["locationcity", "yardcity"],
    "state": ["locationstate", "yardstate"],
    "zip": ["locationzip", "yardzip"],
    "keys": ["haskeysyesorno", "haskeys", "keys"],
    "yard_name": ["yardname", "location"],
}

_DAMAGE_MAP = [
    (("water", "flood"), "flood"),
    (("burn", "fire"), "fire"),
    (("theft", "stripped", "missing/altered"), "theft_recovery"),
    (("hail",), "hail"),
    (("frame", "undercarriage", "rollover", "roll over"), "frame"),
    (("minor dent", "scratch", "normal wear", "vandalism", "cosmetic"), "minor_collision"),
    (("front end", "rear end", "side", "all over", "top/roof"), "moderate_collision"),
]


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _num(v) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v)) or 0)
    except ValueError:
        return 0.0


def _map_damage(primary: str, secondary: str = "") -> str:
    text = f"{primary} {secondary}".lower()
    for needles, code in _DAMAGE_MAP:
        if any(n in text for n in needles):
            return code
    return "other"


def _map_title(title: str) -> str:
    t = (title or "").lower()
    if "salv" in t or "certificate of destruction" in t or "non-repair" in t:
        return "salvage"
    if "clean" in t or t.strip() in ("certificate of title", "clear"):
        return "clean"
    return "salvage"


def _iso_date(mdY: str, hhmm: str = "") -> str:
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", (mdY or "").strip())
    if not m:
        return ""
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    t = re.sub(r"\D", "", hhmm or "")
    hh, mi = (int(t[:-2] or 0), int(t[-2:] or 0)) if t else (0, 0)
    if hh > 23 or mi > 59:
        hh, mi = 0, 0
    return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mi:02d}:00"


def parse_csv(content: bytes) -> list[dict]:
    """Parse a Copart sales CSV into normalized row dicts."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    # locate header row (first row containing a lot-number-ish column)
    header_idx = 0
    for i, r in enumerate(rows[:10]):
        normed = {_norm_header(c) for c in r}
        if normed & {"lotnumber", "vin"}:
            header_idx = i
            break
    header = rows[header_idx]
    col_of = {}
    for field, candidates in _HEADER_CANDIDATES.items():
        for j, h in enumerate(header):
            if _norm_header(h) in candidates:
                col_of[field] = j
                break
    out = []
    for r in rows[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        def g(f):
            j = col_of.get(f)
            return r[j].strip() if j is not None and j < len(r) else ""
        row = {f: g(f) for f in _HEADER_CANDIDATES}
        if not row["lot_number"] and not row["vin"]:
            continue
        out.append(row)
    return out


def _row_tokens(row: dict) -> str:
    return f"{row['make']} {row['model']} {row['body_style']}".lower()


def match_row(row: dict, s: dict) -> bool:
    if s.get("make") and s["make"].lower() not in (row["make"] or "").lower():
        return False
    if s.get("model"):
        hay = _row_tokens(row)
        tokens = re.findall(r"[a-z0-9]+", s["model"].lower())
        if not all(t in hay for t in tokens):
            return False
    year = int(_num(row["year"]))
    if s.get("year_min") and year and year < s["year_min"]:
        return False
    if s.get("year_max") and year and year > s["year_max"]:
        return False
    if s.get("miles_max"):
        odo = _num(row["odometer"])
        if odo and odo > s["miles_max"]:
            return False
    if s.get("price_max"):
        bid = _num(row["high_bid"])
        if bid and bid > s["price_max"]:
            return False
    if s.get("state") and (row["state"] or "").upper() != s["state"].upper():
        return False
    return True


def row_to_car(row: dict, search_id: int) -> dict:
    runs = "run" in (row["runs"] or "").lower()
    notes_bits = [b for b in (
        f"Damage: {row['damage']}" if row["damage"] else "",
        f"Secondary: {row['damage2']}" if row["damage2"] else "",
        f"Keys: {row['keys']}" if row["keys"] else "",
        f"Title: {row['title']}" if row["title"] else "",
        "Imported from Copart CSV.",
    ) if b]
    return {
        "mc_listing_id": f"copart:{row['lot_number']}",
        "search_id": search_id,
        "lot_number": row["lot_number"],
        "vin": row["vin"],
        "source": "copart",
        "platform": "copart_public",
        "year": int(_num(row["year"])) or None,
        "make": (row["make"] or "").title(),
        "model": row["model"],
        "trim": row["body_style"],
        "miles": _num(row["odometer"]) or None,
        "title_type": _map_title(row["title"]),
        "damage_type": _map_damage(row["damage"], row["damage2"]),
        "run_drive": runs,
        "auction_date": _iso_date(row["sale_date"], row["sale_time"]),
        "location": ", ".join(x for x in (row["city"].title() if row["city"] else "",
                                          row["state"].upper()) if x),
        "listing_url": f"https://www.copart.com/lot/{row['lot_number']}"
                       if row["lot_number"] else "",
        "current_bid": _num(row["high_bid"]),
        "clean_value": _num(row["retail_value"]),      # Copart's est. retail — starting point
        "repair_estimate": _num(row["repair_cost"]),   # Copart's estimate — refine it
        "status": "watching",
        "is_new": 1,
        "notes": " | ".join(notes_bits),
    }


def import_csv(content: bytes) -> dict:
    """Parse, match against active saved searches, add new matches."""
    rows = parse_csv(content)
    searches = db.list_searches(active_only=True)
    result = {"rows_parsed": len(rows), "searches": len(searches),
              "matched": 0, "added": 0, "skipped_seen": 0, "added_cars": []}
    if not searches:
        result["error"] = "No active saved searches — create one in the Scanner tab first."
        return result
    for row in rows:
        hit = next((s for s in searches if match_row(row, s)), None)
        if not hit:
            continue
        result["matched"] += 1
        key = f"copart:{row['lot_number'] or row['vin']}"
        if db.listing_seen(key):
            result["skipped_seen"] += 1
            continue
        if row["vin"] and db.vin_tracked(row["vin"]):
            db.mark_listing_seen(key, hit["id"])
            result["skipped_seen"] += 1
            continue
        car = row_to_car(row, hit["id"])
        created = db.create_car(car)
        db.mark_listing_seen(key, hit["id"])
        result["added"] += 1
        result["added_cars"].append(
            f"#{created['id']} {created['year']} {created['make']} {created['model']}")
    return result
