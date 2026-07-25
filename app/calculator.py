"""
Max-bid calculator: fee stack + hidden-cost contingencies + the 40-60% rebuild rule.

Core rule: total invested (bid + fees + transport + repairs + contingencies)
should stay within target_ratio (default 50%, range 40-60%) of clean-title
market value. Rebuilt-title resale is estimated at rebuilt_factor (default 70%)
of clean value, since rebuilt titles sell 20-50% below clean.
"""
import json

from .fees import CONTINGENCY_COSTS, DAMAGE_GUIDANCE, fee_breakdown

BID_STEP = 25  # search granularity for max bid


def contingency_breakdown(flags: dict) -> dict:
    items = []
    for key, cfg in CONTINGENCY_COSTS.items():
        if flags.get(key):
            items.append((cfg["label"], float(cfg["amount"])))
    return {"items": items, "total": round(sum(a for _, a in items), 2)}


def all_in_cost(platform: str, bid: float, title_type: str,
                transport: float, repair: float, cont_total: float) -> dict:
    fees = fee_breakdown(platform, bid, title_type)
    total = round(bid + fees["total"] + transport + repair + cont_total, 2)
    return {"bid": bid, "fees": fees, "transport": transport, "repair": repair,
            "contingencies": cont_total, "total": total}


def find_max_bid(platform: str, title_type: str, ceiling: float,
                 transport: float, repair: float, cont_total: float) -> float:
    """Largest bid (in $25 steps) keeping all-in cost <= ceiling."""
    fixed = transport + repair + cont_total
    if ceiling <= fixed:
        return 0.0
    lo, hi = 0, int(ceiling)  # bid can never exceed the ceiling itself
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        bid = mid - (mid % BID_STEP)
        cost = bid + fee_breakdown(platform, bid, title_type)["total"] + fixed
        if cost <= ceiling:
            best = bid
            lo = mid + BID_STEP if mid % BID_STEP == 0 else (mid // BID_STEP + 1) * BID_STEP
        else:
            hi = mid - 1
    return float(best)


def car_metrics(car: dict) -> dict:
    """Full economics for a tracked car. Returns None-safe dict for the UI."""
    platform = car.get("platform") or "copart_public"
    title_type = car.get("title_type") or "salvage"
    clean_value = float(car.get("clean_value") or 0)
    repair = float(car.get("repair_estimate") or 0)
    transport = float(car.get("transport_estimate") or 0)
    target_ratio = float(car.get("target_ratio") or 0.5)
    rebuilt_factor = float(car.get("rebuilt_factor") or 0.7)
    planned_bid = float(car.get("planned_bid") or car.get("current_bid") or 0)

    flags = car.get("contingencies") or {}
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except (ValueError, TypeError):
            flags = {}
    cont = contingency_breakdown(flags)

    damage = DAMAGE_GUIDANCE.get(car.get("damage_type") or "other",
                                 DAMAGE_GUIDANCE["other"])

    result = {
        "contingency": cont,
        "damage_warning": damage["warning"],
        "suggested_ratio": damage["suggested_ratio"],
        "max_bid": None, "budget_ceiling": None, "rebuilt_resale": None,
        "all_in": None, "margin": None, "margin_pct": None, "verdict": None,
    }
    if clean_value <= 0:
        result["verdict"] = "set_clean_value"
        return result

    ceiling = round(clean_value * target_ratio, 2)
    rebuilt_resale = round(clean_value * rebuilt_factor, 2)
    max_bid = find_max_bid(platform, title_type, ceiling, transport, repair,
                           cont["total"])
    result.update({"budget_ceiling": ceiling, "rebuilt_resale": rebuilt_resale,
                   "max_bid": max_bid})

    if planned_bid > 0:
        cost = all_in_cost(platform, planned_bid, title_type, transport,
                           repair, cont["total"])
        margin = round(rebuilt_resale - cost["total"], 2)
        result["all_in"] = cost
        result["margin"] = margin
        result["margin_pct"] = round(margin / cost["total"] * 100, 1) if cost["total"] else None
        invested_ratio = cost["total"] / clean_value
        if margin <= 0:
            result["verdict"] = "losing"
        elif invested_ratio <= target_ratio:
            result["verdict"] = "strong"
        elif invested_ratio <= 0.60:
            result["verdict"] = "marginal"
        else:
            result["verdict"] = "over_budget"
    else:
        result["verdict"] = "no_bid_set"
    return result
