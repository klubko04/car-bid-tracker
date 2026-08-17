"""
IAAI web pull — stage 1 of the analytics pipeline, iaai.com source. RAW JSON ONLY.

    pull_iaai_web_01.py  ->  raw .json  ->  iaaiweb_json2csv_iaai_01.py  ->  .csv
                                  |
                             pull_apibara_01.py  (enrichment: full VIN + seller)

Sibling of pull_apibara_01.py, same archive envelope, same data/ layout, same
"archive it untouched" rule. The difference is what it costs and what it sees:

    pull_apibara_01.py   100 API calls/MONTH, 20 lots/call, full VIN + seller,
                         sold history. Misses lots (see COVERAGE below).
    THIS SCRIPT          no quota, no key, 100 lots/request, every listing state
                         including "Auction Not Assigned". VIN and seller are
                         masked by IAAI.

They are complementary, not substitutes, and they JOIN — see THE JOIN below.

TWO STEPS
---------
step 1  GET /Search?searchkeyword=...      one request, up to 100 lots
step 2  GET /VehicleDetail/<itemId>~US     one request per lot, opt-in via
                                           --details, adds ACV / estimated
                                           repair / title doc / restraint etc.

Step 1 alone answers "what is out there". Step 2 is what makes a row usable for
the money math: ActualCashValue and EstimatedRepairCost are IAAI's own numbers
and appear ONLY on the lot page.

    python analytics/scripts/pull_iaai_web_01.py --keyword "2018 Audi A5"
    python analytics/scripts/pull_iaai_web_01.py --make Audi --model A5 \
        --year-range 2018-2023 --details
    python analytics/scripts/pull_iaai_web_01.py --keyword "2018 Audi A5" \
        --details --max-details 10
    python analytics/scripts/pull_iaai_web_01.py --keyword "2018 Audi A5" --dry-run

THE JOIN (why this script records `stock_number` first)
-------------------------------------------------------
IAAI puts TWO numbers on every lot and they are not interchangeable:

    itemId        46203349   the /VehicleDetail/<id>~US URL id
    stockNumber   45704693   what Apibara calls `lot_number`

`stock_number` is the join key to an Apibara record, verified on 8/8 overlapping
2018 A5 lots — in every case iaai.com's masked `WAUENCF5XJA******` matched the
Apibara full VIN for the same stock number. So the masked VIN is not a dead end:
pull breadth here for free, then spend Apibara calls only on the shortlist to
resolve full VINs for MarketCheck pricing.

COVERAGE — why this source exists at all
----------------------------------------
Observed 2026-08-13, "2018 Audi A5": iaai.com returned 65 lots, of which 56 were
`Auction Not Assigned` (in inventory, no sale date yet), 7 `Prebid`, 2
`Prebid/BuyNow`. Apibara's unfiltered 2018-2023 A5 open query returned 14 and
did NOT include lot 46163678~US, a 2026-08-25 sale sitting in pre-bid on the
site. Apibara has a real coverage gap on current inventory; this closes it.

PAGINATION — GET page 1, POST the rest
---------------------------------------
The search GET renders at most PageSize=100 and honours NO page parameter:
`&page=2`, `&CurrentPage=2` and `&pagesize=25` each returned byte-identical
page 1. That made the 100-row ceiling look absolute; it is not.

The site's own Knockout pager POSTs the page's `GBPSearchQuery` model back to
`/Search` with `CurrentPage` bumped:

    SearchPage.js:  QueryInvoker.Ajax("/Search", "POST", JSON.stringify(e))

So this script GETs page 1 (which carries the model and the total), then POSTs
for each remaining page. Verified on "2018 Mazda Mazda3": 137 reported, page 1
returned 100 and page 2 returned 37, with ZERO overlap and a union of exactly
137. The POST answers with the same results fragment, so every parser below
works on it unchanged.

`--max-pages` (default 20 = 2,000 lots) is a safety cap, not a target: paging
stops as soon as the reported total is covered. `truncated` now means the pager
genuinely did not reach the end — a capped run or a failed page — rather than
being implied by a full first page. `--year-range` remains useful for keeping
each query small, but is no longer required to get past 100.

MARKET SCOPE: US ONLY BY DEFAULT
---------------------------------
IAA runs the US and Canada under one site and one search, so a plain
"2018 Audi A5" query returns Canadian lots mixed in — 2 of 67 on one observed
pull. They differ end to end: prices in CAD, provinces instead of state codes,
postal codes instead of ZIPs, no branch coordinates (so no distance), no
listing-state token, and no fee model in app/fees.py.

`--market` defaults to `us` and excludes them BEFORE anything is archived or
any detail page is fetched, keyed on the item-id suffix (`~US` / `~CA`) that
step 1 already carries. `--market all` keeps everything; `--market ca` inverts.

This is the one place this script departs from "archive everything untouched",
so it does not depart quietly: every exclusion is recorded in
`queries[].excluded_by_market` and `counts.excluded_by_market` with the lot
numbers, and `search_params.market` becomes part of the archive's cohort
identity — a us-only and an all-markets archive of the same keyword do not
cover the same space, and lot_history_01 must not read absence in one against
the other.

MASKED, AND STAYING MASKED
--------------------------
`vin_mask` is the first 11 VIN characters (WAUENCF5XJA******) — enough for year,
plant, engine and trim family, NOT enough to identify the car. `Seller` and
`SellerType` are fully masked (******) and `ProviderName`/`ProviderGroup` come
back empty. All are unmasked only for IAAI's paid membership tier. Resolve VIN
and seller name via the Apibara join instead.

Masking is narrower than it looks, though, and only these are actually lost:

    VIN           attributes.VIN is masked; Apibara carries the full 17
    seller NAME   ProviderName/Seller blank; Apibara has "State Farm Group…"

Seller CLASS survives: attributes.Origin ("Insurance") and ProviderType ("INS")
are populated on 65/65 lots, and those are exactly what the flattener's
seller_class() reads — so insurance-vs-dealer filtering works web-only, it is
only the company name that needs Apibara.

Branch coordinates survive too: attributes.StorageLocationLatitude/Longitude are
populated on 65/65, so distance_mi needs no app/branch_geo.py fallback. (They
are null under view_model.inventory — that block is a null-filled client-side
template. inventoryView.attributes is the populated one.)

WHAT GETS ARCHIVED
------------------
Untouched, in this order of trust — every one is IAAI's own JSON, not scraped
text, so a markup change cannot silently corrupt it:

    search  #VehicleDetails    JSON array, one entry per lot: AuctionDate,
                               InventoryStatus, PreBid/BuyNow/TimedAuction flags
    search  #GBPSearchQuery    the server's own query model it answered with
    search  ImageModalClicked  identity tuple: stock, itemId, vinMask, branch,
                               year, make, model, series
    search  AddDelWatch        the state label (Prebid | Prebid/BuyNow |
                               Auction Not Assigned) + auction datetime
    detail  #ProductDetailsVM  the whole lot view-model, archived verbatim under
                               detail.view_model, with the parts the pipeline
                               reads lifted into detail.fields:
                                 attributes           208 keys, IAAI's own
                                 vehicle_information  11 pairs
                                 vehicle_description  15 pairs
                                 sale_information     10 pairs (ACV, repair est)
                                 images               keys[] w/h, videos[], 360

NOTE ON view_model.inventory vs inventoryView: the model carries two copies of
the lot. `inventory` is a null-filled client-side template — reading it is how
you conclude, wrongly, that coordinates and specs are missing. `inventoryView`
is the populated one, and it is what detail.fields is lifted from.

`row_text` keeps each search row's visible tokens verbatim as a list, so nothing
on the page is discarded even where this script has no name for it.

FRAGILITY
---------
This is a public web page, not a contract. The JSON blocks are the stable
anchor; the ImageModalClicked/AddDelWatch regexes are the brittle part. Every
parse failure is COUNTED and reported rather than swallowed — a run that finds
0 lots exits non-zero, because silently writing an empty archive is the one
failure mode that would quietly poison the pipeline.

robots.txt disallows /Search (and allows /VehicleDetail). Run at the operator's
discretion; --delay throttles both steps and defaults to 1.5s.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # repo root
DATA_DIR = ROOT / "analytics" / "data"

# Web search only ever exposes CURRENT inventory — sold lots are behind the paid
# membership — so every pull lands in the open bucket, next to the Apibara ones.
MODE = "open"
PLATFORM = "iaai"
SOURCE = "iaai-web"

BASE = "https://www.iaai.com"
SEARCH_PATH = "/Search"
DETAIL_PATH = "/VehicleDetail/{item_id}"

RATE_DELAY = 1.5
PAGE_SIZE = 100          # server-side ceiling; not adjustable via the GET

# A browser UA is required: the site is a browser-facing page and the default
# urllib agent gets a challenge instead of HTML.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")


def layer_dir(mode=MODE, layer="json-raw", platform=PLATFORM):
    """analytics/data/<bucket>/<layer>/<platform>/ — same layout as Apibara."""
    return DATA_DIR / mode / layer / platform


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def fetch(url, timeout=45):
    """-> (status, html). Never raises; a failed fetch is data, not a crash."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:2000]
    except Exception as e:  # noqa: BLE001
        return 0, f"__ERROR__ {e}"


