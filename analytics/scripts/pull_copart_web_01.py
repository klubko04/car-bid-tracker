"""
Copart web pull — stage 1 of the analytics pipeline. RAW SOURCE ARCHIVE ONLY.

    pull_copart_web_01.py
        -> data/open/json-raw/copart/copartweb_copart_open_*.json
        -> future copart_web_adapt_01.py
        -> apibara_json2csv_copart_01.py

``copart_vpic_adapt_01.py`` is deliberately NOT in that chain: vPIC decodes a
VIN and Copart masks the VIN here. See VIN MASKING below.

The browser URL supplied by the operator is the public discovery surface:

    https://www.copart.com/lotSearchResults?free=true&query=2018%20audi%20s5

The HTML shell is protected by Imperva and is not a stable data contract. The
page itself POSTs form fields to Copart's first-party JSON endpoint instead:

    POST https://www.copart.com/public/lots/search

This script archives that response verbatim. No API key or APIBara quota is
used. The default invocation is the current prime cohort and makes six search
requests, one per year:

    python analytics/scripts/pull_copart_web_01.py
    python analytics/scripts/pull_copart_web_01.py --details
    python analytics/scripts/pull_copart_web_01.py --details --max-details 5
    python analytics/scripts/pull_copart_web_01.py --dry-run

EXACT S5 FILTERING
------------------
Copart's model group is literally ``S5/RS5``. Filtering on that group admits
RS5 lots. A free-form search is broader still: the live ``2018 audi s5`` query
reported 35,248 rows because its tokens are not an exact identity predicate.

Every yearly request therefore combines the operator-visible free-form query
with Copart's exact YEAR, MAKE and *model-description* (MODL) facets:

    filter[YEAR] = lot_year:"2018"
    filter[MAKE] = lot_make_desc:"AUDI"
    filter[MODL] = lot_model_desc:"S5"       # not MODLG="S5/RS5"

The returned ``lcy`` / ``mkn`` / ``lm`` fields then pass through a second,
client-side exact gate. Rejected rows remain preserved in the raw page response
and are named under ``queries[].excluded_identity``; they are merely absent from
the top-level records handed to the future adapter.

SELLER — READ FROM THE SEARCH ROW, NOT FROM DETAILS
---------------------------------------------------
The seller company name ships in the search response itself, in ``scn``. No
detail request is needed and none will help. On the live 2018-2023 Audi S5
cohort ``scn`` was populated on 18 of 73 rows (25%), every one a carrier:
GEICO 10, USAA 5, CSAA 1, Bristol West 1, Farmers 1.

``showSeller`` is a UI display flag, NOT a data-presence flag: 14 of those 18
rows carry ``scn`` while ``showSeller`` is false. Reading ``showSeller``
instead of ``scn`` is how a 25%-coverage field looks like a 5% one.

Copart never publishes a seller *type* on any public surface — there is no such
field on the row and no seller facet on the search response. Class is therefore
inferred from the name by ``copart_seller.classify``, which returns
``insurance`` / ``finance`` / ``dealer`` / ``non_insurance`` / ``unknown``
together with the rule that fired. Absence stays ``unknown``; it never becomes
``non_insurance``.

WHAT ``--details`` IS ACTUALLY FOR
----------------------------------
Not seller data. The first-party endpoint

    GET /public/data/lotdetails/solr/{lot}

returns the same Solr document as the search row — 111 identical keys, and it
*drops* five the search row has, including ``ltd``/``lmtd`` (trim). It contains
no seller name and no seller type.

It is also rate-limited hard. A full 73-lot run scored 6 successes and 67
failures: Imperva served 45 challenges as HTTP 200 and 22 as 403, and the
lot-page HTML fallback was blocked on every single row, so its parser has never
run against live Copart output. ``--details`` is off by default, prints a
warning when enabled, and exists only to re-probe whether that contract has
changed. Do not put it in a scheduled pull.

VIN MASKING
-----------
Copart masks the VIN on the public surface — ``fv`` arrives as
``WAUB4CF52JA******`` on 73 of 73 rows, in both search and detail responses.
No full VIN means this source cannot feed ``copart_vpic_adapt_01.py`` (whose
VIN_RE rejects the mask) and cannot be VIN-joined to an APIBara pull. Lots from
this source are keyed by lot number alone. The count is reported per run.

MARKET SCOPE
------------
This is the raw capture stage and intentionally does not delete Canadian rows.
It records ``locCountry``/``siteCodes`` and reports the observed market mix.
The Copart web adapter must apply the project's US-only boundary, mirroring the
existing APIBara Copart adapter. This keeps the raw source complete while
preventing Canada from reaching adapted JSON or CSV.

FRAGILITY
---------
These are public website endpoints, not a supported API. Search/detail failures,
non-JSON responses and Imperva challenges are explicit archive data. A run that
finds no exact records exits non-zero rather than writing a plausible-looking
empty cohort.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import copart_market
import copart_seller


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "analytics" / "data"

BASE = "https://www.copart.com"
SEARCH_PAGE = BASE + "/lotSearchResults"
SEARCH_ENDPOINT = BASE + "/public/lots/search"
DETAIL_ENDPOINT = BASE + "/public/data/lotdetails/solr/{lot_number}"

PLATFORM = "copart"
SOURCE = "copart-web"
MODE = "open"
PAGE_SIZE = 100
RATE_DELAY = 1.5
DEFAULT_MAKE = "Audi"
DEFAULT_MODEL = "S5"
DEFAULT_YEARS = (2018, 2023)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)


def layer_dir():
    return DATA_DIR / MODE / "json-raw" / PLATFORM


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def clean(value):
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text or None


def norm_identity(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def quote_solr(value):
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def parse_year_range(value):
    match = re.fullmatch(r"\s*(\d{4})\s*(?:[-:]\s*(\d{4}))?\s*", value or "")
    if not match:
        raise argparse.ArgumentTypeError("--year-range wants YYYY or YYYY-YYYY")
    low, high = int(match.group(1)), int(match.group(2) or match.group(1))
    if high < low:
        raise argparse.ArgumentTypeError(f"--year-range {low}-{high} is backwards")
    return low, high


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def display_search_url(keyword):
    return SEARCH_PAGE + "?" + urllib.parse.urlencode({"free": "true", "query": keyword})


def detail_url(record):
    raw = record.get("search") if isinstance(record.get("search"), dict) else record
    lot = str(
        record.get("lot_number") or raw.get("ln") or raw.get("lotNumberStr") or ""
    ).strip()
    slug = str(raw.get("ldu") or "").strip(" /")
    return f"{BASE}/lot/{lot}" + (f"/{slug}" if slug else "")


def search_form(year, make, model, page=0, size=PAGE_SIZE):
    """Repeated form fields used by Copart's own public search page."""
    keyword = f"{year} {make} {model}"
    return [
        ("query", keyword),
        ("filter[YEAR]", f"lot_year:{quote_solr(year)}"),
        ("filter[MAKE]", f"lot_make_desc:{quote_solr(str(make).upper())}"),
        ("filter[MODL]", f"lot_model_desc:{quote_solr(str(model).upper())}"),
        ("sort", "auction_date_type desc"),
        ("sort", "auction_date_utc asc"),
        ("page", str(page)),
        ("size", str(size)),
        ("watchListOnly", "false"),
        ("freeFormSearch", "true"),
    ]


