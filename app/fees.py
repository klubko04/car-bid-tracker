"""
Fee schedules for US car auction platforms.
2026 estimates compiled from public fee pages and buyer research.
ALL NUMBERS ARE EDITABLE — update tiers here when platforms change fees.
"""

# ---------------------------------------------------------------------------
# COPART — public / non-licensed buyer, secured payment (wire/cashier's check)
# ---------------------------------------------------------------------------
# (max_bid_inclusive, fee)
COPART_BUYER_TIERS = [
    (399, 75), (899, 135), (1399, 185), (1999, 235), (2499, 285),
    (2999, 335), (3499, 385), (3999, 435), (4499, 485), (4999, 535),
]
COPART_PCT_ABOVE_5000 = 0.10          # ~10% of sale price above $5,000
COPART_VIRTUAL_BID_FEE = 99           # internet/live bid fee per vehicle
COPART_GATE_FEE = {"clean": 79, "salvage": 95}
COPART_ENVIRONMENTAL_FEE = 15
COPART_TITLE_MAIL_FEE = 20


def copart_buyer_fee(bid: float) -> float:
    if bid <= 0:
        return 0.0
    for cap, fee in COPART_BUYER_TIERS:
        if bid <= cap:
            return float(fee)
    return round(bid * COPART_PCT_ABOVE_5000, 2)


# ---------------------------------------------------------------------------
# IAAI — public (non-licensed) buyer. Public schedule is not published openly;
# estimate = Copart public tier + 1% of bid (IAAI runs ~2-5% higher overall).
# ---------------------------------------------------------------------------
IAAI_INTERNET_BID_TIERS = [
    (499, 29), (999, 39), (1999, 49), (3999, 59), (5999, 69), (7999, 79),
]
IAAI_INTERNET_BID_ABOVE = 89
IAAI_SERVICE_FEE = 95                 # pull-out / loading, per unit
IAAI_ENVIRONMENTAL_FEE = 15
IAAI_TITLE_FEE = 20


def iaai_buyer_fee(bid: float) -> float:
    return round(copart_buyer_fee(bid) + 0.01 * bid, 2)


def iaai_internet_fee(bid: float) -> float:
    for cap, fee in IAAI_INTERNET_BID_TIERS:
        if bid <= cap:
            return float(fee)
    return float(IAAI_INTERNET_BID_ABOVE)


# ---------------------------------------------------------------------------
# A BETTER BID — Copart broker. Pays Copart fees + broker stack.
# ---------------------------------------------------------------------------
ABB_BID_FEE_TIERS_LIVE = [            # (max_bid_inclusive, fee) — live bidding
    (99, 0), (499, 50), (999, 65), (1499, 85), (1999, 95),
    (3999, 110), (5999, 125), (7999, 145),
]
ABB_BID_FEE_ABOVE = 160
ABB_TRANSACTION_MIN = 299             # $299 or 9%, whichever is greater
ABB_TRANSACTION_PCT = 0.09
ABB_COPART_BROKER_FEE = 100
ABB_MAILING_FEE = 20


def abb_bid_fee(bid: float) -> float:
    for cap, fee in ABB_BID_FEE_TIERS_LIVE:
        if bid <= cap:
            return float(fee)
    return float(ABB_BID_FEE_ABOVE)


# ---------------------------------------------------------------------------
# AUTOBIDMASTER — Copart broker, membership tiers change the transaction fee.
# Annual membership (Advanced $189/yr, Premium $349/yr) NOT included per-car.
# ---------------------------------------------------------------------------
ABM_ADVANCED_TXN = (299, 0.05)        # $299 or 5%, whichever greater
ABM_PREMIUM_TXN = (250, 0.04)         # $250 or 4%, whichever greater
ABM_PROCESSING_FEE = 249              # website processing, $150-369 by location
ABM_COPART_BROKER_FEE = 100


def abm_transaction_fee(bid: float, tier: tuple) -> float:
    minimum, pct = tier
    return round(max(minimum, bid * pct), 2)


# ---------------------------------------------------------------------------
# Hidden-cost contingencies (defaults, editable)
# ---------------------------------------------------------------------------
CONTINGENCY_COSTS = {
    "inspection": {"label": "Pre-bid inspection", "amount": 150},
    "non_runner": {"label": "Non-runner winch/forklift surcharge", "amount": 150},
    "storage": {"label": "Storage-risk buffer", "amount": 100},
    "relocation": {"label": "Secondary tow / relocation buffer", "amount": 300},
    "rereg": {"label": "Salvage inspection & re-registration", "amount": 250},
}