def post(url, body, referer, timeout=60):
    """-> (status, html). Same never-raises contract as fetch()."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": referer,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:2000]
    except Exception as e:  # noqa: BLE001
        return 0, f"__ERROR__ {e}"


def search_url(keyword):
    return (f"{BASE}{SEARCH_PATH}?"
            + urllib.parse.urlencode({"searchkeyword": keyword}))


def search_pages(keyword, max_pages, delay):
    """Yield (page_no, status, html) for every page of one keyword.

    Page 1 is a GET; pages 2+ are POSTs of the page's own GBPSearchQuery model
    with CurrentPage bumped — which is exactly what the site's Knockout pager
    does (`QueryInvoker.Ajax("/Search","POST",JSON.stringify(e))` in
    SearchPage.js).

    This replaces the old 100-row ceiling. The GET honours no page parameter at
    all — `&page=2`, `&CurrentPage=2` and `&pagesize=25` were each tested and
    returned byte-identical page 1 — which is why the ceiling looked like a hard
    limit for so long. The POST is the only way through.
    """
    url = search_url(keyword)
    status, doc = fetch(url)
    yield 1, status, doc
    if status != 200:
        return

    model = _json_input(doc, "GBPSearchQuery")
    total = None
    m = _TOTAL_RE.search(doc)
    if m:
        total = int(m.group(1).replace(",", ""))
    per_page = (model or {}).get("PageSize") or PAGE_SIZE
    if not model or not total or total <= per_page:
        return

    pages = min(-(-total // per_page), max_pages)     # ceil, capped
    for page in range(2, pages + 1):
        time.sleep(delay)
        body = {**model, "CurrentPage": page}
        status, doc = post(f"{BASE}{SEARCH_PATH}", body, referer=url)
        yield page, status, doc
        if status != 200:
            return


def detail_url(item_id):
    return BASE + DETAIL_PATH.format(item_id=urllib.parse.quote(item_id))


# --------------------------------------------------------------------------
# parsers — every one returns a default rather than raising
# --------------------------------------------------------------------------
def _json_input(doc, element_id):
    """<input id="X" value="{escaped json}" /> -> parsed, or None."""
    m = re.search(
        r'<input[^>]*\bid="%s"[^>]*\bvalue="([\[{].*?)"\s*/>' % re.escape(element_id),
        doc, re.S)
    if not m:
        return None
    try:
        return json.loads(html.unescape(m.group(1)))
    except (ValueError, TypeError):
        return None


def _json_script(doc, element_id):
    """<script type="application/json" id="X">…</script> -> parsed, or None.

    Attribute order varies between blocks, so match on id and require the json
    type somewhere in the same tag rather than assuming a fixed order.
    """
    m = re.search(
        r'<script([^>]*\bid="%s"[^>]*)>(.*?)</script>' % re.escape(element_id),
        doc, re.S)
    if not m or "application/json" not in m.group(1):
        return None
    try:
        return json.loads(m.group(2))
    except ValueError:
        return None


_IMAGE_MODAL_RE = re.compile(
    r"ImageModalClicked\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',"
    r"\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'")

_WATCH_RE = re.compile(
    r"AddDelWatch\(this,\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',"
    r"\s*'([^']*)'\)")

_TOTAL_RE = re.compile(r'id="TotalVehicleAmount"[^>]*>\s*([\d,]+)')


def parse_identity(doc):
    """itemId -> the identity tuple IAAI passes to its own image modal."""
    out = {}
    for stock, item_id, vin_mask, branch, year, make, model, series in \
            _IMAGE_MODAL_RE.findall(doc):
        out[item_id] = {
            "item_id": item_id,
            "stock_number": stock,          # <- the Apibara lot_number join key
            "vin_mask": vin_mask,
            "branch_code": branch,
            "year": int(year) if year.isdigit() else None,
            "make": make,
            "model": model,
            "series": series,
        }
    return out


def parse_states(doc):
    """itemId -> the listing state label + the auction datetime beside it."""
    out = {}
    for item_id, branch, when, actn_ln_id, state in _WATCH_RE.findall(doc):
        out[item_id] = {
            "state": html.unescape(state),
            "auction_datetime_raw": html.unescape(when),
            "actn_ln_id": actn_ln_id,
            "branch_code": branch,
        }
    return out


def clean_text(fragment):
    fragment = re.sub(r"<(script|style|svg)\b.*?</\1>", "", fragment, flags=re.S)
    # IAAI nests whole tags inside attribute values (data-original-title holds an
    # <img> for the hover preview). Naive tag stripping ends the tag at that
    # inner '>', spilling the rest of the attributes out as visible text — so
    # drop any attribute whose value contains markup before stripping.
    fragment = re.sub(r'\s[a-zA-Z-]+="[^"]*<[^"]*"', "", fragment)
    text = re.sub(r"<[^>]+>", "\x00", fragment)
    parts = (html.unescape(p).strip() for p in text.split("\x00"))
    return [re.sub(r"\s+", " ", p) for p in parts if p.strip()]


def parse_row_text(doc, item_id):
    """The visible tokens of one result row, verbatim and in order.

    Deliberately un-named: this stage does not decide what "76,471 mi" means,
    it only guarantees the value survives into the archive.
    """
    anchor = doc.find(f'href="/VehicleDetail/{item_id}"')
    if anchor == -1:
        return []
    # Start after the anchor tag closes, or the tail of that tag's own
    # attributes becomes the first "visible" token.
    start = doc.find(">", anchor)
    start = anchor if start == -1 else start + 1
    nxt = doc.find('class="table-row table-row-border"', start)
    end = nxt if nxt != -1 else start + 14000
    return clean_text(doc[start:end])[:80]


def parse_search(doc):
    """One search page -> (records, meta). Records keyed by itemId."""
    identity = parse_identity(doc)
    states = parse_states(doc)
    details = _json_input(doc, "VehicleDetails") or []
    by_id = {d.get("Id"): d for d in details if isinstance(d, dict)}

    total = None
    m = _TOTAL_RE.search(doc)
    if m:
        total = int(m.group(1).replace(",", ""))

    # Union of every id any parser saw, so a regex that misses one lot shows up
    # as a null section rather than a vanished row.
    ids = list(by_id) or list(identity)
    for i in identity:
        if i not in ids:
            ids.append(i)

    records = []
    for item_id in ids:
        records.append({
            "item_id": item_id,
            "stock_number": (identity.get(item_id) or {}).get("stock_number"),
            "identity": identity.get(item_id),
            "listing": by_id.get(item_id),
            "state": states.get(item_id),
            "row_text": parse_row_text(doc, item_id),
            "detail_url": detail_url(item_id) if item_id else None,
            "detail": None,          # filled by step 2
        })

    meta = {
        "total_reported": total,
        "returned": len(records),
        "query_model": _json_input(doc, "GBPSearchQuery"),
        "missing_identity": sum(1 for r in records if not r["identity"]),
        "missing_listing": sum(1 for r in records if not r["listing"]),
        "missing_state": sum(1 for r in records if not r["state"]),
    }
    return records, meta


def parse_detail(doc):
    """A lot page -> its ProductDetailsVM, plus the flattened key/value sections.

    The view-model carries two copies of the data: `inventory`, a null-filled
    client-side template, and `inventoryView`, the populated one. Only the
    latter is flattened; the whole model is archived regardless.
    """
    vm = _json_script(doc, "ProductDetailsVM")
    if vm is None:
        # A removed lot answers HTTP **200** with a DetailsNotFoundView shell and
        # no view-model — verified on lot 45250068 the day after it sold via Buy
        # Now (200, 115KB, no ProductDetailsVM, vs ~360KB for a live lot). So
        # status code cannot be used to detect removal; absence of the model can.
        if "DetailsNotFoundView" in doc:
            return {"gone": True, "reason": "DetailsNotFoundView",
                    "view_model": None, "fields": {}}
        return None

    def section(node):
        if not isinstance(node, dict):
            return {}
        values = node.get("$values")
        if not isinstance(values, list):
            return {}
        return {it.get("key"): it.get("value") for it in values
                if isinstance(it, dict) and it.get("key")}

    view = vm.get("inventoryView") or {}
    images = view.get("imageDimensions") or {}

    def unref(node):
        """$values list -> plain list, dropping JSON.NET's $id bookkeeping."""
        vals = (node or {}).get("$values") if isinstance(node, dict) else None
        if not isinstance(vals, list):
            return []
        return [{k: v for k, v in it.items() if not str(k).startswith("$")}
                if isinstance(it, dict) else it for it in vals]

    return {
        "view_model": vm,
        "fields": {
            "vehicle_information": section(view.get("vehicleInformation")),
            "vehicle_description": section(view.get("vehicleDescription")),
            "sale_information": section(view.get("saleInformation")),
            # The 208-key block IAAI populates for its own page. It is the
            # richest thing on the lot and carries the fields the pipeline
            # actually keys on — StorageLocation lat/lng, Origin/ProviderType,
            # ODOValue, EstRepairCost, damage codes — so it is surfaced here
            # rather than left buried in view_model.
            "attributes": {k: v for k, v in (view.get("attributes") or {}).items()
                           if not str(k).startswith("$")},
            "images": {
                "keys": unref(images.get("keys")),
                "videos": unref(images.get("videos")),
                "image_360_url": images.get("image360Url") or None,
                "vrd_url": images.get("vrdUrl") or None,
                "undercarriage": images.get("undercarriageInd"),
                "deep_zoom": images.get("deepZoomInd"),
            },
        },
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def multiword(value):
    """nargs='+' -> one string, so `--model ES 350` needs no quoting."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return " ".join(str(x) for x in value).strip()


def parse_year_range(s):
    m = re.fullmatch(r"\s*(\d{4})\s*(?:[-:]\s*(\d{4}))?\s*", s or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"--year-range wants YYYY-YYYY or YYYY, got {s!r}")
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    if hi < lo:
        raise argparse.ArgumentTypeError(f"--year-range {lo}-{hi} is backwards")
    return lo, hi


def build_keywords(args):
    """-> [(keyword, label)]. One keyword per request.

    --year-range fans out into one query per year, because a single query is
    capped at 100 rows server-side and cannot be paged (see module docstring).
    """
    if args.keyword:
        kw = multiword(args.keyword)
        return [(kw, kw)]

    stem = " ".join(x for x in (multiword(args.make), multiword(args.model)) if x)
    if not stem:
        raise SystemExit(
            "nothing to search for: pass --keyword, or --make/--model")
    if not args.year_range:
        return [(stem, stem)]
    lo, hi = args.year_range
    return [(f"{y} {stem}", f"{y} {stem}") for y in range(lo, hi + 1)]


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="pull_iaai_web_01.py",
        description="Pull raw iaai.com listing JSON into data/open/json-raw/iaai/. "
                    "No API key, no quota. No filtering, no derived fields.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="One search request per year with --year-range; one more per lot "
               "with --details. Use --dry-run to see the URLs first.")
    ap.add_argument("--keyword", nargs="+",
                    help='raw search phrase, e.g. --keyword 2018 Audi A5')
    ap.add_argument("--make", nargs="+", metavar="MAKE")
    ap.add_argument("--model", nargs="+", metavar="MODEL")
    ap.add_argument("--year-range", type=parse_year_range, metavar="YYYY-YYYY",
                    help="fans out into one query per year (100-row cap)")
    ap.add_argument("--details", action="store_true",
                    help="step 2: also GET each lot page (ACV, repair estimate, "
                         "title doc). One request per lot")
    ap.add_argument("--max-details", type=int, default=0, metavar="N",
                    help="cap step 2 at N lots (default: all)")
    ap.add_argument("--max-pages", type=int, default=20, metavar="N",
                    help="safety cap on search pages per keyword (default 20 = "
                         "2,000 lots). Page 1 is a GET, pages 2+ are POSTs")
    ap.add_argument("--market", choices=list(MARKETS), default="us", type=str.lower,
                    help="which IAA marketplace to archive (default: us). "
                         "Canadian lots price in CAD and have no fee model — "
                         "'all' keeps them, and the archive always records what "
                         "was excluded")
    ap.add_argument("--details-state", action="append", default=[], metavar="STATE",
                    help="step 2 only for these listing states; implies "
                         "--details. Repeatable or comma-separated. Use "
                         "'assigned' for everything except Auction Not "
                         "Assigned — on a 65-lot A5 pull that is 9 requests "
                         "instead of 65")
    ap.add_argument("--delay", type=float, default=RATE_DELAY,
                    help=f"seconds between requests (default {RATE_DELAY})")
    ap.add_argument("--keep-html", action="store_true",
                    help="also archive the raw HTML (large: ~2MB per search "
                         "page, ~350KB per lot page)")
    ap.add_argument("--out", help="output basename or path (default: auto-named)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the URLs that would be fetched, fetch nothing")
    return ap


UNASSIGNED = "auction not assigned"

# IAA runs the US and Canada under one site and ONE search, so a plain
# "2018 Audi A5" query returns Canadian lots mixed in. They are a different
# proposition end to end — prices in CAD, provinces instead of state codes,
# postal codes instead of ZIPs, no branch coordinates, no listing-state token,
# and no fee model in app/fees.py — so the default scope is US only.
#
# This is a market SCOPE, not an analytical filter: it decides which of IAA's
# two marketplaces the archive covers, the way a make/model does. What it is
# not is free — a lot excluded here never reaches json-raw, so the archive
# records the exclusion (count and lot ids) rather than pretending the search
# returned less than it did. Use --market all to keep everything.
MARKETS = {"us": {"US"}, "ca": {"CA"}, "all": None}


def tenant_of(item_id):
    """'Imp_3069335~CA' -> 'CA'. Available from step 1, no detail fetch needed."""
    s = str(item_id or "")
    return s.rsplit("~", 1)[-1].upper() if "~" in s else ""


def wanted_states(values):
    """--details-state values -> a set of lowercased state names, or None.

    'assigned' is the useful one and is expanded at match time rather than to a
    fixed list, so a state IAAI adds later (as TimedAuction appeared) is
    included automatically instead of being silently skipped.
    """
    out = set()
    for raw in values or []:
        for tok in str(raw).split(","):
            tok = tok.strip().lower()
            if tok:
                out.add(tok)
    return out or None


def state_matches(state, wanted):
    if not wanted:
        return True
    s = str(state or "").strip().lower()
    if "assigned" in wanted and s and s != UNASSIGNED:
        return True
    return s in wanted


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def resolve_out_path(args, keywords):
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    if args.out:
        p = Path(args.out)
        if not p.suffix:
            p = p.with_suffix(".json")
        return p if p.is_absolute() else layer_dir() / p

    label = slugify(keywords[0][1])
    if len(keywords) > 1 and args.year_range:
        # "audi_a5_2019_2023" beats "2023_audi_a5_x5" — the stem stays put and
        # the range is readable, so the file sorts next to its siblings.
        lo, hi = args.year_range
        stem = " ".join(x for x in (multiword(args.make), multiword(args.model)) if x)
        label = f"{slugify(stem)}_{lo}_{hi}"
    elif len(keywords) > 1:
        label = f"{slugify(keywords[0][1])}_x{len(keywords)}"
    return layer_dir() / f"iaaiweb_{PLATFORM}_{MODE}_{label}_{stamp}.json"


# --------------------------------------------------------------------------
def summarize(records):
    states, years, branches = {}, {}, {}
    detailed = acv = 0
    for r in records:
        st = (r.get("state") or {}).get("state") or "—"
        states[st] = states.get(st, 0) + 1
        ident = r.get("identity") or {}
        y = ident.get("year")
        if y:
            years[y] = years.get(y, 0) + 1
        b = ident.get("branch_code") or "—"
        branches[b] = branches.get(b, 0) + 1
        if r.get("detail"):
            detailed += 1
            if (r["detail"].get("fields", {})
                    .get("sale_information", {}).get("ActualCashValue")):
                acv += 1

    dated = sorted((r["listing"] or {}).get("AuctionDate", "")[:10]
                   for r in records if (r.get("listing") or {}).get("AuctionDate"))
    print(f"\n  records:      {len(records)}")
    print(f"  states:       {dict(sorted(states.items(), key=lambda kv: -kv[1]))}")
    if dated:
        print(f"  sale dates:   {dated[0]} .. {dated[-1]}   "
              f"({len(records) - len(dated)} unassigned)")
    if years:
        print(f"  years:        {dict(sorted(years.items()))}")
    print(f"  branches:     {len(branches)} distinct")
    joinable = sum(1 for r in records if r.get("stock_number"))
    print(f"  stock #s:     {joinable}/{len(records)} present "
          f"(the Apibara lot_number join key)")
    if detailed:
        print(f"  detail pages: {detailed} fetched, {acv} with ActualCashValue")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    want_states = wanted_states(args.details_state)
    if want_states:
        args.details = True          # asking WHICH lots implies wanting details
    keywords = build_keywords(args)
    out_path = resolve_out_path(args, keywords)

    print("=" * 78)
    print(f"IAAI web ({BASE}) — {len(keywords)} search request(s)")
    for _, label in keywords:
        print(f"    {label}")
    print(f"  market:      {args.market}"
          + ("  (Canadian lots excluded before archiving)"
             if args.market == "us" else ""))
    print(f"  step 2:      {'ON — one request per lot' if args.details else 'off (--details)'}"
          + (f"  limited to {sorted(want_states)}" if want_states else ""))
    print(f"  page cap:    {PAGE_SIZE} lots/query, no server-side paging")
    print("=" * 78)

    if args.dry_run:
        print("\n  DRY RUN — nothing fetched.")
        for kw, _ in keywords:
            print(f"  GET {search_url(kw)}")
        if args.details:
            print(f"  GET {detail_url('<itemId>~US')}   (per lot)")
        print(f"  would write -> {out_path}")
        return 0

    out = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": argv,
        "platform": PLATFORM,
        "source": SOURCE,
        "mode": MODE,
        "search_params": {
            "keywords": [kw for kw, _ in keywords],
            "page_size": PAGE_SIZE,
            "details": bool(args.details),
            # Part of the cohort identity: a us-only archive and an all-markets
            # archive of the same keyword do not cover the same space, so
            # absence in one cannot be read against the other.
            "market": args.market,
        },
        "queries": [],
    }

    by_id, requests_made, truncated = {}, 0, False
    keep_tenants = MARKETS[args.market]
    skipped_market = {}

    # ---- step 1 ----------------------------------------------------------
    for n, (kw, label) in enumerate(keywords, 1):
        url = search_url(kw)
        entry = {"keyword": kw, "url": url, "status": None, "pages": []}
        records, meta, failed = [], {}, False

        for page_no, status, doc in search_pages(kw, args.max_pages, args.delay):
            requests_made += 1
            entry["pages"].append({"page": page_no, "status": status,
                                   "method": "GET" if page_no == 1 else "POST"})
            entry["status"] = entry["status"] or status
            if status != 200:
                entry["error"] = doc[:500]
                failed = failed or page_no == 1
                print(f"  [{n}/{len(keywords)}] {label:<28} page {page_no}: "
                      f"HTTP {status}  FAILED")
                break
            page_recs, page_meta = parse_search(doc)
            # Which search found this lot. With --year-range one archive holds
            # several searches, and absence is only meaningful within the search
            # that could have returned the lot — a "2019 Audi A5" page proves
            # nothing about a 2018 lot.
            for r in page_recs:
                r["keyword"] = kw
            records.extend(page_recs)
            meta = meta or page_meta
            meta["total_reported"] = page_meta.get("total_reported") or meta.get("total_reported")
            if args.keep_html:
                entry.setdefault("html", []).append(doc)

        if failed or not records:
            out["queries"].append(entry)
            if n < len(keywords):
                time.sleep(args.delay)
            continue

        # de-dupe defensively: pages are disjoint in practice (0 overlap across
        # 137 Mazda3 lots), but a listing shifting between requests could repeat
        seen_ids, deduped = set(), []
        for r in records:
            if r["item_id"] in seen_ids:
                continue
            seen_ids.add(r["item_id"])
            deduped.append(r)
        records = deduped
        meta["returned"] = len(records)
        meta["pages_fetched"] = len(entry["pages"])

        # Market scope, applied before anything is archived or fetched. Keyed on
        # the item-id suffix, which step 1 already carries — so excluded lots
        # cost no detail request either.
        if keep_tenants is not None:
            excluded = [r for r in records
                        if tenant_of(r["item_id"]) and
                        tenant_of(r["item_id"]) not in keep_tenants]
            records = [r for r in records if r not in excluded]
            if excluded:
                by_t = {}
                for r in excluded:
                    by_t.setdefault(tenant_of(r["item_id"]), []).append(
                        r.get("stock_number") or r["item_id"])
                entry["excluded_by_market"] = by_t
                skipped_market.update({k: skipped_market.get(k, 0) + len(v)
                                       for k, v in by_t.items()})

        entry.update(meta)
        entry["returned"] = len(records)
        # Truncation now means the pager did not reach the end — either
        # --max-pages capped it or a page failed. It is no longer implied by a
        # full first page, since pages 2+ are reachable.
        got, want = len(records) + sum(len(v) for v in
                                       (entry.get("excluded_by_market") or {}).values()), \
            meta.get("total_reported")
        entry["truncated"] = bool(want and got < want)
        truncated = truncated or entry["truncated"]
        out["queries"].append(entry)

        for r in records:
            by_id.setdefault(r["item_id"], r)

        flag = "  *** TRUNCATED ***" if entry["truncated"] else ""
        total = meta["total_reported"]
        drop = entry.get("excluded_by_market")
        pg = meta.get("pages_fetched", 1)
        print(f"  [{n}/{len(keywords)}] {label:<28} {len(records):>3} lot(s)"
              f"   site total: {total}   pages: {pg}{flag}")
        if drop:
            for t, lots in sorted(drop.items()):
                print(f"        excluded {len(lots)} {t} lot(s) "
                      f"(--market {args.market}): {', '.join(map(str, lots[:8]))}")
        if meta["missing_identity"] or meta["missing_listing"]:
            print(f"        parse gaps: identity={meta['missing_identity']} "
                  f"listing={meta['missing_listing']} state={meta['missing_state']}")
        if n < len(keywords):
            time.sleep(args.delay)

    records = list(by_id.values())
    if not records:
        raise SystemExit(
            "\nNo lots parsed from any query. Either the search genuinely has no "
            "results, or iaai.com changed its markup — check the archived "
            "queries[].status and re-run with --keep-html to inspect.")

    # ---- step 2 ----------------------------------------------------------
    detail_failures = 0
    if args.details:
        targets = [r for r in records
                   if state_matches((r.get("state") or {}).get("state"), want_states)]
        if want_states:
            print(f"\n  --details-state {sorted(want_states)}: "
                  f"{len(targets)} of {len(records)} lot(s) match")
        if args.max_details > 0:
            targets = targets[:args.max_details]
        print(f"\n  step 2: fetching {len(targets)} lot page(s) "
              f"at {args.delay}s intervals "
              f"(~{int(len(targets) * args.delay / 60) + 1} min)")
        for n, r in enumerate(targets, 1):
            status, doc = fetch(r["detail_url"])
            requests_made += 1
            parsed = parse_detail(doc) if status == 200 else None
            if parsed is not None and parsed.get("gone"):
                # Not a failure: the lot left the site between the search page
                # and this fetch. Recorded so the archive says so explicitly.
                r["detail_gone"] = parsed["reason"]
                r["detail"] = None
            elif parsed is None:
                detail_failures += 1
                r["detail_error"] = {"status": status, "body": doc[:300]}
            else:
                if not args.keep_html:
                    parsed.pop("raw_html", None)
                r["detail"] = parsed
            if args.keep_html and status == 200:
                r["detail_html"] = doc
            if n % 10 == 0 or n == len(targets):
                print(f"        {n}/{len(targets)}   failures: {detail_failures}")
            if n < len(targets):
                time.sleep(args.delay)

    out["records"] = records
    out["counts"] = {
        "records": len(records),
        "requests": requests_made,
        "queries": len(keywords),
        "details_fetched": sum(1 for r in records if r.get("detail")),
        "detail_failures": detail_failures,
        "truncated": truncated,
        "market": args.market,
        "excluded_by_market": skipped_market,
    }

    summarize(records)
    if truncated:
        print(f"\n  *** One or more queries hit the {PAGE_SIZE}-lot ceiling. "
              f"Slice finer (--year-range, or a narrower --keyword) — the site "
              f"has more lots than this archive contains. ***")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print("\n" + "=" * 78)
    print(f"Done. {requests_made} HTTP request(s), 0 API quota used.")
    print(f"  JSON -> {out_path}")
    if not args.details:
        print("  tip: re-run with --details for ACV / estimated repair / title doc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
