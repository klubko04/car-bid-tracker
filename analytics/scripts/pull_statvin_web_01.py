"""stat.vin search pull — stage 1 of the Copart seller/VIN enrichment path.

    pull_statvin_web_01.py
        -> data/open/json-raw/copart/statvin_copart_open_*.json
    copart_statvin_enrich_01.py
        -> fills seller class and full VIN on adapted Copart records

The folder axis is the auction house, not the vendor, so this lands beside
``apibara_*`` and ``copartweb_*`` under ``copart/``; the filename prefix is what
separates the sources.

WHAT THIS SOURCE ACTUALLY PUBLISHES
-----------------------------------
The operator-visible search page is the whole contract:

    https://stat.vin/search-auto?make=Audi&model=A5&auction[]=2
                                &year_from=2018&year_to=2023&page=N

``auction[]=2`` is Copart, and ``model`` wants stat.vin's own option value
(``S5_group_id_24870``), not a bare name -- a bare name matches for some models
and silently returns an empty page for others.

Note that stat.vin echoes COPART'S MODEL GROUP, so an S5 search returns cards
titled "AUDI S5/RS5" and mixes both models. That is exactly the shared group
that forced the exact-MODL facet in pull_copart_web_01.py. It is harmless here
because nothing downstream trusts this source's model: the join key is the
Copart lot number, and the enricher validates year and VIN prefix before
accepting anything.

Each page carries 20 lot cards, and every card's
photo ``title`` attribute is a complete, structured identity record:

    title="AUDI A5 2022. Lot# 62253796. VIN WAUSAAF56NA027846. Auction COPART"

That gives the Copart lot number -- the join key this repo already uses
everywhere -- plus the FULL VIN. Copart's own public surface masks the VIN
(``WAUSAAF56NA******``), so this is the only non-APIBara source in the pipeline
that can complete one.

Alongside it each card carries a seller **type**, not a seller name:

    Insurance / Insurance company
    Dealer / Non-insurance

This matters and is easy to misread. Copart publishes a *name* on a minority of
lots (25% for S5, 46% for RS5) and never a type. stat.vin publishes a *type* on
every lot and never a name. They are complementary, not redundant, which is why
the enricher treats Copart's name as the senior source and this as the next one
down: a name identifies the carrier, a type only bins it.

Measured against the existing archives on the first 20-lot A5 page:

    stat.vin type vs Copart's named carrier   9/9 agree
    stat.vin type vs APIBara seller.type     16/17 agree
    stat.vin VIN vs Copart's masked prefix   19/19 agree, 0 conflicts
    seller coverage                          20/20, vs 9/20 and 17/20

WHY THIS GOES THROUGH A BROWSER
-------------------------------
``/search-auto`` answers a stdlib HTTP request with a Cloudflare interstitial
("Just a moment...", HTTP 403) no matter how complete the headers are. This
script therefore renders the same URL in the operator's own dedicated
debugging Chrome profile, via browser_fetch_page_01.ps1.

That is not a way around the check. The check still runs, in a real browser,
exactly as it does for a person clicking the same link. Nothing here forges a
token, solves a challenge, or retries to wear one down -- a page that does not
render is recorded as a failure and the run stops.

robots.txt (fetched 2026-08-20) allows this path and forbids others:

    Allow: /                      <- /search-auto is permitted
    Disallow: /vin/               <- per-lot detail pages are NOT
    Disallow: */ajax/  /public/  /livewire/   <- the XHR endpoints are NOT

So the search listing is the sanctioned surface, and it happens to be the one
that carries everything needed. This script reads ONLY ``/search-auto`` and
never follows a ``/vin/`` link. Keep it that way.

Be a considerate client: the default delay is one page every 6 seconds, an A5
cohort is two pages, and there is no reason to re-pull within a session.

Examples:

    python analytics/scripts/pull_statvin_web_01.py --model A5
    python analytics/scripts/pull_statvin_web_01.py --model S5 --max-pages 3
    python analytics/scripts/pull_statvin_web_01.py --model A5 --dry-run
    python analytics/scripts/pull_statvin_web_01.py --model A5 \
        --html 1=/tmp/page1.html          # offline reparse, no browser
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as html_lib
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "analytics" / "scripts"
DATA_DIR = ROOT / "analytics" / "data"
FETCH_SCRIPT = SCRIPTS / "browser_fetch_page_01.ps1"
START_SCRIPT = SCRIPTS / "start_copart_browser_01.ps1"

BASE = "https://stat.vin"
SEARCH_PATH = "/search-auto"
PLATFORM = "copart"
SOURCE = "statvin-search"
MODE = "open"
PAGE_SIZE = 20
RATE_DELAY = 6.0

# stat.vin's auction selector. Only Copart is wired up: the rest of this
# pipeline keys on a Copart lot number, so an IAAI row would have nothing to
# join to until an IAAI-side enricher exists.
AUCTION_COPART = "2"

DEFAULT_MAKE = "Audi"
DEFAULT_YEARS = (2018, 2023)

CARD_SPLIT = re.compile(r'(?=<div class="app-box app-listing-card")')
# The photo title is the only place identity appears already parsed.
# The vehicle portion is deliberately permissive: stat.vin echoes COPART'S
# MODEL GROUP, so an S5 search titles its cards "AUDI S5/RS5". The slash broke
# an earlier character class. Everything after it is anchored, so `.+?` is safe.
CARD_TITLE = re.compile(
    r'title="(.+?)\s+(\d{4})\.\s*Lot#\s*(\d+)\.\s*'
    r'VIN\s*([A-HJ-NPR-Z0-9]{17})\.\s*Auction\s+([A-Z]+)"'
)
SELLER_BLOCK = re.compile(r"Seller:\s*</div>(.*?)</div>\s*</div>", re.S)
TOTAL_RESULTS = re.compile(r"in total\s+([\d\s,]+)\s+results", re.I)
SVG = re.compile(r"<svg\b.*?</svg>", re.S | re.I)
SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()


def clean(value):
    text = re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()
    return text or None


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def search_url(make, model, year_from, year_to, page=1):
    params = [("make", make), ("model", model), ("auction[]", AUCTION_COPART),
              ("year_from", str(year_from)), ("year_to", str(year_to))]
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE}{SEARCH_PATH}?" + urllib.parse.urlencode(params)


def visible_text(fragment):
    return clean(TAG.sub(" ", SVG.sub(" ", fragment or "")))


def parse_seller(card):
    """-> (raw label, class token). 'Insurance Insurance company' -> insurance."""
    match = SELLER_BLOCK.search(SVG.sub(" ", card))
    raw = visible_text(match.group(1)) if match else None
    if not raw:
        return None, None
    head = raw.split(" ")[0].casefold()
    if head.startswith("insur"):
        return raw, "insurance"
    if head.startswith("dealer"):
        return raw, "dealer"
    # A new badge value is data, not something to force into a known bin.
    return raw, None


def parse_page(document, page=None, url=None):
    """Parse one rendered search page into lot records. Never raises."""
    body = SCRIPT.sub(" ", document or "")
    total = None
    match = TOTAL_RESULTS.search(TAG.sub(" ", body))
    if match:
        try:
            total = int(re.sub(r"[^\d]", "", match.group(1)))
        except ValueError:
            total = None
    records, skipped = [], []
    for card in CARD_SPLIT.split(body)[1:]:
        identity = CARD_TITLE.search(card)
        if not identity:
            skipped.append({"reason": "no_identity_title"})
            continue
        vehicle, year, lot, vin, auction = identity.groups()
        seller_raw, seller_class = parse_seller(card)
        records.append({
            "lot_number": lot,
            "vin": vin if VIN_RE.match(vin) else None,
            "year": int(year),
            "vehicle": clean(vehicle),
            "auction": clean(auction),
            "seller_label": seller_raw,
            "seller_class": seller_class,
            "page": page,
            "page_url": url,
        })
    return {"records": records, "skipped": skipped, "total_results": total,
            "cards_seen": len(records) + len(skipped)}


def ensure_browser():
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", windows_path(START_SCRIPT)],
        check=True, capture_output=True, text=True,
    )


def windows_path(path):
    result = subprocess.run(["wslpath", "-w", str(Path(path).resolve())],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


# A rendered results page carries either lot cards or the "in total N results"
# banner. The ~6.5 KB pre-hydration shell carries neither, and treating it as a
# result meant reporting an empty cohort for a search that actually had lots --
# the failure looked identical to a genuinely empty cohort.
def page_is_rendered(document):
    if not document:
        return False
    if 'app-box app-listing-card' in document:
        return True
    return bool(TOTAL_RESULTS.search(TAG.sub(" ", document)))


def fetch_rendered(url, destination, timeout_seconds=45, settle_seconds=3,
                   attempts=3):
    """Render one URL in the operator's debugging Chrome and return its HTML.

    Retries with a longer settle window when the page comes back unhydrated.
    This is a slow client, not a persistent one: the escalation is bounded and
    a still-unrendered page is reported as a failure, not as zero lots.
    """
    document = ""
    for attempt in range(1, attempts + 1):
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", windows_path(FETCH_SCRIPT),
             "-Url", url, "-Out", windows_path(destination),
             "-TimeoutSeconds", str(timeout_seconds),
             "-SettleSeconds", str(settle_seconds * attempt)],
            check=True, capture_output=True, text=True,
        )
        document = destination.read_text(encoding="utf-8", errors="replace")
        if page_is_rendered(document) or is_challenge(document):
            return document
        if attempt < attempts:
            print(f"      page not hydrated ({len(document)} bytes) — "
                  f"retry {attempt + 1}/{attempts} with a longer settle")
            time.sleep(2.0 * attempt)
    return document


def is_challenge(document):
    lowered = str(document or "").casefold()
    return "just a moment" in lowered or "cf-challenge" in lowered


def parse_html_args(values):
    output = {}
    for value in values or []:
        page, separator, filename = str(value).partition("=")
        if not separator or not page.isdigit():
            raise ValueError("--html wants PAGE=FILE")
        path = Path(filename).expanduser()
        if not path.is_file():
            raise ValueError(f"--html file not found: {path}")
        output[int(page)] = path
    return output


def parse_year_range(value):
    match = re.fullmatch(r"\s*(\d{4})\s*(?:[-:]\s*(\d{4}))?\s*", value or "")
    if not match:
        raise argparse.ArgumentTypeError("--year-range wants YYYY or YYYY-YYYY")
    low, high = int(match.group(1)), int(match.group(2) or match.group(1))
    if high < low:
        raise argparse.ArgumentTypeError(f"--year-range {low}-{high} is backwards")
    return low, high


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pull_statvin_web_01.py",
        description="Archive stat.vin Copart search pages for seller/VIN enrichment.",
    )
    parser.add_argument("--make", default=DEFAULT_MAKE)
    parser.add_argument("--model", required=True,
                        help="stat.vin model option value, e.g. A5_group_id_24918, "
                             "S5_group_id_24870, RS5_group_id_24931. A bare name "
                             "sometimes works and sometimes silently returns an "
                             "empty page -- prefer the site's own option value")
    parser.add_argument("--year-range", type=parse_year_range, default=DEFAULT_YEARS,
                        metavar="YYYY-YYYY")
    parser.add_argument("--max-pages", type=int, default=25, metavar="N",
                        help="safety cap (default: 25 = 500 lots)")
    parser.add_argument("--delay", type=float, default=RATE_DELAY,
                        help=f"seconds between page renders (default: {RATE_DELAY})")
    parser.add_argument("--html", action="append", default=[], metavar="PAGE=FILE",
                        help="reparse a saved page offline instead of rendering it")
    parser.add_argument("--keep-html", action="store_true",
                        help="retain rendered HTML beside the archive")
    parser.add_argument("--out", help="output path (default: auto-named)")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def layer_dir():
    return DATA_DIR / MODE / "json-raw" / PLATFORM


def resolve_out_path(args):
    low, high = args.year_range
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    if args.out:
        path = Path(args.out)
        if not path.suffix:
            path = path.with_suffix(".json")
        return path if path.is_absolute() else layer_dir() / path
    label = f"{slugify(args.make)}_{slugify(args.model)}_{low}_{high}"
    return layer_dir() / f"statvin_{PLATFORM}_{MODE}_{label}_{stamp}.json"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1")
    low, high = args.year_range
    out_path = resolve_out_path(args)
    try:
        saved_pages = parse_html_args(args.html)
    except ValueError as error:
        raise SystemExit(str(error)) from None

    print("=" * 78)
    print(f"STAT.VIN — Copart {args.make} {args.model} {low}-{high}")
    print(f"  url:      {search_url(args.make, args.model, low, high)}")
    print(f"  robots:   /search-auto allowed; /vin/ and ajax endpoints are not")
    print(f"  transport:{'saved HTML (offline)' if saved_pages else ' rendered in the debugging Chrome profile'}")
    print("=" * 78)

    if args.dry_run:
        print("\n  DRY RUN — nothing fetched.")
        for page in (1, 2, 3):
            print(f"  GET {search_url(args.make, args.model, low, high, page)}")
        print(f"  would write -> {out_path}")
        return 0

    archive = {
        "generated_at": now_iso(), "argv": argv, "platform": PLATFORM,
        "source": SOURCE, "mode": MODE,
        "search_params": {
            "make": args.make, "model": args.model, "year_min": low,
            "year_max": high, "auction": "copart", "page_size": PAGE_SIZE,
            "robots_policy": "search-auto only; /vin/ never requested",
            "transport": "saved_html" if saved_pages else "operator_browser_render",
        },
        "pages": [], "records": [],
    }

    if not saved_pages:
        try:
            ensure_browser()
        except subprocess.CalledProcessError as error:
            raise SystemExit(
                "could not reach the debugging Chrome profile:\n"
                f"{error.stderr or error.stdout}"
            ) from None

    html_dir = out_path.parent / f"{out_path.stem}_html"
    if args.keep_html or not saved_pages:
        html_dir.mkdir(parents=True, exist_ok=True)

    by_lot = {}
    total_results = None
    cards_seen = 0
    duplicates = 0
    page = 1
    while page <= args.max_pages:
        url = search_url(args.make, args.model, low, high, page)
        entry = {"page": page, "url": url}
        try:
            if page in saved_pages:
                document = saved_pages[page].read_text(encoding="utf-8", errors="replace")
                entry["transport"] = "saved_html"
            elif saved_pages:
                break  # offline mode: only reparse what was supplied
            else:
                document = fetch_rendered(url, html_dir / f"page_{page:03d}.html")
                entry["transport"] = "browser_render"
        except subprocess.CalledProcessError as error:
            entry.update(status="render_failed",
                         error=(error.stderr or error.stdout or "")[:400])
            archive["pages"].append(entry)
            print(f"  [{page}] render FAILED — stopping")
            break
        entry["sha256"] = sha256_text(document)
        if is_challenge(document):
            # A challenge is a stop condition, not something to work around.
            entry.update(status="challenged")
            archive["pages"].append(entry)
            print(f"  [{page}] Cloudflare challenge — stopping. "
                  "Open the URL in the debugging Chrome window once, then re-run.")
            break

        if not page_is_rendered(document):
            entry.update(status="not_rendered", bytes=len(document))
            archive["pages"].append(entry)
            print(f"  [{page}] page never hydrated ({len(document)} bytes) — stopping")
            break

        parsed = parse_page(document, page=page, url=url)
        total_results = parsed["total_results"] or total_results
        entry.update(status="ok", cards=parsed["cards_seen"],
                     parsed=len(parsed["records"]), skipped=len(parsed["skipped"]),
                     total_results=parsed["total_results"])
        archive["pages"].append(entry)
        cards_seen += parsed["cards_seen"]
        for record in parsed["records"]:
            if record["lot_number"] in by_lot:
                # The listing re-orders between requests, so a lot can land on
                # two consecutive pages. That is a duplicate, not a shortfall.
                duplicates += 1
                continue
            by_lot[record["lot_number"]] = record
        print(f"  [{page}] {len(parsed['records'])}/{parsed['cards_seen']} card(s) parsed"
              f"   running total {len(by_lot)}"
              + (f" of {total_results}" if total_results else ""))

        if not parsed["records"]:
            break
        if total_results is not None and cards_seen >= total_results:
            break
        if len(parsed["records"]) < PAGE_SIZE:
            break
        page += 1
        time.sleep(max(0.0, args.delay))

    records = list(by_lot.values())
    if not records:
        hint = ""
        if any(p.get("status") == "not_rendered" for p in archive["pages"]):
            hint = ("\n\nThe page never hydrated. That is a render problem, not "
                    "an empty cohort -- the browser profile may be busy. Re-run; "
                    "the saved HTML beside the archive shows what came back.")
        elif "_group_id_" not in args.model:
            hint = (f"\n\n--model {args.model!r} is a bare name. stat.vin's own "
                    "option values look like 'S5_group_id_24870'; a bare name "
                    "matches for some models and silently returns an empty page "
                    "for others. Read the model <select> on the search page and "
                    "pass that value.")
        raise SystemExit(
            "\nNo lots were parsed. Either the cohort is empty, the page markup "
            "changed, or every render was challenged. The raw HTML is kept beside "
            "the archive so the contract can be diffed." + hint
        )

    archive["records"] = records
    # Absent and unrecognised are different facts and must not share a bin:
    # "not_published" means stat.vin rendered no seller block for that lot,
    # "unclassified" means it rendered a badge value this parser does not know
    # yet -- the second one is a contract change worth acting on.
    seller_counts = Counter(
        record["seller_class"] or ("unclassified" if record["seller_label"]
                                   else "not_published")
        for record in records
    )
    archive["counts"] = {
        "records": len(records),
        "pages_fetched": len(archive["pages"]),
        "total_results_reported": total_results,
        "cards_seen": cards_seen,
        "duplicate_cards": duplicates,
        # Truncation is about pages not visited, so it compares CARDS SEEN with
        # the reported total. Comparing unique lots would call a cohort
        # truncated whenever the listing repeated one across a page boundary.
        "truncated": bool(total_results is not None and cards_seen < total_results),
        "seller_class": dict(seller_counts),
        "full_vins": sum(1 for r in records if r["vin"]),
        "unclassified_seller_labels": sorted(
            {r["seller_label"] for r in records if not r["seller_class"] and r["seller_label"]}
        ),
    }

    print(f"\n  records: {len(records)} unique lot(s) from {cards_seen} card(s)"
          + (f" of {total_results} reported" if total_results else "")
          + (f"; {duplicates} repeated across pages" if duplicates else ""))
    print(f"  sellers: {dict(seller_counts)}")
    print(f"  VINs:    {archive['counts']['full_vins']}/{len(records)} full (unmasked)")
    if archive["counts"]["truncated"]:
        print("  *** TRUNCATED — raise --max-pages to complete the cohort ***")
    if archive["counts"]["unclassified_seller_labels"]:
        print(f"  new seller labels: {archive['counts']['unclassified_seller_labels']}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")
    print("\n" + "=" * 78)
    print(f"Done. 0 API quota used.")
    print(f"  JSON -> {out_path}")
    if not args.keep_html and not saved_pages:
        print(f"  HTML -> {html_dir}")
    print("  next: copart_statvin_enrich_01.py ADAPTED.json --statvin <this file>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
