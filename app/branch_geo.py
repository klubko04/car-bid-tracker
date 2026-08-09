"""
Distance-from-Federal-Way (98003) bucketing for the image-archive pipeline.

apibara's `location.display` field (e.g. "Portland North (OR)") is the only
location signal reliably present on every record -- per-lot coordinates
(`facility.lat`/`details.attributes.StorageLocation*`) were populated on
only 3 of 75 records observed in test/test_apibara_sold_iaai_02.py, so the
API's own distance/facility fields cannot be relied on.

This module is a static, hand-curated city/state -> lat/lng table instead:
city-level accuracy (not exact branch coordinates), which is enough to sort
a lot into a 250-mile bucket. Unrecognized cities fall back to their state's
geographic centroid; a location string that doesn't parse as "City (ST)"
returns None / "unknown".

Extend CITY_COORDS as real branch names turn up in live pulls that aren't
covered here.
"""
import math
import re

# Federal Way, WA 98003 -- transport destination
DEST_LAT, DEST_LNG = 47.3223, -122.3126
ROAD_FACTOR = 1.2  # great-circle -> rough road miles (US long-haul)
BUCKET_STEP = 250  # miles

STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.2, -149.4), "AZ": (34.2, -111.9), "AR": (34.9, -92.4),
    "CA": (37.2, -119.7), "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5),
    "DC": (38.9, -77.0), "FL": (28.6, -82.4), "GA": (32.6, -83.4), "HI": (20.3, -156.3),
    "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.0, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.0, -92.0), "ME": (45.4, -69.2),
    "MD": (39.0, -76.7), "MA": (42.2, -71.5), "MI": (44.3, -85.4), "MN": (46.3, -94.3),
    "MS": (32.7, -89.7), "MO": (38.4, -92.5), "MT": (46.9, -110.4), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.1, -74.7), "NM": (34.5, -106.1),
    "NY": (42.9, -75.5), "NC": (35.6, -79.4), "ND": (47.5, -100.5), "OH": (40.4, -82.8),
    "OK": (35.6, -97.5), "OR": (44.0, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.5, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3),
    "UT": (39.3, -111.7), "VT": (44.0, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.5),
    "WV": (38.6, -80.7), "WI": (44.6, -89.9), "WY": (42.8, -107.3),
}

