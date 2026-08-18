"""Shared Copart market classification for the US-only analytics pipeline.

APIBara does not expose a reliable market field on Copart records.  The branch
region in ``location.display`` is the strongest signal; facility state/ZIP are
fallbacks.  Unknown is deliberately distinct from UnitedStates so the adapter
can keep only positively identified US lots rather than letting an ambiguous
record leak into US fee and currency maths.
"""
import re

_LOCATION_REGION = re.compile(r"\(([A-Za-z]{2})\)\s*$")
_CANADIAN_POSTAL = re.compile(r"^[A-Z]\d[A-Z](?:\s?\d[A-Z]\d)?")
_US_ZIP = re.compile(r"^\d{5}(?:[- ]?\d{4})?")

CANADIAN_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC",
    "SK", "YT",
}
CANADIAN_REGION_NAMES = {
    "ALBERTA": "AB", "BRITISH COLUMBIA": "BC", "MANITOBA": "MB",
    "NEW BRUNSWICK": "NB", "NEWFOUNDLAND": "NL", "NOVA SCOTIA": "NS",
    "ONTARIO": "ON", "PRINCE EDWARD ISLAND": "PE", "QUEBEC": "QC",
    "SASKATCHEWAN": "SK",
}
US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def nested(data, *path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def branch_state(record):
    display = str(nested(record, "location", "display") or "")
    match = _LOCATION_REGION.search(display)
    if match:
        return match.group(1).upper()
    upper = display.upper()
    for name, code in CANADIAN_REGION_NAMES.items():
        if name in upper:
            return code
    state = str(nested(record, "facility", "state") or "").strip().upper()
    return state or None


def market(record):
    region = str(branch_state(record) or "").upper()
    if region in CANADIAN_REGIONS:
        return "Canada"
    if region in US_REGIONS:
        return "UnitedStates"
    postal = str(nested(record, "facility", "zip") or "").strip().upper()
    if _CANADIAN_POSTAL.match(postal):
        return "Canada"
    if _US_ZIP.match(postal):
        return "UnitedStates"
    return None


def is_us(record):
    return market(record) == "UnitedStates"
