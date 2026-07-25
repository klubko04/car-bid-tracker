"""
MarketCheck auction scanner: saved searches -> poll -> new matches -> watchlist + email.

Endpoint (verified from docs.marketcheck.com):
  GET https://api.marketcheck.com/v2/search/car/auction/active
  Auth: api_key query parameter.
  Auction data refreshes DAILY by 11:00 UTC -> polling more than twice a day
  is wasted quota. Default interval: 12h.

Free plan budget: 500 calls/month. One search polled every 12h = ~60 calls/mo.

Env:
  MARKETCHECK_API_KEY   (required for live scanning)
  MARKETCHECK_BASE_URL  (default https://api.marketcheck.com/v2)
  POLL_INTERVAL_HOURS   (default 12)
  MARKETCHECK_MOCK=1    (testing only: canned responses, no HTTP)
  SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS  (optional email alerts)
  ALERT_EMAIL_TO / ALERT_EMAIL_FROM
"""
import asyncio
import os
import smtplib
from email.message import EmailMessage

import httpx

from . import apibara, db

MC_API_KEY = os.getenv("MARKETCHECK_API_KEY", "")
MC_BASE = os.getenv("MARKETCHECK_BASE_URL", "https://api.marketcheck.com/v2").rstrip("/")
MC_MOCK = os.getenv("MARKETCHECK_MOCK", "") == "1"
POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "12"))

_MOCK_RESPONSE = {
    "num_found": 2,
    "listings": [
        {"id": "MOCK-LEXUS-1", "vin": "JTHCE1D20F5000001", "price": 6200, "miles": 88500,
         "heading": "2016 Lexus IS 300 AWD", "vdp_url": "https://example.com/lot/1",
         "source": "copart.com", "stock_no": "58330101", "dom": 3,
         "build": {"year": 2016, "make": "Lexus", "model": "IS 300", "trim": "AWD"},
         "dealer": {"city": "Portland", "state": "OR"}},
        {"id": "MOCK-LEXUS-2", "vin": "JTHCE1D25G5000002", "price": 5100, "miles": 104000,
         "heading": "2016 Lexus IS 300 Base", "vdp_url": "https://example.com/lot/2",
         "source": "iaai.com", "stock_no": "31990244", "dom": 1,
         "build": {"year": 2016, "make": "Lexus", "model": "IS 300", "trim": "Base"},
         "dealer": {"city": "Seattle", "state": "WA"}},
    ],
}


def is_configured() -> bool:
    """Scanner runs if any discovery source is set (Apibara preferred)."""
    return apibara.is_configured() or bool(MC_API_KEY) or MC_MOCK


def discovery_source() -> str:
    return "apibara" if apibara.is_configured() else (
        "marketcheck" if (MC_API_KEY or MC_MOCK) else "none")


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("ALERT_EMAIL_TO"))


def _build_params(s: dict) -> dict:
    p = {"api_key": MC_API_KEY, "rows": 50,
         "sort_by": "first_seen", "sort_order": "desc"}
    if s.get("make"):
        p["make"] = s["make"]
    if s.get("model"):
        p["model"] = s["model"]
    if s.get("trim"):
        p["trim"] = s["trim"]
    if s.get("year_min") or s.get("year_max"):
        p["year_range"] = f"{s.get('year_min') or 1981}-{s.get('year_max') or 2030}"
    if s.get("price_max"):
        p["price_range"] = f"1-{int(s['price_max'])}"
    if s.get("miles_max"):
        p["miles_range"] = f"0-{int(s['miles_max'])}"
    if s.get("zip"):
        p["zip"] = s["zip"]
        p["radius"] = int(s.get("radius") or 100)
    elif s.get("state"):
        p["state"] = s["state"]
    return p


async def _fetch(params: dict) -> dict:
    if MC_MOCK:
        return _MOCK_RESPONSE
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{MC_BASE}/search/car/auction/active", params=params)
    resp.raise_for_status()
    return resp.json()


def _platform_for_source(source: str) -> str:
    s = (source or "").lower()
    if "iaa" in s:
        return "iaai_public"
    return "copart_public"


def normalize_listing(l: dict, search_id: int) -> dict:
    build = l.get("build") or {}
    dealer = l.get("dealer") or {}
    source = l.get("source") or "auction"
    loc = ", ".join(x for x in (dealer.get("city"), dealer.get("state")) if x)
    return {
        "mc_listing_id": str(l.get("id") or ""),
        "search_id": search_id,
        "vin": l.get("vin") or "",
        "lot_number": str(l.get("stock_no") or ""),
        "source": "iaai" if "iaa" in source.lower() else "copart",
        "platform": _platform_for_source(source),
        "year": build.get("year"),
        "make": build.get("make") or "",
        "model": build.get("model") or "",
        "trim": build.get("trim") or "",
        "miles": l.get("miles"),
        "title_type": "salvage",
        "damage_type": "other",
        "location": loc,
        "listing_url": l.get("vdp_url") or "",
        "current_bid": float(l.get("price") or 0),
        "status": "watching",
        "is_new": 1,
        "notes": f"Auto-added by scanner (src: {source}, "
                 f"{l.get('dom', '?')} days on market). Verify damage & title on the listing.",
    }