# (city, state) -> (lat, lng). City names lowercased, no directional suffix.
CITY_COORDS = {
    ("seattle", "WA"): (47.6062, -122.3321), ("tacoma", "WA"): (47.2529, -122.4443),
    ("spokane", "WA"): (47.6588, -117.4260), ("portland", "OR"): (45.5152, -122.6784),
    ("eugene", "OR"): (44.0521, -123.0868), ("medford", "OR"): (42.3265, -122.8756),
    ("sacramento", "CA"): (38.5816, -121.4944), ("san francisco", "CA"): (37.7749, -122.4194),
    ("san jose", "CA"): (37.3382, -121.8863), ("los angeles", "CA"): (34.0522, -118.2437),
    ("san diego", "CA"): (32.7157, -117.1611), ("fresno", "CA"): (36.7378, -119.7871),
    ("bakersfield", "CA"): (35.3733, -119.0187), ("phoenix", "AZ"): (33.4484, -112.0740),
    ("tucson", "AZ"): (32.2226, -110.9747), ("las vegas", "NV"): (36.1699, -115.1398),
    ("reno", "NV"): (39.5296, -119.8138), ("denver", "CO"): (39.7392, -104.9903),
    ("colorado springs", "CO"): (38.8339, -104.8214), ("salt lake city", "UT"): (40.7608, -111.8910),
    ("boise", "ID"): (43.6150, -116.2023), ("albuquerque", "NM"): (35.0844, -106.6504),
    ("dallas", "TX"): (32.7767, -96.7970), ("fort worth", "TX"): (32.7555, -97.3308),
    ("houston", "TX"): (29.7604, -95.3698), ("san antonio", "TX"): (29.4241, -98.4936),
    ("austin", "TX"): (30.2672, -97.7431), ("el paso", "TX"): (31.7619, -106.4850),
    ("oklahoma city", "OK"): (35.4676, -97.5164), ("tulsa", "OK"): (36.1540, -95.9928),
    ("kansas city", "MO"): (39.0997, -94.5786), ("st louis", "MO"): (38.6270, -90.1994),
    ("minneapolis", "MN"): (44.9778, -93.2650), ("chicago", "IL"): (41.8781, -87.6298),
    ("indianapolis", "IN"): (39.7684, -86.1581), ("columbus", "OH"): (39.9612, -82.9988),
    ("cleveland", "OH"): (41.4993, -81.6944), ("cincinnati", "OH"): (39.1031, -84.5120),
    ("detroit", "MI"): (42.3314, -83.0458), ("grand rapids", "MI"): (42.9634, -85.6681),
    ("milwaukee", "WI"): (43.0389, -87.9065), ("des moines", "IA"): (41.5868, -93.6250),
    ("omaha", "NE"): (41.2565, -95.9345), ("memphis", "TN"): (35.1495, -90.0490),
    ("nashville", "TN"): (36.1627, -86.7816), ("atlanta", "GA"): (33.7490, -84.3880),
    ("birmingham", "AL"): (33.5186, -86.8104), ("jackson", "MS"): (32.2988, -90.1848),
    ("new orleans", "LA"): (29.9511, -90.0715), ("baton rouge", "LA"): (30.4515, -91.1871),
    ("little rock", "AR"): (34.7465, -92.2896), ("louisville", "KY"): (38.2527, -85.7585),
    ("charlotte", "NC"): (35.2271, -80.8431), ("raleigh", "NC"): (35.7796, -78.6382),
    ("columbia", "SC"): (34.0007, -81.0348), ("jacksonville", "FL"): (30.3322, -81.6557),
    ("orlando", "FL"): (28.5383, -81.3792), ("tampa", "FL"): (27.9506, -82.4572),
    ("miami", "FL"): (25.7617, -80.1918), ("richmond", "VA"): (37.5407, -77.4360),
    ("virginia beach", "VA"): (36.8529, -75.9780), ("washington", "DC"): (38.9072, -77.0369),
    ("baltimore", "MD"): (39.2904, -76.6122), ("philadelphia", "PA"): (39.9526, -75.1652),
    ("pittsburgh", "PA"): (40.4406, -79.9959), ("new york", "NY"): (40.7128, -74.0060),
    ("albany", "NY"): (42.6526, -73.7562), ("buffalo", "NY"): (42.8864, -78.8784),
    ("boston", "MA"): (42.3601, -71.0589), ("hartford", "CT"): (41.7658, -72.6734),
    ("providence", "RI"): (41.8240, -71.4128), ("portland", "ME"): (43.6591, -70.2568),
    ("newark", "NJ"): (40.7357, -74.1724), ("wilmington", "DE"): (39.7447, -75.5484),
    ("charleston", "WV"): (38.3498, -81.6326), ("anchorage", "AK"): (61.2181, -149.9003),
    ("honolulu", "HI"): (21.3069, -157.8583),
}

_DIRECTIONAL_SUFFIXES = {
    "north", "south", "east", "west", "central", "nw", "ne", "sw", "se", "metro",
}
_DISPLAY_RE = re.compile(r"^(.*?)\s*\(([A-Za-z]{2})\)\s*$")


def _strip_directional(city_words: list) -> list:
    while city_words and city_words[-1].lower().strip(".,") in _DIRECTIONAL_SUFFIXES:
        city_words = city_words[:-1]
    while city_words and city_words[-1].isdigit():
        city_words = city_words[:-1]
    return city_words


def _lookup_city(city_raw: str, state: str):
    words = _strip_directional(city_raw.split())
    while words:
        key = (" ".join(words).lower(), state)
        if key in CITY_COORDS:
            return CITY_COORDS[key]
        words = words[:-1]
    return None


def coords_for_location(location_display: str):
    """(lat, lng) for a location.display string, or None if unresolvable."""
    if not location_display:
        return None
    m = _DISPLAY_RE.match(location_display.strip())
    if not m:
        return None
    city_raw, state = m.group(1), m.group(2).upper()
    hit = _lookup_city(city_raw, state)
    if hit:
        return hit
    return STATE_CENTROIDS.get(state)


def haversine_mi(lat: float, lng: float) -> float:
    R = 3958.8
    p1, l1, p2, l2 = map(math.radians, (DEST_LAT, DEST_LNG, lat, lng))
    return 2 * R * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))


def distance_miles(location_display: str):
    """Approximate road-distance miles from Federal Way, WA, or None."""
    c = coords_for_location(location_display)
    if c is None:
        return None
    return haversine_mi(*c) * ROAD_FACTOR


def distance_bucket(location_display: str) -> str:
    """'250mi' / '500mi' / ... (rounded up to BUCKET_STEP), or 'unknown'."""
    miles = distance_miles(location_display)
    if miles is None:
        return "unknown"
    bucket = max(BUCKET_STEP, int(math.ceil(miles / BUCKET_STEP) * BUCKET_STEP))
    return f"{bucket}mi"