def form_summary(form):
    out = {}
    for key, value in form:
        if key in out:
            out[key] = out[key] if isinstance(out[key], list) else [out[key]]
            out[key].append(value)
        else:
            out[key] = value
    return out


class HttpSession:
    """Cookie-preserving stdlib transport with a never-raises result contract."""

    def __init__(self, timeout=60):
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request(self, request):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.getcode(), raw.decode("utf-8", "replace"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace"), dict(error.headers)
        except Exception as error:  # noqa: BLE001 - failures are archived, not hidden
            return 0, f"__ERROR__ {type(error).__name__}: {error}", {}

    def post_form(self, url, form, referer):
        body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": BASE,
            "Referer": referer,
        })
        return self.request(request)

    def get(self, url, referer=None):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        return self.request(urllib.request.Request(url, headers=headers))


def json_body(body):
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def parse_search_payload(payload):
    if not isinstance(payload, dict):
        return None, "response was not JSON"
    if payload.get("returnCode") != 1:
        message = (payload.get("data") or {}).get("errorMsg") \
            if isinstance(payload.get("data"), dict) else None
        return None, clean(message) or clean(payload.get("returnCodeDesc")) or "Copart error"
    results = ((payload.get("data") or {}).get("results") or {})
    content = results.get("content")
    if not isinstance(content, list):
        return None, "data.results.content was not a list"
    return results, None


