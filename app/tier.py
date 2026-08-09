"""
Car want-list tier classification for the image-archive pipeline.

Tier 1 -- the target buys:
    Audi S4 / S5, 2018-2022
    Lexus IS300 / IS350, 6-cylinder (3.5L V6), 2016-2020
Tier 2 -- close alternates:
    Lexus IS300, 4-cylinder (2.0L turbo I4)
    Audi A4 / A5
    Infiniti Q50
Tier 3 -- everything else worth archiving:
    Honda / Toyota / Mazda sedans

Unmatched make/model returns None -- caller decides whether to skip it from
the tiered archive (see app/image_pipeline.py).
"""
import re

TIER_1 = "Tier 1"
TIER_2 = "Tier 2"
TIER_3 = "Tier 3"


def _is_six_cylinder(engine_raw: str) -> bool:
    text = (engine_raw or "").upper()
    if "V-6" in text or "V6" in text:
        return True
    if "I-4" in text or "I4" in text or " L4" in text:
        return False
    m = re.search(r"(\d\.\d)\s*L", text)
    if m:
        return float(m.group(1)) >= 3.0
    return False


def classify(make: str, model: str, year, engine_raw: str = "") -> str | None:
    make_n = (make or "").strip().lower()
    model_n = (model or "").strip().lower().replace(" ", "")
    try:
        y = int(year) if year else None
    except (TypeError, ValueError):
        y = None

    if make_n == "audi" and model_n in ("s4", "s5") and y and 2018 <= y <= 2022:
        return TIER_1
    if make_n == "lexus" and model_n in ("is300", "is350") and y and 2016 <= y <= 2020:
        return TIER_1 if _is_six_cylinder(engine_raw) else TIER_2
    if make_n == "audi" and model_n in ("a4", "a5"):
        return TIER_2
    if make_n == "infiniti" and model_n == "q50":
        return TIER_2
    if make_n in ("honda", "toyota", "mazda"):
        return TIER_3
    return None