async def _discover(s: dict) -> dict:
    """Return {cars: [normalized car dicts], num_found, error} from the active source."""
    if apibara.is_configured():
        return await apibara.search(s)
    # MarketCheck fallback
    try:
        data = await _fetch(_build_params(s))
        cars = [normalize_listing(l, s["id"]) for l in (data.get("listings") or [])]
        return {"cars": cars, "num_found": int(data.get("num_found") or 0), "error": None}
    except httpx.HTTPStatusError as e:
        return {"cars": [], "num_found": 0,
                "error": f"MarketCheck HTTP {e.response.status_code}: {e.response.text[:150]}"}
    except httpx.HTTPError as e:
        return {"cars": [], "num_found": 0, "error": f"Request failed: {e}"}


async def run_search(s: dict) -> dict:
    """Run one saved search; add unseen listings to the watchlist."""
    result = {"search_id": s["id"], "name": s.get("name") or "", "added": [],
              "num_found": 0, "error": None}
    found = await _discover(s)
    result["num_found"] = found["num_found"]
    result["error"] = found["error"]
    for car in found["cars"]:
        lid = car.get("mc_listing_id") or ""
        if not lid or db.listing_seen(lid):
            continue
        if car.get("vin") and db.vin_tracked(car["vin"]):
            db.mark_listing_seen(lid, s["id"])
            continue
        created = db.create_car(car)
        db.mark_listing_seen(lid, s["id"])
        result["added"].append(created)
    db.update_search_run(s["id"], result["num_found"], result["error"])
    return result


async def run_all() -> list[dict]:
    results = []
    for s in db.list_searches(active_only=True):
        results.append(await run_search(s))
    new_cars = [c for r in results for c in r["added"]]
    if new_cars:
        send_email_alert(new_cars)
    return results


def send_email_alert(new_cars: list[dict]) -> bool:
    """Plain-text email via SMTP. Silently skips when not configured."""
    if not smtp_configured():
        return False
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pw = os.getenv("SMTP_PASS", "")
    to = os.getenv("ALERT_EMAIL_TO")
    sender = os.getenv("ALERT_EMAIL_FROM", user or to)

    lines = []
    for c in new_cars:
        bid = f"${c['current_bid']:,.0f}" if c.get("current_bid") else "no price"
        miles = f"{c['miles']:,.0f} mi" if c.get("miles") else "miles n/a"
        lines.append(f"- {c.get('year','')} {c.get('make','')} {c.get('model','')} "
                     f"{c.get('trim','')} | {bid} | {miles} | {c.get('location','')}\n"
                     f"  {c.get('listing_url','')}")
    msg = EmailMessage()
    msg["Subject"] = f"Car Bid Tracker: {len(new_cars)} new auction match(es)"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("New auction matches on your saved searches:\n\n"
                    + "\n".join(lines)
                    + "\n\nOpen the tracker to set clean value & planned bid.")
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 - alerting must never crash the poller
        print(f"[scanner] email alert failed: {e}")
        return False


async def poller_loop():
    """Background loop: run all active searches every POLL_INTERVAL_HOURS."""
    await asyncio.sleep(5)  # let the app finish booting
    while True:
        try:
            results = await run_all()
            added = sum(len(r["added"]) for r in results)
            print(f"[scanner] polled {len(results)} search(es), {added} new match(es)")
        except Exception as e:  # noqa: BLE001 - poller must survive anything
            print(f"[scanner] poll cycle failed: {e}")
        await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)


async def fetch_clean_value(car: dict) -> dict:
    """
    Clean-title market value from MarketCheck DEALER comps (where their data
    is strong): median asking price of matching used listings nationwide.
    Costs 1 API call (2 if the trim is too narrow and we retry without it).
    """
    if MC_MOCK:
        return {"ok": True, "value": 18750.0, "count": 42, "basis": "median (mock)"}
    if not MC_API_KEY:
        return {"ok": False, "error": "MARKETCHECK_API_KEY not set in .env"}

    async def _stats_call(with_trim: bool) -> tuple[float | None, str, int]:
        params = {"api_key": MC_API_KEY, "car_type": "used",
                  "stats": "price", "rows": 0}
        if car.get("year"):
            params["year"] = str(car["year"])
        if car.get("make"):
            params["make"] = car["make"]
        if car.get("model"):
            params["model"] = car["model"]
        if with_trim and car.get("trim"):
            params["trim"] = car["trim"]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{MC_BASE}/search/car/active", params=params)
        resp.raise_for_status()
        data = resp.json()
        stats = ((data.get("stats") or {}).get("price")) or {}
        value = stats.get("median") or stats.get("mean")
        basis = "median" if stats.get("median") else "mean"
        return (float(value) if value else None, basis,
                int(data.get("num_found") or 0))

    try:
        value, basis, count = await _stats_call(with_trim=True)
        if value is None and car.get("trim"):
            value, basis, count = await _stats_call(with_trim=False)
            basis += ", trim ignored"
        if value is None:
            return {"ok": False,
                    "error": f"No priced dealer comps found ({count} listings)."}
        return {"ok": True, "value": round(value), "count": count, "basis": basis}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"MarketCheck HTTP {e.response.status_code}: "
                                      f"{e.response.text[:150]}"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Request failed: {e}"}