def identity_match(record, year, make, model):
    actual = {
        "year": record.get("lcy"),
        "make": record.get("mkn"),
        "model": record.get("lm"),
        "model_group": record.get("lmg"),
    }
    reasons = []
    try:
        actual_year = int(actual["year"])
    except (TypeError, ValueError):
        actual_year = None
    if actual_year != int(year):
        reasons.append(f"year={actual['year']!r}")
    if norm_identity(actual["make"]) != norm_identity(make):
        reasons.append(f"make={actual['make']!r}")
    if norm_identity(actual["model"]) != norm_identity(model):
        reasons.append(f"model={actual['model']!r}")
    return not reasons, reasons, actual


def market_label(record):
    """Market of a web search row.

    Two traps, both of which previously sent Canadian lots to UnitedStates:
    Copart sends the country as the ISO-3 code ``"CAN"``, not ``"Canada"``; and
    a Canadian lot is cross-listed on the US site, so it carries BOTH
    ``CPRTCA`` and ``CPRTUS``. The site codes must therefore be read
    Canada-first, and only as a last resort.
    """
    country = norm_identity(record.get("locCountry"))
    if country in {"usa", "us", "unitedstates", "unitedstatesofamerica"}:
        return "UnitedStates"
    if country in {"can", "ca", "canada"}:
        return "Canada"
    # locState is a plain two-letter region on the web row; reuse the region
    # sets the APIBara-side classifier already maintains.
    state = str(record.get("locState") or "").strip().upper()
    if state in copart_market.CANADIAN_REGIONS:
        return "Canada"
    if state in copart_market.US_REGIONS:
        return "UnitedStates"
    sites = {str(value).upper() for value in record.get("siteCodes") or []}
    if "CPRTCA" in sites:
        return "Canada"
    if "CPRTUS" in sites:
        return "UnitedStates"
    return "unknown"


