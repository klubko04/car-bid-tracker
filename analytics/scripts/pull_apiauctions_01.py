"""
API Auctions pull — stage 1, RAW JSON ONLY. Sibling of pull_apibara_01.py.

    pull_apiauctions_01.py  ->  raw .json  ->  (a json2csv stage, when written)

Same contract as the Apibara puller: build a server-side query, archive the
untouched responses, derive nothing. Output goes to the SAME tree, because the
folder axis is the auction house (iaai / copart), not the vendor:

    analytics/data/{sold|open}/json-raw/{iaai|copart}/apiauctions_*.json
                                                      ^^^^^^^^^^^
    the filename prefix is what separates vendors — apibara_* vs apiauctions_*

Run:

    python analytics/scripts/pull_apiauctions_01.py iaai open \
        --make Audi --model A5 --year-range 2018-2023 --max-pages 5

Token: APIAUCTIONS_API_TOKEN in the repo-root .env (see .env.example).
Free demo tier is 10 requests/HOUR — small, but per_page is 100, so one page
here carries what five Apibara pages do.

HOW THIS DIFFERS FROM APIBARA, AND WHY THE SCRIPT LOOKS DIFFERENT
-----------------------------------------------------------------
POST, not GET; `Authorization: Bearer` header, not `X-API-Key`. Beyond that,
three differences shape the code:

1. PAGE-BASED, WITH A TOTAL. Apibara hands out an opaque cursor and never says
   how many lots match, so `--max-pages` there is a guess. Here the first
   response carries `meta.total` and `meta.last_page`, so this script prints the
   real page count after ONE call and stops early rather than guessing. On a
   10-requests/hour tier that difference is the whole ballgame.

2. SOLD AND OPEN ARE DIFFERENT ENDPOINTS, not a status param:
       sold -> POST /api/v2/get-cars         (vehicles + full trading history)
       open -> POST /api/v2/get-active-lots  (live/scheduled, not yet sold)
   So the mode positional selects an endpoint, and the filter set legal for one
   is not legal for the other (see ENDPOINTS below).

3. PAIRED-REQUIRED RANGE FILTERS. Passing `*_to` without its `*_from` is a 422.
   That is validated here before spending a request, the same way the Apibara
   puller validates enums locally.

WHAT IS NOT AVAILABLE HERE (and is, on Apibara)
-----------------------------------------------
seller       No seller field and no seller filter. Apibara's
             `seller_type=insurance` — the filter every sold pull in this repo
             relies on — has no equivalent, so insurance and dealer lots come
             back mixed and cannot be told apart afterwards either.
body style   Only `car_info_vehicle_type` (PASSENGER CAR / TRUCK). Nothing that
             separates Coupe from Sedan.
ACV / repair The IAAI economics that make an Apibara row self-pricing
             (ActualCashValue, EstimatedRepairCost) are absent. There is
             `estimate_retail`, which is a retail estimate, NOT the insurer's
             cash value — do not treat them as the same number.
cylinders    Not on the lot; only via a separate /vin-decoding call.

WHAT IS AVAILABLE HERE (and is not, on Apibara)
-----------------------------------------------
sale_price_from/to      filter by realised sale price, server-side
created_at/updated_at   incremental sync — pull only what changed since a date
history[]               every row carries its own trading history, where Apibara
                        needs one /history call per VIN
per_page up to 100      vs 20
meta.total              know the result size before paging

Response schemas are NOT in their OpenAPI spec (every response is documented as
just "OK - List of vehicles"), so the field list above comes from the docs'
example responses and may be abridged. That is precisely what this script is
for: archive the real payload and look.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "analytics" / "data"

BASE = "https://apiauctions.io"
RATE_DELAY = 1.2                 # free tier is quota- not rate-bound; be polite
VENDOR = "apiauctions"           # filename prefix; folder stays the auction house

# REQUIRED. apiauctions.io sits behind Cloudflare with "browser signature"
# blocking on, and urllib's default `Python-urllib/3.x` is on the banned list —
# it answers 403 error 1010 at the edge, before the API or the token is ever
# consulted. Any ordinary identifying UA gets through; this one names the client
# honestly rather than impersonating a browser.
USER_AGENT = "car-bid-tracker/1.0 (+analytics; python-urllib)"

# mode -> (bucket folder, endpoint). Sold and open are different endpoints here,
# not two values of one status parameter.
ENDPOINTS = {
    "sold": ("sold", "/api/v2/get-cars"),
    "open": ("open", "/api/v2/get-active-lots"),
}

# platform -> exact `auction_name` value. From POST /api/v2/get-auctions:
# COPART, COPART CANADA, COPART FINLAND, IAAI, EMIRATES AUCTION.
AUCTION_NAMES = {"iaai": "IAAI", "copart": "COPART"}

# `*_to` requires its `*_from`, else the API answers 422. Checked locally so a
# malformed request never costs a request.
PAIRED = ("auction_date", "sale_price", "updated_at", "created_at",
          "estimate_retail", "odometer", "current_bid", "buy_now_price")

# Filters each endpoint accepts. get-active-lots has no sale_price/odometer
# (nothing has sold yet); get-cars has no current_bid/buy_now_price ranges.
COMMON = {"make", "model", "year_from", "year_to", "auction_name",
          "auction_date_from", "auction_date_to", "is_buy_now",
          "estimate_retail_from", "estimate_retail_to",
          "created_at_from", "created_at_to", "car_info_vehicle_type",
          "page", "per_page"}
ENDPOINT_PARAMS = {
    "/api/v2/get-cars": COMMON | {
        "sale_price_from", "sale_price_to", "odometer_from", "odometer_to",
        "updated_at_from", "updated_at_to"},
    "/api/v2/get-active-lots": COMMON | {
        "current_bid_from", "current_bid_to",
        "buy_now_price_from", "buy_now_price_to", "without_sale_date"},
}


# --------------------------------------------------------------------------
# env + transport
# --------------------------------------------------------------------------
def read_env_key(path=ENV_PATH, name="APIAUCTIONS_API_TOKEN"):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def build_url(path, params):
    return f"{BASE}{path}?" + urllib.parse.urlencode(params, doseq=True)


def post(token, path, params, form_body=False):
    """-> (status, payload, rate_headers).

    Their docs are inconsistent about where parameters travel: the curl examples
    put them on the query string of a POST with an empty body ("all parameters
    travel on the query string"), while the OpenAPI spec declares a required
    form-urlencoded body. Query string is tried first because that is what the
    documented examples do; main() retries once with a form body on a 422, and
    reports which shape worked so the next run can skip the retry.
    """
    headers = {"Accept": "application/json",
               "User-Agent": USER_AGENT,
               "Authorization": f"Bearer {token}"}
    if form_body:
        url = f"{BASE}{path}"
        data = urllib.parse.urlencode(params, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        url = build_url(path, params)
        data = b""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.getcode(), body, rate_headers(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error_body": raw[:600]}
        return e.code, body, rate_headers(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}, {}


def rate_headers(headers):
    """X-RateLimit-* — worth surfacing on a 10-requests/hour tier."""
    out = {}
    for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        v = headers.get(k) if headers else None
        if v is not None:
            out[k] = v
    return out


def describe_rate(rl):
    if not rl:
        return ""
    left = rl.get("X-RateLimit-Remaining")
    limit = rl.get("X-RateLimit-Limit")
    reset = rl.get("X-RateLimit-Reset")
    bits = f"{left}/{limit} left" if left is not None else ""
    if reset:
        try:
            when = dt.datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S")
            bits += f", resets {when}"
        except (TypeError, ValueError):
            pass
    return f"   [rate: {bits}]" if bits else ""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def multiword(value):
    """nargs='+' -> one string, so `--model A5 Sportback` needs no quotes."""
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
    lo = int(m.group(1))
    hi = int(m.group(2) or m.group(1))
    if hi < lo:
        raise argparse.ArgumentTypeError(f"--year-range {lo}-{hi} is backwards")
    return lo, hi


def parse_date(s):
    try:
        return dt.date.fromisoformat(s.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"dates must be YYYY-MM-DD, got {s!r}") from None


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="pull_apiauctions_01.py",
        description="Pull raw API Auctions JSON into "
                    "data/{sold|open}/json-raw/{iaai|copart}/apiauctions_*.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Free demo tier: 10 requests/HOUR. per_page is 100, and the "
               "first response reports meta.total, so the run prints the real "
               "page count instead of guessing.")
    ap.add_argument("platform", choices=list(AUCTION_NAMES), type=str.lower)
    ap.add_argument("mode", choices=list(ENDPOINTS), type=str.lower,
                    help="sold -> /get-cars (history), open -> /get-active-lots")
    ap.add_argument("--make", nargs="+", metavar="MAKE")
    ap.add_argument("--model", nargs="+", metavar="MODEL")
    ap.add_argument("--year-range", type=parse_year_range, metavar="YYYY-YYYY")
    ap.add_argument("--auction-date-range", nargs="+", type=parse_date,
                    metavar="YYYY-MM-DD",
                    help="one date = today..that date; two = from..to")
    ap.add_argument("--sale-price-range", nargs=2, type=int,
                    metavar=("FROM", "TO"), help="sold only")
    ap.add_argument("--odometer-range", nargs=2, type=int,
                    metavar=("FROM", "TO"), help="sold only")
    ap.add_argument("--auction-name",
                    help=f"override the exact auction name "
                         f"(default: {AUCTION_NAMES})")
    ap.add_argument("--per-page", type=int, default=100,
                    help="max 100 (default 100)")
    ap.add_argument("--max-pages", type=int, default=5,
                    help="page cap; EACH PAGE IS ONE REQUEST (default 5). "
                         "Paging stops at meta.last_page on its own")
    ap.add_argument("--out", help="output basename or path")
    ap.add_argument("--form-body", action="store_true",
                    help="send params as a form body instead of query string")
    ap.add_argument("--skip-token-check", action="store_true",
                    help="bypass the truncated-key guard")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved request, make no call")
    return ap


def resolve_params(args, endpoint):
    p = {"page": 1, "per_page": min(max(args.per_page, 1), 100)}
    name = args.auction_name or AUCTION_NAMES[args.platform]
    p["auction_name"] = name
    if args.make:
        p["make"] = multiword(args.make)
    if args.model:
        p["model"] = multiword(args.model)
    if args.year_range:
        p["year_from"], p["year_to"] = (str(y) for y in args.year_range)
    if args.auction_date_range:
        d = args.auction_date_range
        if len(d) > 2:
            raise SystemExit("--auction-date-range takes one or two dates")
        lo, hi = sorted([dt.date.today(), d[0]] if len(d) == 1 else list(d))
        p["auction_date_from"], p["auction_date_to"] = lo.isoformat(), hi.isoformat()
    if args.sale_price_range:
        p["sale_price_from"], p["sale_price_to"] = sorted(args.sale_price_range)
    if args.odometer_range:
        p["odometer_from"], p["odometer_to"] = sorted(args.odometer_range)

    allowed = ENDPOINT_PARAMS[endpoint]
    bad = sorted(k for k in p if k not in allowed)
    if bad:
        raise SystemExit(
            f"{', '.join(bad)} not accepted by {endpoint} — that endpoint takes "
            f"{', '.join(sorted(allowed))}")
    for stem in PAIRED:
        lo, hi = f"{stem}_from" in p, f"{stem}_to" in p
        if lo != hi:
            raise SystemExit(
                f"{stem}_from/{stem}_to must be passed together (the API "
                f"answers 422 otherwise)")
    return p


def default_basename(args, params):
    bits = [VENDOR, args.platform, args.mode]
    bits += [multiword(x).lower().replace(" ", "-")
             for x in (args.make, args.model) if x]
    if args.year_range:
        bits.append(f"{args.year_range[0]}-{args.year_range[1]}")
    if "auction_date_from" in params:
        bits.append(f"{params['auction_date_from']}_{params['auction_date_to']}")
    bits.append(dt.datetime.now().strftime("%Y%m%dT%H%M%S"))
    return "_".join(bits)


def resolve_out_path(args, params):
    bucket, _ = ENDPOINTS[args.mode]
    out_dir = DATA_DIR / bucket / "json-raw" / args.platform
    if args.out:
        p = Path(args.out)
        return (p if p.is_absolute() else out_dir / p).with_suffix(".json")
    return (out_dir / default_basename(args, params)).with_suffix(".json")


# --------------------------------------------------------------------------
def summarize(records):
    """Coverage report, and a field census — the point of a first contact is
    finding out what the payload actually carries."""
    if not records:
        print("  no records returned")
        return
    keys, filled = {}, {}
    for r in records:
        for k, v in r.items():
            keys[k] = keys.get(k, 0) + 1
            if v not in (None, "", [], {}):
                filled[k] = filled.get(k, 0) + 1
    n = len(records)
    print(f"\n  records: {n}")
    print(f"  fields per record: {len(keys)}")
    print(f"  {'field':<28} filled")
    for k in sorted(keys, key=lambda k: (-filled.get(k, 0), k)):
        example = next((r[k] for r in records
                        if r.get(k) not in (None, "", [], {})), "")
        if isinstance(example, (list, dict)):
            example = f"<{type(example).__name__} len={len(example)}>"
        print(f"    {k:<28} {filled.get(k, 0):>3}/{n}   {str(example)[:52]}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be >= 1")

    bucket, endpoint = ENDPOINTS[args.mode]
    params = resolve_params(args, endpoint)
    out_path = resolve_out_path(args, params)

    print("=" * 78)
    print(f"API AUCTIONS — {args.platform.upper()} / {args.mode}  ->  {endpoint}")
    print(f"  params:  {params}")
    print(f"  budget:  up to {args.max_pages} request(s), {params['per_page']}/page")
    print("=" * 78)

    if args.dry_run:
        print(f"\n  DRY RUN — no call made.\n  POST {build_url(endpoint, params)}")
        print(f"  would write -> {out_path}")
        return 0

    token = read_env_key()
    if not token:
        raise SystemExit(
            f"No APIAUCTIONS_API_TOKEN in {ENV_PATH}\n"
            f"  Add a line:  APIAUCTIONS_API_TOKEN=your_demo_token\n"
            f"  Get one at https://apiauctions.io/register (free tier).")

    # The dashboard lists keys by a truncated PREFIX ("sk_live_fd0c36fb…") and
    # shows the full value only once, at creation. Copying from the key list
    # therefore yields something that looks like a key but can only ever 401 —
    # and on a 10-requests/hour tier, finding that out the expensive way costs a
    # tenth of the hourly budget. Refuse the obvious prefix shape instead.
    if not args.skip_token_check and re.fullmatch(
            r"sk_(live|test)_[0-9a-f]{8}", token):
        raise SystemExit(
            f"APIAUCTIONS_API_TOKEN looks like the dashboard's display PREFIX, "
            f"not a full key ({len(token)} chars).\n"
            f"  The full value is shown only at creation — it cannot be "
            f"re-read from the key list.\n"
            f"  Fix: https://apiauctions.io/api-keys -> Rotate -> copy the full "
            f"value from the dialog.\n"
            f"  Override with --skip-token-check if their keys really are this "
            f"short.")

    out = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "argv": argv,
        "vendor": VENDOR,
        "platform": args.platform,
        "mode": args.mode,
        "endpoint": endpoint,
        "server_params": params,
        "pages": [],
    }
    records, calls, form_body = [], 0, args.form_body
    last_page = None

    while calls < args.max_pages:
        page_params = dict(params, page=len(out["pages"]) + 1)
        code, data, rl = post(token, endpoint, page_params, form_body)
        calls += 1

        # one automatic retry in the other parameter shape — their docs and
        # their OpenAPI spec disagree about which is correct
        if code == 422 and not form_body and calls == 1:
            print(f"  page 1: HTTP 422 on query-string params — retrying once "
                  f"as a form body{describe_rate(rl)}")
            form_body = True
            code, data, rl = post(token, endpoint, page_params, form_body)
            calls += 1

        out["pages"].append({"status": code, "params": page_params,
                             "form_body": form_body, "rate": rl, "raw": data})
        if code != 200:
            print(f"\n  HTTP {code}: {json.dumps(data)[:500]}{describe_rate(rl)}")
            if code == 401:
                print("  -> token missing/invalid")
            elif code == 402:
                print("  -> no active plan on the account, or its quota is "
                      "spent. 'No active tariff found' means the token is "
                      "valid but no tariff is attached — activate a plan "
                      "(Free = 10 req/hour) in the dashboard.")
            elif code == 403 and "1010" in json.dumps(data):
                print("  -> Cloudflare blocked the client signature before the "
                      "API saw it. USER_AGENT is set for exactly this; if it "
                      "still fires, that header is being stripped.")
            elif code == 422:
                print("  -> parameters rejected in BOTH shapes; check the "
                      "paired *_from/*_to rules and endpoint-specific filters")
            break

        rows = data.get("data") or []
        records.extend(rows)
        meta = data.get("meta") or {}
        total, last_page = meta.get("total"), meta.get("last_page")
        page_no = meta.get("page", len(out["pages"]))
        print(f"  page {page_no}: {len(rows)} lot(s)"
              f"   total={total} last_page={last_page}"
              f"   [{'form' if form_body else 'query'}]{describe_rate(rl)}")

        # The whole advantage over a cursor API: after ONE call we know the size
        if page_no == 1 and last_page:
            need = min(last_page, args.max_pages)
            print(f"    -> {total} lot(s) across {last_page} page(s); this run "
                  f"will fetch {need}"
                  + ("" if last_page <= args.max_pages else
                     f" and STOP SHORT (raise --max-pages to {last_page})"))
        if not rows or (last_page and page_no >= last_page):
            break
        time.sleep(RATE_DELAY)

    truncated = bool(last_page and len(out["pages"]) < last_page)
    if truncated:
        print(f"\n  *** TRUNCATED at --max-pages {args.max_pages} of "
              f"{last_page} page(s) ***")

    out["counts"] = {"records": len(records), "calls_used": calls,
                     "truncated": truncated, "last_page": last_page}
    summarize(records)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n" + "=" * 78)
    print(f"Done. {calls} request(s) used.")
    print(f"  JSON -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