# ---------------------------------------------------------------------------
# Damage-type guidance for the 40-60% rebuild rule
# ---------------------------------------------------------------------------
DAMAGE_GUIDANCE = {
    "theft_recovery":     {"label": "Recovered theft",     "suggested_ratio": 0.55, "warning": None},
    "hail":               {"label": "Hail",                "suggested_ratio": 0.55, "warning": None},
    "minor_collision":    {"label": "Minor collision",     "suggested_ratio": 0.55, "warning": None},
    "moderate_collision": {"label": "Moderate collision",  "suggested_ratio": 0.50, "warning": None},
    "frame":              {"label": "Frame / unibody",     "suggested_ratio": 0.40,
                           "warning": "Frame damage is often uneconomical to repair properly."},
    "flood":              {"label": "Flood",               "suggested_ratio": 0.30,
                           "warning": "Flood cars resell at 10-30% of clean value. Generally avoid."},
    "fire":               {"label": "Fire",                "suggested_ratio": 0.30,
                           "warning": "Fire damage fetches the lowest resale. Generally avoid."},
    "other":              {"label": "Other / unknown",     "suggested_ratio": 0.50, "warning": None},
}


def fee_breakdown(platform: str, bid: float, title_type: str = "salvage") -> dict:
    """Itemized auction + broker fees for a winning bid on a platform."""
    bid = max(0.0, float(bid or 0))
    tt = title_type if title_type in ("clean", "salvage") else "salvage"
    items = []

    if platform == "copart_public":
        items = [
            ("Copart buyer fee", copart_buyer_fee(bid)),
            ("Virtual bid fee", COPART_VIRTUAL_BID_FEE),
            (f"Gate fee ({tt} title)", COPART_GATE_FEE[tt]),
            ("Environmental fee", COPART_ENVIRONMENTAL_FEE),
            ("Title handling", COPART_TITLE_MAIL_FEE),
        ]
    elif platform == "iaai_public":
        items = [
            ("IAAI buyer fee (est.)", iaai_buyer_fee(bid)),
            ("Internet bid fee", iaai_internet_fee(bid)),
            ("Service fee (pull-out/loading)", IAAI_SERVICE_FEE),
            ("Environmental fee", IAAI_ENVIRONMENTAL_FEE),
            ("Title handling", IAAI_TITLE_FEE),
        ]
    elif platform == "abetterbid":
        items = [
            ("Copart buyer fee", copart_buyer_fee(bid)),
            (f"Copart gate fee ({tt} title)", COPART_GATE_FEE[tt]),
            ("Copart environmental fee", COPART_ENVIRONMENTAL_FEE),
            ("Copart broker fee", ABB_COPART_BROKER_FEE),
            ("Mailing fee", ABB_MAILING_FEE),
            ("A Better Bid bid fee", abb_bid_fee(bid)),
            ("A Better Bid transaction fee",
             round(max(ABB_TRANSACTION_MIN, bid * ABB_TRANSACTION_PCT), 2)),
        ]
    elif platform in ("abm_advanced", "abm_premium"):
        tier = ABM_ADVANCED_TXN if platform == "abm_advanced" else ABM_PREMIUM_TXN
        items = [
            ("Copart buyer fee", copart_buyer_fee(bid)),
            (f"Copart gate fee ({tt} title)", COPART_GATE_FEE[tt]),
            ("Copart environmental fee", COPART_ENVIRONMENTAL_FEE),
            ("Copart broker fee", ABM_COPART_BROKER_FEE),
            ("ABM transaction fee", abm_transaction_fee(bid, tier)),
            ("ABM website processing fee", ABM_PROCESSING_FEE),
        ]
    else:
        raise ValueError(f"Unknown platform: {platform}")

    items = [(label, round(float(amt), 2)) for label, amt in items]
    return {"items": items, "total": round(sum(a for _, a in items), 2)}


PLATFORMS = {
    "copart_public": "Copart direct (public, where allowed)",
    "iaai_public": "IAAI direct (public)",
    "abetterbid": "A Better Bid (Copart broker)",
    "abm_advanced": "AutoBidMaster Advanced (Copart broker)",
    "abm_premium": "AutoBidMaster Premium (Copart broker)",
}
