"""
Car Bid Tracker — FastAPI backend.
Run:  uvicorn main:app --reload   then open http://127.0.0.1:8000
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before app.auction_api reads env

import asyncio  # noqa: E402

from fastapi import FastAPI, File, HTTPException, Query, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import apibara, auction_api, copart_csv, db, scanner  # noqa: E402
from app.calculator import all_in_cost, car_metrics, contingency_breakdown, find_max_bid  # noqa: E402
from app.fees import CONTINGENCY_COSTS, DAMAGE_GUIDANCE, PLATFORMS, fee_breakdown  # noqa: E402

app = FastAPI(title="Car Bid Tracker")
db.init_db()


@app.on_event("startup")
async def _start_poller():
    if scanner.is_configured():
        asyncio.create_task(scanner.poller_loop())

STATIC_DIR = Path(__file__).resolve().parent / "static"


class CarPayload(BaseModel):
    lot_number: str | None = None
    vin: str | None = None
    source: str | None = None
    platform: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    title_type: str | None = None
    damage_type: str | None = None
    run_drive: bool | None = None
    auction_date: str | None = None
    location: str | None = None
    listing_url: str | None = None
    current_bid: float | None = None
    planned_bid: float | None = None
    clean_value: float | None = None
    repair_estimate: float | None = None
    transport_estimate: float | None = None
    contingencies: dict | None = None
    target_ratio: float | None = None
    rebuilt_factor: float | None = None
    status: str | None = None
    notes: str | None = None
    miles: float | None = None
    is_new: bool | None = None


class SearchPayload(BaseModel):
    name: str | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    price_max: float | None = None
    miles_max: float | None = None
    zip: str | None = None
    radius: int | None = None
    state: str | None = None
    active: bool | None = None


class QuickCalcPayload(BaseModel):
    platform: str = "copart_public"
    title_type: str = "salvage"
    bid: float = 0
    clean_value: float = 0
    repair_estimate: float = 0
    transport_estimate: float = 0
    contingencies: dict = {}
    target_ratio: float = 0.5
    rebuilt_factor: float = 0.7
    damage_type: str = "other"


def _with_metrics(car: dict) -> dict:
    car["metrics"] = car_metrics(car)
    return car


@app.get("/api/meta")
def meta():
    return {
        "platforms": PLATFORMS,
        "damage_types": {k: v["label"] for k, v in DAMAGE_GUIDANCE.items()},
        "contingencies": CONTINGENCY_COSTS,
        "statuses": list(db.VALID_STATUSES),
        "provider": auction_api.PROVIDER if auction_api.API_KEY or auction_api.PROVIDER == "demo" else "demo",
        "scanner_configured": scanner.is_configured(),
        "discovery_source": scanner.discovery_source(),
        "poll_hours": scanner.POLL_INTERVAL_HOURS,
        "smtp_configured": scanner.smtp_configured(),
    }


@app.get("/api/cars")
def cars_list(status: str | None = Query(default=None)):
    return [_with_metrics(c) for c in db.list_cars(status)]


@app.post("/api/cars")
def cars_create(payload: CarPayload):
    return _with_metrics(db.create_car(payload.model_dump(exclude_none=True)))


@app.patch("/api/cars/{car_id}")
def cars_update(car_id: int, payload: CarPayload):
    car = db.update_car(car_id, payload.model_dump(exclude_none=True))
    if not car:
        raise HTTPException(404, "Car not found")
    return _with_metrics(car)


@app.delete("/api/cars/{car_id}")
def cars_delete(car_id: int):
    if not db.delete_car(car_id):
        raise HTTPException(404, "Car not found")
    return {"deleted": car_id}


@app.get("/api/ranking")
def ranking():
    """Cars ranked by value: margin % desc; unpriced cars sink to the bottom."""
    cars = [_with_metrics(c) for c in db.list_cars()
            if c["status"] not in ("lost", "archived")]
    def key(c):
        mp = c["metrics"].get("margin_pct")
        return (0, -mp) if mp is not None else (1, 0)
    return sorted(cars, key=key)


@app.post("/api/calc")
def quick_calc(p: QuickCalcPayload):
    """Ad-hoc calculator without saving a car."""
    cont = contingency_breakdown(p.contingencies)
    ceiling = round(p.clean_value * p.target_ratio, 2) if p.clean_value else None
    result = {
        "fees": fee_breakdown(p.platform, p.bid, p.title_type) if p.bid else None,
        "contingency": cont,
        "budget_ceiling": ceiling,
        "rebuilt_resale": round(p.clean_value * p.rebuilt_factor, 2) if p.clean_value else None,
        "damage_warning": DAMAGE_GUIDANCE.get(p.damage_type, DAMAGE_GUIDANCE["other"])["warning"],
        "max_bid": None, "all_in": None, "margin": None, "margin_pct": None,
    }
    if ceiling:
        result["max_bid"] = find_max_bid(p.platform, p.title_type, ceiling,
                                         p.transport_estimate, p.repair_estimate,
                                         cont["total"])
    if p.bid:
        cost = all_in_cost(p.platform, p.bid, p.title_type,
                           p.transport_estimate, p.repair_estimate, cont["total"])
        result["all_in"] = cost
        if result["rebuilt_resale"]:
            result["margin"] = round(result["rebuilt_resale"] - cost["total"], 2)
            result["margin_pct"] = round(result["margin"] / cost["total"] * 100, 1)
    return result


@app.get("/api/lookup")
async def lookup(vin: str | None = None, lot: str | None = None,
                 source: str = "copart"):
    if not vin and not lot:
        raise HTTPException(400, "Provide ?vin= or ?lot=")
    # Apibara first — real Copart/IAAI, accepts VIN or lot number
    if apibara.is_configured():
        return await apibara.lookup(vin=vin or "", lot=lot or "")
    # MarketCheck VIN lookup fallback
    if vin and scanner.is_configured():
        try:
            data = await scanner._fetch({"api_key": scanner.MC_API_KEY,
                                         "vin": vin.strip(), "rows": 1})
            listings = data.get("listings") or []
            if listings:
                car = scanner.normalize_listing(listings[0], 0)
                car.pop("search_id", None)
                car.pop("is_new", None)
                car.pop("status", None)
                car.pop("notes", None)
                return {"found": True, "car": car, "provider": "marketcheck",
                        "error": None}
            return {"found": False, "car": None, "provider": "marketcheck",
                    "error": "VIN not found in active auction listings."}
        except Exception as e:  # noqa: BLE001 - fall back to demo lookup
            return {"found": False, "car": None, "provider": "marketcheck",
                    "error": f"MarketCheck lookup failed: {e}"}
    return await auction_api.lookup(vin=vin, lot=lot, source=source)


# ---------------------------------------------------------------------------
# Saved searches (scanner)
# ---------------------------------------------------------------------------

@app.get("/api/searches")
def searches_list():
    return db.list_searches()


@app.post("/api/searches")
def searches_create(payload: SearchPayload):
    return db.create_search(payload.model_dump(exclude_none=True))


@app.patch("/api/searches/{search_id}")
def searches_update(search_id: int, payload: SearchPayload):
    s = db.update_search(search_id, payload.model_dump(exclude_none=True))
    if not s:
        raise HTTPException(404, "Search not found")
    return s


@app.delete("/api/searches/{search_id}")
def searches_delete(search_id: int):
    if not db.delete_search(search_id):
        raise HTTPException(404, "Search not found")
    return {"deleted": search_id}


@app.post("/api/searches/run")
async def searches_run_all():
    if not scanner.is_configured():
        raise HTTPException(400, "Scanner not configured — set MARKETCHECK_API_KEY in .env")
    results = await scanner.run_all()
    return {"results": [{**r, "added": len(r["added"])} for r in results]}


@app.post("/api/searches/{search_id}/run")
async def searches_run_one(search_id: int):
    if not scanner.is_configured():
        raise HTTPException(400, "Scanner not configured — set MARKETCHECK_API_KEY in .env")
    s = db.get_search(search_id)
    if not s:
        raise HTTPException(404, "Search not found")
    r = await scanner.run_search(s)
    if r["added"]:
        scanner.send_email_alert(r["added"])
    return {**r, "added": len(r["added"])}


@app.post("/api/import/copart")
async def import_copart(file: UploadFile = File(...)):
    """Import an official Copart member sales-list CSV and match saved searches."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    result = copart_csv.import_csv(content)
    return result


@app.post("/api/cars/{car_id}/fetch_value")
async def fetch_value(car_id: int):
    """Auto-fill clean-title value from MarketCheck dealer comps (1-2 calls)."""
    car = db.get_car(car_id)
    if not car:
        raise HTTPException(404, "Car not found")
    r = await scanner.fetch_clean_value(car)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    car = db.update_car(car_id, {"clean_value": r["value"]})
    return {"ok": True, "value": r["value"], "count": r["count"],
            "basis": r["basis"], "car": _with_metrics(car)}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
