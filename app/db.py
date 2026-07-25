"""SQLite storage for tracked cars (stdlib sqlite3, no ORM)."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("TRACKER_DB",
                         Path(__file__).resolve().parent.parent / "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_number TEXT DEFAULT '',
    vin TEXT DEFAULT '',
    source TEXT DEFAULT 'copart',            -- copart | iaai
    platform TEXT DEFAULT 'copart_public',   -- fee route, see fees.PLATFORMS
    year INTEGER,
    make TEXT DEFAULT '',
    model TEXT DEFAULT '',
    trim TEXT DEFAULT '',
    title_type TEXT DEFAULT 'salvage',       -- clean | salvage
    damage_type TEXT DEFAULT 'other',
    run_drive INTEGER DEFAULT 1,
    auction_date TEXT DEFAULT '',            -- ISO datetime
    location TEXT DEFAULT '',
    listing_url TEXT DEFAULT '',
    current_bid REAL DEFAULT 0,
    planned_bid REAL DEFAULT 0,
    clean_value REAL DEFAULT 0,
    repair_estimate REAL DEFAULT 0,
    transport_estimate REAL DEFAULT 0,
    contingencies TEXT DEFAULT '{}',         -- JSON flags
    target_ratio REAL DEFAULT 0.5,
    rebuilt_factor REAL DEFAULT 0.7,
    status TEXT DEFAULT 'watching',          -- watching | bidding | won | lost | archived
    notes TEXT DEFAULT '',
    miles REAL,
    search_id INTEGER,
    mc_listing_id TEXT DEFAULT '',
    is_new INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    make TEXT DEFAULT '',
    model TEXT DEFAULT '',
    trim TEXT DEFAULT '',
    year_min INTEGER,
    year_max INTEGER,
    price_max REAL,
    miles_max REAL,
    zip TEXT DEFAULT '',
    radius INTEGER DEFAULT 100,
    state TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    last_run_at TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    last_num_found INTEGER,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS seen_listings (
    listing_id TEXT PRIMARY KEY,
    search_id INTEGER,
    seen_at TEXT
);
"""

# columns added after the first release — applied to old DBs on startup
_CAR_MIGRATIONS = {
    "miles": "REAL",
    "search_id": "INTEGER",
    "mc_listing_id": "TEXT DEFAULT ''",
    "is_new": "INTEGER DEFAULT 0",
}

CAR_FIELDS = [
    "lot_number", "vin", "source", "platform", "year", "make", "model", "trim",
    "title_type", "damage_type", "run_drive", "auction_date", "location",
    "listing_url", "current_bid", "planned_bid", "clean_value",
    "repair_estimate", "transport_estimate", "contingencies", "target_ratio",
    "rebuilt_factor", "status", "notes",
    "miles", "search_id", "mc_listing_id", "is_new",
]

SEARCH_FIELDS = [
    "name", "make", "model", "trim", "year_min", "year_max", "price_max",
    "miles_max", "zip", "radius", "state", "active",
]

VALID_STATUSES = ("watching", "bidding", "won", "lost", "archived")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cars)")}
        for col, ddl in _CAR_MIGRATIONS.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE cars ADD COLUMN {col} {ddl}")
        conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["contingencies"] = json.loads(d.get("contingencies") or "{}")
    except (ValueError, TypeError):
        d["contingencies"] = {}
    d["run_drive"] = bool(d.get("run_drive"))
    return d


def list_cars(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM cars"
    args = []
    if status:
        q += " WHERE status = ?"
        args.append(status)
    q += " ORDER BY CASE WHEN auction_date = '' THEN 1 ELSE 0 END, auction_date ASC, id DESC"
    with _connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(q, args).fetchall()]


def get_car(car_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _clean_payload(data: dict) -> dict:
    out = {}
    for k in CAR_FIELDS:
        if k not in data or data[k] is None:
            continue
        v = data[k]
        if k == "contingencies" and not isinstance(v, str):
            v = json.dumps(v)
        if k in ("run_drive", "is_new"):
            v = 1 if v else 0
        if k == "status" and v not in VALID_STATUSES:
            continue
        out[k] = v
    return out


def create_car(data: dict) -> dict:
    payload = _clean_payload(data)
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    with _connect() as conn:
        cur = conn.execute(f"INSERT INTO cars ({cols}) VALUES ({marks})",
                           list(payload.values()))
        conn.commit()
        return get_car(cur.lastrowid)


def update_car(car_id: int, data: dict) -> dict | None:
    payload = _clean_payload(data)
    if not payload:
        return get_car(car_id)
    payload["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in payload)
    with _connect() as conn:
        conn.execute(f"UPDATE cars SET {sets} WHERE id = ?",
                     [*payload.values(), car_id])
        conn.commit()
    return get_car(car_id)


def delete_car(car_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM cars WHERE id = ?", (car_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Saved searches (scanner)
# ---------------------------------------------------------------------------

def list_searches(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM saved_searches"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY id DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def get_search(search_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM saved_searches WHERE id = ?",
                           (search_id,)).fetchone()
    return dict(row) if row else None


def _clean_search_payload(data: dict) -> dict:
    out = {}
    for k in SEARCH_FIELDS:
        if k not in data or data[k] is None:
            continue
        v = data[k]
        if k == "active":
            v = 1 if v else 0
        out[k] = v
    return out


def create_search(data: dict) -> dict:
    payload = _clean_search_payload(data)
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO saved_searches ({cols}) VALUES ({marks})",
            list(payload.values()))
        conn.commit()
        return get_search(cur.lastrowid)


def update_search(search_id: int, data: dict) -> dict | None:
    payload = _clean_search_payload(data)
    if not payload:
        return get_search(search_id)
    payload["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in payload)
    with _connect() as conn:
        conn.execute(f"UPDATE saved_searches SET {sets} WHERE id = ?",
                     [*payload.values(), search_id])
        conn.commit()
    return get_search(search_id)


def delete_search(search_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        conn.commit()
        return cur.rowcount > 0


def update_search_run(search_id: int, num_found: int, error: str | None):
    with _connect() as conn:
        conn.execute(
            "UPDATE saved_searches SET last_run_at = ?, last_num_found = ?, "
            "last_error = ? WHERE id = ?",
            (_now(), num_found, error or "", search_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Seen-listing dedup
# ---------------------------------------------------------------------------

def listing_seen(listing_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM seen_listings WHERE listing_id = ?",
                            (listing_id,)).fetchone() is not None


def mark_listing_seen(listing_id: str, search_id: int):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_listings (listing_id, search_id, seen_at) "
            "VALUES (?, ?, ?)", (listing_id, search_id, _now()))
        conn.commit()


def vin_tracked(vin: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM cars WHERE vin = ? AND vin != ''",
                            (vin,)).fetchone() is not None