class VisibleHTML(HTMLParser):
    """Small stdlib visible-text/image collector for the detail-page fallback."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.tokens = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "noscript"}:
            self.hidden += 1
        if tag == "img":
            values = dict(attrs)
            url = values.get("src") or values.get("data-src")
            if url:
                normalized = urllib.parse.urljoin(BASE + "/", html.unescape(url.strip()))
                if normalized.startswith(("https://", "http://")) \
                        and normalized not in self.images:
                    self.images.append(normalized)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            value = clean(data)
            if value:
                self.tokens.append(value)


DETAIL_LABELS = (
    "Seller", "VIN", "Lot number", "Sale name", "Location", "Title code",
    "Odometer", "Primary damage", "Secondary damage", "Estimated retail value",
    "Repair cost", "Cylinders", "Color", "Has key", "Engine type",
    "Transmission", "Vehicle type", "Drivetrain", "Fuel", "Body style",
    "Sale date", "Highlights", "Notes", "Current bid",
)


def labelled_value(tokens, label):
    target = label.casefold()
    for index, token in enumerate(tokens):
        folded = token.casefold()
        if folded == target or folded == target + ":":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if folded.startswith(target + ":"):
            return clean(token[len(label) + 1:])
    return None


def parse_detail_html(document):
    parser = VisibleHTML()
    try:
        parser.feed(document)
    except Exception:  # HTMLParser is forgiving; keep partial tokens if it still fails
        pass
    labels = {label: labelled_value(parser.tokens, label) for label in DETAIL_LABELS}
    return {
        "labels": {key: value for key, value in labels.items() if value},
        "image_urls": parser.images,
        "visible_text_tokens": parser.tokens,
    }


SELLER_NAME_KEYS = {
    "seller", "sellername", "sellercompany", "sellercompanyname", "sellerdisplayname",
}
SELLER_TYPE_KEYS = {"sellertype", "sellerclass", "sellerclassification"}


def recursive_scalar(document, wanted):
    found = []

    def visit(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                next_path = path + (str(key),)
                if normalized in wanted and not isinstance(child, (dict, list)) and clean(child):
                    found.append((".".join(next_path), clean(child)))
                visit(child, next_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (str(index),))

    visit(document)
    return found[0] if found else (None, None)


# Seller keys as they appear on the SEARCH row. These are the ones that carry
# real data; the detail endpoint has no seller field at all (see module
# docstring). ``scn`` is the company name, ``smd`` its social-media blob, and
# ``showSeller`` is a display flag that does not track whether ``scn`` is set.
SEARCH_SELLER_NAME_KEY = "scn"
SEARCH_SELLER_SHOW_KEY = "showSeller"
SEARCH_SELLER_MEDIA_KEY = "smd"


def classify_seller(name=None, published_type=None, source=None):
    """Thin delegate to the shared taxonomy so Copart has ONE classifier.

    Kept as a named function because the detail-page fallback and the search
    row both feed it, and because the repo already carries three divergent
    copies of the damage-string mapping — this one is not going to become the
    second seller equivalent.
    """
    return copart_seller.classify(name, published_type, source)


def search_seller(row):
    """Seller for one search row. Costs nothing: the name is already here.

    Returns the shared classifier's audit dict plus the two raw Copart signals,
    so a later reader can tell "Copart published no name" (``scn`` absent) from
    "Copart published a name it chose not to render" (``scn`` set,
    ``showSeller`` false) — 14 of 18 named lots in the reference cohort are the
    second kind.
    """
    name = clean(row.get(SEARCH_SELLER_NAME_KEY))
    result = classify_seller(name, source=f"search.{SEARCH_SELLER_NAME_KEY}" if name else None)
    result["show_seller_flag"] = bool(row.get(SEARCH_SELLER_SHOW_KEY))
    result["social_media"] = row.get(SEARCH_SELLER_MEDIA_KEY) or None
    return result


def better_seller(current, candidate):
    """Prefer a classified seller over an unclassified one; else keep current.

    Detail data is not automatically better than search data here — usually it
    is strictly worse (empty). Only take it when it actually resolved a class.
    """
    if not candidate or candidate.get("class") in (None, "unknown"):
        return current
    if not current or current.get("class") == "unknown":
        return candidate
    return current


VIN_MASK_RE = re.compile(r"[^A-HJ-NPR-Z0-9]")


def vin_is_masked(value):
    """Copart returns 'WAUB4CF52JA******' on the public surface — 73/73 rows."""
    text = str(value or "").strip().upper()
    return bool(text) and (len(text) != 17 or bool(VIN_MASK_RE.search(text)))


def parse_detail_json(payload):
    """Seller sweep over a detail payload.

    Retained as a probe, not as a data path: no observed Copart detail response
    has ever contained any of SELLER_NAME_KEYS/SELLER_TYPE_KEYS. If one ever
    does, this catches it and ``better_seller`` will prefer it over the search
    row. Until then it returns an unknown that gets discarded.
    """
    name_path, name = recursive_scalar(payload, SELLER_NAME_KEYS)
    type_path, published_type = recursive_scalar(payload, SELLER_TYPE_KEYS)
    source = type_path or name_path
    return {"seller": classify_seller(name, published_type, source)}


def is_challenge(body):
    folded = str(body or "").casefold()
    return "_incapsula_resource" in folded or "request unsuccessful" in folded


def detail_payload_ok(payload):
    if not isinstance(payload, dict):
        return False
    return_code = payload.get("returnCode")
    if return_code is not None and return_code != 1:
        return False
    data = payload.get("data")
    return not (isinstance(data, dict) and data.get("errorMsg"))


def fetch_detail(session, record, keep_html=False):
    raw = record.get("search") if isinstance(record.get("search"), dict) else record
    lot = str(
        record.get("lot_number") or raw.get("ln") or raw.get("lotNumberStr") or ""
    ).strip()
    page_url = record.get("detail_url") or detail_url(record)
    api_url = DETAIL_ENDPOINT.format(lot_number=urllib.parse.quote(lot))
    attempts = []

    status, body, headers = session.get(api_url, referer=page_url)
    content_type = clean(headers.get("Content-Type") or headers.get("content-type"))
    payload = json_body(body)
    attempts.append({
        "kind": "detail_json", "url": api_url, "status": status,
        "content_type": content_type, "sha256": sha256_text(body),
        "error": None if status == 200 and detail_payload_ok(payload) else
                 ("imperva_challenge" if is_challenge(body) else "non_json_or_http_error"),
    })
    if status == 200 and detail_payload_ok(payload):
        return {
            "status": "ok", "source": "copart_detail_json", "page_url": page_url,
            "attempts": attempts, "raw": payload, "fields": parse_detail_json(payload),
        }, len(attempts)

    status, body, headers = session.get(
        page_url, referer=display_search_url(str(raw.get("lcy") or ""))
    )
    content_type = clean(headers.get("Content-Type") or headers.get("content-type"))
    parsed = parse_detail_html(body) if status == 200 and not is_challenge(body) else None
    attempts.append({
        "kind": "lot_page", "url": page_url, "status": status,
        "content_type": content_type, "sha256": sha256_text(body),
        "error": None if parsed is not None else
                 ("imperva_challenge" if is_challenge(body) else "http_or_parse_error"),
    })
    if parsed is None:
        # No seller key here. Emitting classify_seller() would stamp "unknown"
        # over a perfectly good search-row classification — that is exactly how
        # the first live run reported 73/73 unknown while holding 18 carriers.
        return {"status": "failed", "source": None, "page_url": page_url,
                "attempts": attempts, "fields": {}}, len(attempts)

    seller_name = parsed["labels"].get("Seller")
    parsed["seller"] = classify_seller(seller_name, source="detail_page.Seller")
    detail = {"status": "ok", "source": "copart_lot_page", "page_url": page_url,
              "attempts": attempts, "fields": parsed}
    if keep_html:
        detail["raw_html"] = body
    return detail, len(attempts)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pull_copart_web_01.py",
        description="Archive exact Copart web search JSON and optional lot details. "
                    "Defaults to six 2018-2023 Audi S5 queries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--make", default=DEFAULT_MAKE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--year-range", type=parse_year_range, default=DEFAULT_YEARS,
                        metavar="YYYY-YYYY")
    parser.add_argument("--details", action="store_true",
                        help="re-probe the lot-details endpoint (2 requests/lot). It carries NO "
                             "seller data and Imperva blocks it after ~6 lots; seller comes from "
                             "the search row either way. Diagnostic only")
    parser.add_argument("--max-details", type=int, default=0, metavar="N",
                        help="cap detail lots (default: all exact lots)")
    parser.add_argument("--max-pages", type=int, default=20, metavar="N",
                        help="safety cap per yearly query (default: 20)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE, metavar="N",
                        help=f"search rows per request, 1-{PAGE_SIZE} (default: {PAGE_SIZE})")
    parser.add_argument("--delay", type=float, default=RATE_DELAY,
                        help=f"seconds between requests (default: {RATE_DELAY})")
    parser.add_argument("--keep-html", action="store_true",
                        help="retain successful fallback lot-page HTML in the raw archive")
    parser.add_argument("--out", help="output basename/path (default: auto-named)")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def resolve_out_path(args):
    low, high = args.year_range
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    if args.out:
        path = Path(args.out)
        if not path.suffix:
            path = path.with_suffix(".json")
        return path if path.is_absolute() else layer_dir() / path
    label = f"{slugify(args.make)}_{slugify(args.model)}_{low}_{high}"
    return layer_dir() / f"copartweb_{PLATFORM}_{MODE}_{label}_{stamp}.json"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    if not 1 <= args.page_size <= PAGE_SIZE:
        raise SystemExit(f"--page-size must be between 1 and {PAGE_SIZE}")
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1")
    low, high = args.year_range
    years = list(range(low, high + 1))
    out_path = resolve_out_path(args)

    print("=" * 78)
    print(f"COPART WEB — {len(years)} exact {args.make} {args.model} yearly query(s)")
    for year in years:
        print(f"    {year} {args.make} {args.model}")
    print("  identity: free query + exact YEAR/MAKE/MODL facets + client gate")
    print("  market:   raw all-markets capture; US-only boundary belongs in the adapter")
    print(f"  details:  {'ON' if args.details else 'off (--details)'}")
    print("=" * 78)

    if args.dry_run:
        print("\n  DRY RUN — nothing fetched.")
        for year in years:
            keyword = f"{year} {args.make} {args.model}"
            print(f"  PAGE {display_search_url(keyword)}")
            print(f"  POST {SEARCH_ENDPOINT}")
            print(f"       {form_summary(search_form(year, args.make, args.model, 0, args.page_size))}")
        if args.details:
            print(f"  GET  {DETAIL_ENDPOINT.format(lot_number='<lot_number>')}  (per exact lot)")
            print(f"  GET  {BASE}/lot/<lot_number>/<slug>  (fallback)")
        print(f"  would write -> {out_path}")
        return 0

    session = HttpSession()
    archive = {
        "generated_at": now_iso(), "argv": argv, "platform": PLATFORM,
        "source": SOURCE, "mode": MODE,
        "search_params": {
            "make": args.make, "model": args.model, "year_min": low,
            "year_max": high, "page_size": args.page_size,
            "identity_policy": "exact_year_make_model",
            "server_facets": ["YEAR", "MAKE", "MODL"],
            "market_scope": "all_raw_adapter_us_only",
            "details": bool(args.details),
        },
        "queries": [], "records": [],
    }

    by_lot = {}
    requests_made = 0
    any_truncated = False

    for query_index, year in enumerate(years, 1):
        keyword = f"{year} {args.make} {args.model}"
        referer = display_search_url(keyword)
        entry = {
            "keyword": keyword, "display_url": referer,
            "endpoint": SEARCH_ENDPOINT,
            "server_filters": form_summary(search_form(year, args.make, args.model, 0, args.page_size)),
            "pages": [], "excluded_identity": [],
        }
        total = None
        fetched_rows = 0
        exact_rows = 0
        page_number = 0
        query_failed = False

        while page_number < args.max_pages:
            form = search_form(year, args.make, args.model, page_number, args.page_size)
            status, body, headers = session.post_form(SEARCH_ENDPOINT, form, referer)
            requests_made += 1
            payload = json_body(body)
            results, error = parse_search_payload(payload)
            page_entry = {
                "page": page_number, "status": status,
                "content_type": clean(headers.get("Content-Type") or headers.get("content-type")),
                "request_form": form_summary(form), "raw": payload,
            }
            if status != 200 or error:
                query_failed = True
                page_entry["error"] = (
                    "imperva_challenge" if is_challenge(body)
                    else error or clean(body[:300]) or "empty_error_response"
                )
                if payload is None:
                    page_entry["raw_text"] = body
                entry["pages"].append(page_entry)
                break

            rows = results["content"]
            total = int(results.get("totalElements") or 0)
            fetched_rows += len(rows)
            page_entry.update({"returned": len(rows), "total_reported": total})
            entry["pages"].append(page_entry)

            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                matches, reasons, actual = identity_match(raw, year, args.make, args.model)
                lot = str(raw.get("ln") or raw.get("lotNumberStr") or "").strip()
                if not matches:
                    entry["excluded_identity"].append({
                        "lot_number": lot or None, "actual": actual, "reasons": reasons,
                    })
                    continue
                exact_rows += 1
                record = {
                    "lot_number": lot or None, "keyword": keyword,
                    "detail_url": detail_url(raw), "search": raw,
                    "seller": search_seller(raw),
                    "vin_masked": vin_is_masked(raw.get("fv")),
                    "detail": None,
                }
                if lot:
                    by_lot.setdefault(lot, record)

            if fetched_rows >= total or not rows:
                break
            page_number += 1
            time.sleep(max(0, args.delay))

        entry.update({
            "total_reported": total, "rows_fetched": fetched_rows,
            "exact_records": exact_rows,
            "excluded_identity_count": len(entry["excluded_identity"]),
            "pages_fetched": len(entry["pages"]),
            "failed": query_failed,
            "truncated": bool(total is not None and fetched_rows < total),
        })
        any_truncated = any_truncated or entry["truncated"]
        archive["queries"].append(entry)
        flag = "  *** FAILED ***" if entry["failed"] else (
            "  *** TRUNCATED ***" if entry["truncated"] else ""
        )
        print(f"  [{query_index}/{len(years)}] {keyword:<24} "
              f"{exact_rows:>3} exact / {fetched_rows:>3} fetched "
              f"(site total {total}){flag}")
        if entry["excluded_identity"]:
            print(f"      excluded {len(entry['excluded_identity'])} identity mismatch(es)")
        if query_index < len(years):
            time.sleep(max(0, args.delay))

    records = list(by_lot.values())
    if not records:
        raise SystemExit(
            "\nNo exact lots were archived. Copart may have changed the endpoint/field "
            "contract, the cohort may be empty, or every request was challenged."
        )

    detail_http_requests = 0
    detail_failures = 0
    if args.details:
        targets = records[:args.max_details] if args.max_details > 0 else records
        print(f"\n  details: fetching {len(targets)} exact lot(s) at {args.delay}s intervals")
        print("      NOTE --details adds no seller data and is WAF-blocked after ~6 lots.")
        print("      Seller already came from the search row. This is a contract probe.")
        for index, record in enumerate(targets, 1):
            detail, attempts = fetch_detail(session, record, keep_html=args.keep_html)
            detail_http_requests += attempts
            requests_made += attempts
            record["detail"] = detail
            detail_failures += int(detail["status"] != "ok")
            # Only upgrades; a failed or seller-less detail leaves the search
            # row's classification untouched.
            record["seller"] = better_seller(
                record.get("seller"), (detail.get("fields") or {}).get("seller")
            )
            if index % 10 == 0 or index == len(targets):
                print(f"      {index}/{len(targets)}  failures={detail_failures}  "
                      f"seller so far={record['seller'].get('class', 'unknown')}")
            if index < len(targets):
                time.sleep(max(0, args.delay))

    archive["records"] = records
    market_counts = Counter(market_label(record["search"]) for record in records)
    market_lots = {}
    for record in records:
        market_lots.setdefault(market_label(record["search"]), []).append(record["lot_number"])
    # Seller is a property of every record now, not of the optional detail pass.
    seller_counts = Counter(
        (record.get("seller") or {}).get("class", "unknown") for record in records
    )
    seller_named = sum(1 for record in records if (record.get("seller") or {}).get("name"))
    seller_basis = Counter(
        (record.get("seller") or {}).get("basis", "not_published") for record in records
    )
    masked_vins = sum(1 for record in records if record.get("vin_masked"))
    archive["counts"] = {
        "records": len(records), "queries": len(years), "requests": requests_made,
        "search_requests": requests_made - detail_http_requests,
        "details_attempted": sum(1 for record in records if record.get("detail") is not None),
        "detail_http_requests": detail_http_requests,
        "detail_failures": detail_failures, "truncated": any_truncated,
        "failed_queries": sum(query["failed"] for query in archive["queries"]),
        "market_observed": dict(market_counts),
        "non_us_lot_numbers": {
            key: lots for key, lots in market_lots.items() if key != "UnitedStates"
        },
        "seller_class": dict(seller_counts),
        "seller_named": seller_named,
        "seller_basis": dict(seller_basis),
        "vin_masked": masked_vins,
        "vin_usable_for_vpic": len(records) - masked_vins,
    }

    print(f"\n  records: {len(records)} exact unique lots")
    print(f"  markets: {dict(market_counts)}  (raw retained; adapter will enforce US-only)")
    print(f"  sellers: {dict(seller_counts)}  "
          f"({seller_named}/{len(records)} named by Copart)")
    if masked_vins:
        print(f"  VINs:    {masked_vins}/{len(records)} masked — this source cannot feed vPIC")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as stream:
        json.dump(archive, stream, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {requests_made} HTTP request(s), 0 API quota used.")
    print(f"  JSON -> {out_path}")
    print("  next: copart_web_adapt_01.py must reshape + exclude non-US before vPIC/CSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
