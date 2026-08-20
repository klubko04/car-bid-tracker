"""
Stage 4 — download IAAI or Copart lot photos for an OPEN csv-cut.

    data_pull_01.py  ->  csv-cut/*.csv
                              |
                        THIS SCRIPT  ->  images/open/.../{iaai|copart}/{lot}-.../*.jpg

Complements app/image_pipeline.py rather than replacing it. That module builds
the SOLD archive: keyed by VIN, bucketed by tier / make-model / distance / month,
because a sold lot's identity is settled and its photos are a permanent comp.
An open lot is the opposite — its VIN may still be masked, its sale date may not
exist yet, and it will be re-pulled as those resolve. So open photos get one flat
folder per lot and no taxonomy. The HTTP download itself is reused from
app.image_pipeline._download, so both paths share one client and one retry story.

    python analytics/scripts/pull_images_01.py CUT.csv --year 2018 2019 \
        --primary-damage Rear
    python analytics/scripts/pull_images_01.py CUT.csv --where listing_state=Prebid
    python analytics/scripts/pull_images_01.py CUT.csv --dry-run

FOLDER NAME: {lot}-{vin}, AND WHY IT GETS RENAMED
--------------------------------------------------
    images/open/iaai/45490663-WAUENCF55JA084384/     full VIN known
    images/open/iaai/45769760-WAUWNGF57JN******/     still masked

iaai.com masks the VIN on every lot, so an `Auction Not Assigned` lot pulled
web-only is stored under its masked form, asterisks and all. Apibara reveals the
full VIN later — usually once the lot is scheduled and appears in an open pull —
and at that point the folder is RENAMED in place rather than a second folder
being created beside it. One lot, one folder, for the life of the lot.

The rename only ever goes masked -> full. A later pull that knows less (a
web-only re-pull of a lot Apibara already resolved) will not downgrade the name,
because `lot_number` is stable across relists (1 VIN = 1 lot number, verified
1338/1338) and is what identifies the folder.

MASK CHARACTER: the masked tail is written as `xxxxxx`, not `******`. Asterisks
are legal on ext4 and ILLEGAL on Windows, so a `*` tree cannot be copied across
the WSL boundary at all. Folders created under the old `*` scheme are renamed on
sight. `--mask-char` overrides.

Masked-ness is detected as SIX trailing mask characters, never as "contains an
x" — `X` is a legal VIN character and a legal check digit, so a substring test
would misread real VINs as masked.

IMAGE URLS
----------
IAAI URLs are rebuilt from two CSV columns, per the resizer anatomy in
analytics/schema/iaai_csv_schema.md:

    f"{iaai_image_url_prefix}{key}&width={W}&height={H}"

The keys are NOT contiguous (they jump ~11 -> ~115), which is why the CSV stores
the array rather than a count. `--size` picks the dimensions; the resizer honours
whatever is asked, so `full` is genuinely full-res (measured: 400x300 -> 32KB,
845x633 -> 139KB, 2576x1932 -> 854KB on one lot).

Copart has no equivalent resizer key. `copart_image_urls` therefore stores the
pipe-joined `media.items[].large` URLs verbatim. They are Copart's `_hrs.jpg` or
`_vhrs.jpg` assets and are used directly by the downloader; `--size` is
intentionally ignored for Copart.

Re-running is cheap and safe: a file already on disk is skipped, so this is an
incremental sync, not a re-download.
"""
import argparse
import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from csv_image_urls import image_urls as csv_image_urls  # noqa: E402

DATA_DIR = ROOT / "analytics" / "data"
IMAGES_ROOT = ROOT / "images"

# thumb/large match the sizes named in the schema doc; xl is the working default
# because damage is what these photos are for and 845px is too small to judge it.
SIZES = {
    "thumb": (400, 300),
    "large": (845, 633),
    "xl": (1600, 1200),
    "full": (2576, 1932),
}

MANIFEST_COLUMNS = [
    "lot_number", "vin", "vin_masked", "damage_group", "model_folder", "folder", "year", "make",
    "model", "primary_damage", "listing_state", "image_count", "downloaded",
    "skipped", "failed", "renamed_from", "source_csv", "pulled_at",
]

# Top-level split, taken from primary_damage_group. A lot with no damage
# recorded lands in OTHER rather than a fourth bucket — "unclassified" and
# "classified as neither front nor rear/side" are not worth separate folders
# when the folder's job is to group photos for review.
DAMAGE_DIRS = ("FRONT", "REAR-SIDE", "OTHER")


def _ext(url):
    """Return a safe image suffix without importing the HTTP stack for dry-runs."""
    match = re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", str(url), re.I)
    return f".{match.group(1).lower()}" if match else ".jpg"


def group_of(row):
    g = str(row.get("primary_damage_group") or "").strip().upper()
    return g if g in DAMAGE_DIRS else "OTHER"


# IAAI masks exactly the last 6 VIN characters. Testing for the mask character
# anywhere in the name would be wrong: `X` is a legal VIN character (and a legal
# check digit), so `"x" in name` misreads real VINs like WAUX... as masked. Six
# in a row at the end is the mask and nothing else.
_MASKED_TAIL = re.compile(r"(?:\*{6}|[xX]{6})$")


def is_masked_vin(vin):
    return bool(_MASKED_TAIL.search(str(vin or "").strip()))


def money_tag(v):
    """7200.0 -> '$7200'. Empty for absent or zero — no Buy Now, no segment."""
    try:
        n = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return ""
    return f"${int(round(n))}" if n > 0 else ""


_YEAR_SEG = re.compile(r"^(?:19|20)\d{2}$")
_DIST_SEG = re.compile(r"^(\d+)mi$")
# Lot numbers and VINs are FIXED WIDTH in this data — 8 digits and 17 characters,
# on 2,781 of 2,781 rows with no exceptions. That is what makes a bare two-digit
# score unambiguous: nothing else in the name is 1-2 characters long.
LOT_LEN = 8
VIN_LEN = 17
_SCORE_SEG = re.compile(r"^\d{1,2}$")
_MILES_SEG = re.compile(r"^(\d+)k$")
NO_KEYS = "No-Keys"      # NOTE: contains a hyphen; see parse_folder_name
BID_NOW = "BidNow"


def miles_tag(odometer):
    """72358 -> '72k'. Floored, not rounded — '72k' should never overstate.

    Empty when there is no reading at all. A genuine 0-999 mile lot becomes
    '0k', which is correct and informative: it is the signature of an
    inoperable/digital-dash lot where IAAI could not read the odometer.
    """
    try:
        mi = int(float(str(odometer).replace(",", "").strip()))
    except (TypeError, ValueError):
        return ""
    return f"{mi // 1000}k" if mi >= 0 else ""


def keys_tag(has_key):
    """'No-Keys' only when the lot is explicitly flagged as having none.

    Silence means "keys present or not stated" — the flag is only added on a
    definite negative, so its absence never implies a claim.
    """
    s = str(has_key).strip().lower()
    return NO_KEYS if s in ("false", "0", "no", "n") else ""


def sale_tag(listing_state, local_tag=""):
    """Three states, one trailing segment:

        Auction Not Assigned  ->  ''              nothing to say yet
        TimedAuction          ->  'BidNow'        biddable online right now
        Prebid / Prebid+BuyNow -> 'PreBid-0820-Th'  when the sale date is known

    The date comes from IAAI's branch-LOCAL sale time, not from `auction_at`
    (UTC), so the folder always reads the same day the site does.
    """
    st = str(listing_state or "").strip().lower()
    if st == "timedauction":
        return BID_NOW
    if st.startswith("prebid"):
        lt = str(local_tag or "").strip()
        return f"PreBid-{lt}" if lt else "PreBid"
    return ""


def score_tag(score):
    """38 -> '38', 8 -> '08'. Empty when the lot has not been scored.

    Zero-padded for the same reason distance is: `8` would sort after `50` in a
    plain listing. Absent rather than a placeholder when unscored, because IAA
    assigns the score AFTER check-in photos are processed — an `Auction Not
    Assigned` lot routinely arrives without one and gains it days later, at
    which point the folder is renamed to add the segment.
    """
    try:
        n = int(float(str(score).strip()))
    except (TypeError, ValueError):
        return ""
    return f"{n:02d}" if 0 <= n <= 50 else ""


def dist_tag(bucket):
    """'250mi' -> '0250mi'. Zero-padded to 4 digits so names sort by distance.

    Without padding, plain string ordering puts 250mi after 2500mi and before
    3000mi — the folder listing reads as noise. Four digits covers the 3250mi
    top of the observed range with room to spare.
    """
    m = _DIST_SEG.match(str(bucket or "").strip())
    return f"{int(m.group(1)):04d}mi" if m else str(bucket or "").strip()


def folder_name(lot, vin, year, dist_bucket, buy_now, mask_char="x", score=None,
                odometer=None, has_key=None, listing_state=None, local_tag=""):
    """[{year}-][{distance}-]{lot}-{vin}[-${buynow}]

    Year and distance lead so a listing sorts by age then proximity — the two
    coarse filters a human applies before opening anything. Lot and VIN follow as
    the identity, and Buy Now trails because it is the most volatile.

    Every segment except lot and VIN is OPTIONAL, so a lot missing a year,
    coordinates or a Buy Now simply has a shorter name rather than a placeholder
    to decode.
    """
    vin = (vin or "").strip().upper() or "NOVIN"
    if is_masked_vin(vin):
        vin = _MASKED_TAIL.sub(mask_char * 6, vin)
    parts = []
    y = str(year or "").strip()
    if _YEAR_SEG.match(y):
        parts.append(y)
    d = dist_tag(dist_bucket)
    if d:
        parts.append(d)
    parts += [str(lot), vin]
    for tag in (score_tag(score), miles_tag(odometer), keys_tag(has_key),
                money_tag(buy_now), sale_tag(listing_state, local_tag)):
        if tag:
            parts.append(tag)
    return "-".join(parts)


def parse_folder_name(name):
    """'2019-2500mi-45704693-WAUENCF5XJA060484-$7200'
        -> (lot, vin, year, dist, buynow)

    Every segment is identified by SHAPE, never position, because the optional
    ones can be absent and because they have now moved twice. A year is
    19xx/20xx; a distance is digits + `mi`; a Buy Now starts with `$`; the lot is
    the remaining all-digit token; the VIN is the token containing letters.

    Shape-based parsing is what lets one function read every naming generation
    this tree has had — `{lot}-{vin}`, `{lot}-{vin}-{dist}`,
    `{lot}-{vin}-{year}-{dist}` and now `{year}-{dist}-{lot}-{vin}` — which is
    exactly what makes migrating existing folders possible instead of guessing.
    It also keeps masked-VIN detection honest: hand `is_masked_vin()` a whole
    tail and `…xxxxxx-2019-2500mi` reads as NOT masked, silently disabling the
    VIN-resolution rename.
    """
    parts = [p for p in str(name).split("-") if p]
    # `No-Keys` is the one segment containing a hyphen, so splitting shatters it
    # into "No" + "Keys". Rejoin before anything else looks at the tokens —
    # neither half can be mistaken for another segment, which is what makes the
    # repair unambiguous.
    for i in range(len(parts) - 1):
        if parts[i] == "No" and parts[i + 1] == "Keys":
            parts[i:i + 2] = [NO_KEYS]
            break
    # `PreBid-0820-Th` shatters into three tokens for the same reason. Rejoin
    # from the "PreBid" anchor, which nothing else in the name can produce.
    for i, tok in enumerate(parts):
        if tok == "PreBid":
            parts[i:i + 3] = ["-".join(parts[i:i + 3])]
            break

    # Identify by WIDTH first — lot and VIN are fixed-width, so they can be
    # lifted out unambiguously and everything else read from what remains.
    lot = next((p for p in parts if p.isdigit() and len(p) == LOT_LEN), "")
    vin = next((p for p in parts if len(p) == VIN_LEN and not p.isdigit()), "")
    dist = next((p for p in parts if _DIST_SEG.match(p)), "")
    bn = next((p for p in parts if p.startswith("$")), "")
    year = next((p for p in parts if p != lot and _YEAR_SEG.match(p)), "")
    # Whatever short numeric token is left is the score: nothing else in the
    # name is 1-2 characters (year is 4, lot is 8, VIN is 17).
    miles = next((p for p in parts if _MILES_SEG.match(p)), "")
    nokeys = NO_KEYS if NO_KEYS in parts else ""
    bidnow = next((p for p in parts
                    if p == BID_NOW or p.startswith("PreBid")), "")
    used = {lot, vin, dist, bn, year, miles, nokeys, bidnow}
    score = next((p for p in parts if p not in used and _SCORE_SEG.match(p)), "")
    return lot, vin, year, dist, bn, score, miles, nokeys, bidnow


BUCKETS = ("open", "sold")


def lot_dir(platform, lot, vin, group, model, year="", dist_bucket="",
            buy_now="", mask_char="x", bucket="open", score=None,
            odometer=None, has_key=None, listing_state=None, local_tag=""):
    return (IMAGES_ROOT / bucket / model / group / platform
            / folder_name(lot, vin, year, dist_bucket, buy_now, mask_char, score,
                          odometer, has_key, listing_state, local_tag))


def existing_dirs(platform, lot, model=None):
    """Every folder on disk that belongs to this lot, wherever it sits.

    Searched across ALL damage groups AND all model folders, because both can
    change: IAAI revises `primary_damage`, and a lot can surface under a
    different search. The pre-model and pre-group layouts are searched too, so
    older trees migrate on first re-pull instead of being stranded.
    """
    seen = set()
    candidates = []
    # BOTH buckets: a sold lot that relists has to be found under sold/ and
    # moved back to open/, not duplicated there.
    for b in BUCKETS:
        root = IMAGES_ROOT / b
        if not root.is_dir():
            continue
        candidates += list(root.glob(f"*/*/{platform}"))          # model/group/platform
        candidates += [root / g / platform for g in DAMAGE_DIRS]  # pre-model
        candidates.append(root / platform)                        # pre-group
    for c in candidates:
        if not c.is_dir() or c in seen:
            continue
        seen.add(c)
        # The lot number is no longer the name prefix, so every folder is
        # parsed rather than glob-matched. ~300 folders makes this free.
        for p in sorted(c.iterdir()):
            if p.is_dir() and parse_folder_name(p.name)[0] == str(lot):
                yield p


def resolve_folder(platform, lot, vin, group, model, year="", dist_bucket="",
                   buy_now="", mask_char="x", apply=True, bucket="open",
                   score=None, odometer=None, has_key=None, listing_state=None,
                   local_tag=""):
    """`apply=False` reports what WOULD happen and touches nothing.

    Renaming is a side effect of resolution, which made --dry-run mutate the
    tree it was only supposed to describe. A dry run must be readable without
    being destructive.
    """
    return _resolve_folder(platform, lot, vin, group, model, year, dist_bucket,
                           buy_now, mask_char, apply, bucket, score,
                           odometer, has_key, listing_state, local_tag)


def _resolve_folder(platform, lot, vin, group, model, year="", dist_bucket="",
                    buy_now="", mask_char="x", apply=True, bucket="open",
                    score=None, odometer=None, has_key=None, listing_state=None,
                   local_tag=""):
    """-> (path, moved_from). Keeps ONE folder per lot as knowledge improves.

    A lot is identified by its lot number alone — stable across relists — so any
    existing `{lot}-*` folder anywhere in the tree is THIS lot, whatever VIN it
    was named with and whatever damage group it was filed under.

    Four cases, in order of how much they matter:
      group change       primary_damage was revised; MOVE, never fork
      masked -> full     the VIN resolved; rename in place, never a second folder
      full   -> masked   a web-only re-pull knows less; keep the resolved name
      masked -> masked   same knowledge, possibly a different mask character;
                         normalise so one tree does not mix `******` and `xxxxxx`
    """
    want = lot_dir(platform, lot, vin, group, model, year, dist_bucket, buy_now,
                   mask_char, bucket, score, odometer, has_key, listing_state,
                   local_tag)

    for old in existing_dirs(platform, lot):
        if old == want:
            return want, ""
        _, old_vin, *_ = parse_folder_name(old.name)
        _, new_vin, *_ = parse_folder_name(want.name)
        old_masked, new_masked = is_masked_vin(old_vin), is_masked_vin(new_vin)

        # Name and placement are decided independently: keep the better VIN, but
        # always take the CURRENT group, model, distance and Buy Now — those are
        # facts about today, and a stale Buy Now in a folder name is a lie.
        if not old_masked and new_masked:
            target = want.parent / folder_name(lot, old_vin, year, dist_bucket,
                                               buy_now, mask_char, score,
                                               odometer, has_key, listing_state, local_tag)
        else:
            target = want

        if target == old:
            return old, ""
        if target.exists():
            # Destination already populated by an earlier run: use it and leave
            # the stale folder alone rather than merging two trees blind.
            return target, ""
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            old.rename(target)
        return target, str(old.relative_to(IMAGES_ROOT))
    return want, ""


def image_urls(row, size):
    w, h = SIZES[size]
    return csv_image_urls(row, w, h)



# --------------------------------------------------------------------------
# archiving lots that have left the listings
# --------------------------------------------------------------------------
def departed_lots(platform="iaai"):
    """-> {lot_number} that lot_history_01 reports as gone.

    Shared by the archive pass and the download loop ON PURPOSE. They used to
    disagree: `--history` widens the row set to older archives, so a lot that
    has left the site is STILL a row in the cut. The archive moved it to sold/,
    the download loop then resolved it back to open/, and the next model's
    archive moved it again — a ping-pong that left placement decided by
    whichever pass happened to run last.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lot_history_01 as HIST
    paths = HIST.all_archives(platform)
    if not paths:
        return {}, {}
    records = HIST.load_records(paths, platform)
    history = HIST.build_history(records, paths, platform)
    return {k for k, v in history.items() if v.get("exit_state") == "gone"}, history


def bucket_for(lot, gone):
    return "sold" if str(lot) in gone else "open"


def archive_sold(platform="iaai", apply=True, precomputed=None):
    """Move folders for departed lots from images/open/ to images/sold/.

    Photos of a lot that sold are the most valuable thing in the tree — they are
    the comp. Deleting them would be worse than useless, and leaving them in
    `open/` makes the open tree a lie about what is actually biddable. So they
    are archived, keeping the identical {model}/{group}/{platform}/{name} shape
    so an open folder and its sold counterpart are directly comparable.

    "Departed" is `exit_state == 'gone'` from lot_history_01, which is itself
    scope-aware — a lot is only called gone when a later, non-truncated snapshot
    that actually covered its market and keyword failed to contain it. That
    matters here because the move is destructive-ish: a false positive would
    bury a live lot in the sold archive.

    A relist reverses it. `existing_dirs()` searches both buckets, so a lot that
    comes back is found under sold/ and moved to open/ by the next image run
    rather than being downloaded a second time.
    """
    gone, history = precomputed if precomputed else departed_lots(platform)
    if not history:
        return [], []

    moved, skipped = [], []
    root = IMAGES_ROOT / "open"
    if not root.is_dir():
        return moved, skipped
    for folder in sorted(root.glob(f"*/*/{platform}/*")):
        if not folder.is_dir():
            continue
        lot = parse_folder_name(folder.name)[0]
        if lot not in gone:
            continue
        h = history.get(lot) or {}
        # folder is  images/open/{model}/{group}/{platform}/{name}
        model, group = folder.parents[2].name, folder.parents[1].name
        dest = IMAGES_ROOT / "sold" / model / group / platform / folder.name
        if dest.exists():
            skipped.append((folder, dest, "destination exists"))
            continue
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            folder.rename(dest)
        moved.append((folder, dest, h.get("exit_reason") or "unknown",
                      h.get("exit_price_usd") or ""))
    return moved, skipped


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------
def parse_where(values):
    out = []
    for raw in values or []:
        if "=" not in raw:
            raise SystemExit(f"--where wants COLUMN=VALUE, got {raw!r}")
        col, _, val = raw.partition("=")
        out.append((col.strip(), val.strip().lower()))
    return out


def matches(row, args, where):
    if args.year and str(row.get("year") or "") not in args.year:
        return False, "year"
    if args.primary_damage:
        want = {d.strip().lower() for d in args.primary_damage}
        if str(row.get("primary_damage") or "").strip().lower() not in want:
            return False, "primary_damage"
    if args.listing_state:
        want = {s.strip().lower() for s in args.listing_state}
        if str(row.get("listing_state") or "").strip().lower() not in want:
            return False, "listing_state"
    for col, val in where:
        if col not in row:
            raise SystemExit(f"--where column {col!r} is not in the CSV")
        if str(row.get(col) or "").strip().lower() != val:
            return False, col
    if not image_urls(row, "xl"):
        return False, "no images"
    return True, ""


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="pull_images_01.py",
        description="Download lot photos for an open csv-cut into "
                    "images/open/<platform>/{lot}-{vin}/.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_file", help="a csv-cut produced by data_pull_01.py")
    ap.add_argument("--year", nargs="+", help="keep only these model years")
    ap.add_argument("--primary-damage", nargs="+", metavar="DAMAGE")
    ap.add_argument("--listing-state", nargs="+", metavar="STATE")
    ap.add_argument("--where", action="append", default=[], metavar="COL=VALUE",
                    help="exact-match on any CSV column; repeatable")
    ap.add_argument("--size", choices=list(SIZES), default="xl",
                    help=f"image dimensions (default xl = {SIZES['xl'][0]}x{SIZES['xl'][1]})")
    ap.add_argument("--max-lots", type=int, default=0, help="cap lots processed")
    ap.add_argument("--max-images", type=int, default=0,
                    help="cap images per lot (0 = all)")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="seconds between image requests (default 0.25)")
    ap.add_argument("--mask-char", default="x",
                    help="stand-in for IAAI's masked VIN tail in folder names "
                         "(default 'x'). NOT '*' — that is legal on ext4 and "
                         "illegal on Windows, so a '*' tree cannot be copied "
                         "across. Existing '*' folders are migrated on sight.")
    ap.add_argument("--model-folder", metavar="NAME",
                    help='top folder for this search, e.g. "Audi A5". Defaults '
                         'to the dominant make+model in the CSV, so it matches '
                         'the pull_iaai_web search that produced it')
    ap.add_argument(
        "--platform", choices=["iaai", "copart"], default=None,
        help="output platform folder (default: infer from CSV image columns)",
    )
    ap.add_argument("--archive-sold", action="store_true",
                    help="before downloading, move folders for lots that have "
                         "left the listings from images/open/ to images/sold/, "
                         "keeping the same {model}/{group}/{platform} shape")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched, download nothing")
    return ap


def derive_model_folder(rows):
    """The SEARCH this cut came from, e.g. "Audi A5" — not IAAI's per-lot model.

    IAAI writes the trim into `model`, so one "Audi A5" search returns both `A5`
    and `A5 SPORTBACK`. Taking the most common value would name the folder
    "Audi A5 SPORTBACK", which is a trim, not the search. Trims EXTEND the base
    model, so the shortest form is the search term.

    An existing `images/open/*` folder matching case-insensitively wins outright,
    so a tree already organised by hand keeps its exact names rather than
    gaining a near-duplicate.
    """
    # Best source: the search keyword itself, stripped of its leading year.
    # "2019 Audi A5" -> "Audi A5". Exact, and immune to IAAI returning off-target
    # models — a "2018 Audi RS 5" search comes back half RS 3, which no
    # inference from the per-lot `model` field can undo.
    kws = {}
    for r in rows:
        k = re.sub(r"^\s*(19|20)\d{2}\s+", "", str(r.get("search_keyword") or "")).strip()
        if k:
            kws[k] = kws.get(k, 0) + 1
    if kws:
        guess = max(kws.items(), key=lambda kv: kv[1])[0]
        root = IMAGES_ROOT / "open"
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and d.name.lower() == guess.lower():
                    return d.name
        return guess

    makes, models = {}, {}
    for r in rows:
        mk = (r.get("make") or "").strip()
        md = (r.get("model") or "").strip()
        if mk:
            makes[mk] = makes.get(mk, 0) + 1
        if md:
            models[md] = models.get(md, 0) + 1
    if not makes and not models:
        return "UNKNOWN"
    make = max(makes.items(), key=lambda kv: kv[1])[0].title() if makes else ""
    # shortest model string; ties broken by frequency
    model = min(models, key=lambda m: (len(m), -models[m])) if models else ""
    guess = f"{make} {model}".strip()

    root = IMAGES_ROOT / "open"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name.lower() == guess.lower():
                return d.name
    return guess


def resolve_csv(f):
    p = Path(f)
    if p.is_absolute() or p.exists():
        return p
    for b in ("open", "sold"):
        for platform in ("iaai", "copart"):
            cand = DATA_DIR / b / "csv-cut" / platform / f
            if cand.exists():
                return cand
    raise SystemExit(f"csv not found: {f}")


def infer_platform(rows, explicit=None):
    if explicit:
        return explicit
    if rows and "copart_image_urls" in rows[0]:
        return "copart"
    return "iaai"


def main(argv=None):
    import time
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_arg_parser().parse_args(argv)
    where = parse_where(args.where)
    path = resolve_csv(args.csv_file)

    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    platform = infer_platform(rows, args.platform)
    model_folder = args.model_folder or derive_model_folder(rows)

    # One verdict, shared by the archive pass and the download loop below.
    # Computed even when --archive-sold is off would cost a full history build
    # on every run, so it stays opt-in; without the flag every row is treated as
    # open, which is the pre-archive behaviour.
    gone = set()
    if args.archive_sold:
        pre = departed_lots(platform)
        gone = pre[0]
        moved, skipped = archive_sold(platform, apply=not args.dry_run,
                                      precomputed=pre)
        verb = "would move" if args.dry_run else "moved"
        print(f"\n  archive: {verb} {len(moved)} departed lot(s) -> images/sold/")
        for src, dest, reason, price in moved[:20]:
            tag = f"{reason}" + (f" ${price}" if price else "")
            print(f"      {src.parents[2].name}/{src.parents[1].name}/{src.name}  [{tag}]")
        for src, dest, why in skipped:
            print(f"      !! skipped {src.name}: {why}")
    kept, dropped = [], {}
    for r in rows:
        ok, why = matches(r, args, where)
        if ok:
            kept.append(r)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    if args.max_lots:
        kept = kept[:args.max_lots]

    w, h = SIZES[args.size]
    print("=" * 78)
    print(f"Open-lot images — {path.name}")
    print("=" * 78)
    print(f"  {len(rows)} row(s) in, {len(kept)} match")
    if dropped:
        print(f"  filtered out: {dict(sorted(dropped.items(), key=lambda kv: -kv[1]))}")
    if platform == "copart":
        print("  size:   native Copart _hrs/_vhrs.jpg (--size applies to IAAI only)")
    else:
        print(f"  size:   {args.size} ({w}x{h})")
    print(f"  model:  {model_folder}"
          + ("" if args.model_folder else "   (derived from the CSV)"))
    print(f"  target: images/open/{model_folder}/{{{'|'.join(DAMAGE_DIRS)}}}/"
          f"{platform}/{{lot}}-{{vin}}[-{{year}}][-{{dist}}][-{{score}}][-{{mi}}k][-No-Keys][-${{buynow}}][-BidNow]/")
    if not kept:
        raise SystemExit("\nnothing matched the filters")

    planned = sum(len(image_urls(r, args.size)) if not args.max_images
                  else min(len(image_urls(r, args.size)), args.max_images)
                  for r in kept)
    print(f"  {planned} image(s) across {len(kept)} lot(s)"
          f"   ~{int(planned * args.delay / 60) + 1} min")

    if args.dry_run:
        print("\n  DRY RUN — nothing downloaded.")
        for r in kept:
            folder, renamed = resolve_folder(
                platform, r["lot_number"], r["vin"], group_of(r),
                model_folder, r.get("year"), r.get("distance_bucket"),
                r.get("buy_now_usd"), args.mask_char, apply=False,
                bucket=bucket_for(r["lot_number"], gone),
                score=r.get("iaa_vehicle_score"), odometer=r.get("odometer_mi"),
                has_key=r.get("has_key"), listing_state=r.get("listing_state"),
                local_tag=r.get("auction_local_tag"))
            urls = image_urls(r, args.size)
            note = f"   [would move from {renamed}]" if renamed else ""
            print(f"    {r['lot_number']:<10} {r['year']} {r['model']:<10} "
                  f"{str(r['primary_damage'])[:14]:<15} {len(urls):>3} img  "
                  f"-> {group_of(r)}/{platform}/{folder.name}{note}")
        return 0

    # A dry-run only validates selection, folder layout, and URL parsing. Keep
    # the optional HTTP dependency out of that path so CSV contracts can be
    # checked in a minimal analytics environment.
    import httpx
    from app.image_pipeline import _download

    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    manifest, tot_dl, tot_skip, tot_fail = [], 0, 0, 0
    with httpx.Client(headers={"User-Agent": "car-bid-tracker/1.0 (+analytics)"}) as client:
        for n, r in enumerate(kept, 1):
            folder, renamed = resolve_folder(
                platform, r["lot_number"], r["vin"], group_of(r),
                model_folder, r.get("year"), r.get("distance_bucket"),
                r.get("buy_now_usd"), args.mask_char,
                bucket=bucket_for(r["lot_number"], gone),
                score=r.get("iaa_vehicle_score"), odometer=r.get("odometer_mi"),
                has_key=r.get("has_key"), listing_state=r.get("listing_state"),
                local_tag=r.get("auction_local_tag"))
            folder.mkdir(parents=True, exist_ok=True)
            urls = image_urls(r, args.size)
            if args.max_images:
                urls = urls[:args.max_images]

            dl = skip = fail = 0
            for key, url in urls:
                out = folder / f"{r['lot_number']}_{int(key):03d}{_ext(url) or '.jpg'}"
                if out.exists() and out.stat().st_size > 0:
                    skip += 1
                    continue
                if _download(client, url, out):
                    dl += 1
                else:
                    fail += 1
                time.sleep(args.delay)

            tot_dl += dl
            tot_skip += skip
            tot_fail += fail
            flag = f"  MOVED from {renamed}" if renamed else ""
            print(f"  [{n}/{len(kept)}] {group_of(r):<9} {folder.name:<32} "
                  f"{dl} new, {skip} present, {fail} failed{flag}")
            manifest.append({
                "lot_number": r["lot_number"], "vin": r["vin"],
                "vin_masked": is_masked_vin(r["vin"]),
                "damage_group": group_of(r),
                "model_folder": model_folder,
                "folder": str(folder.relative_to(ROOT)),
                "year": r.get("year"), "make": r.get("make"), "model": r.get("model"),
                "primary_damage": r.get("primary_damage"),
                "listing_state": r.get("listing_state"),
                "image_count": len(urls), "downloaded": dl, "skipped": skip,
                "failed": fail, "renamed_from": renamed,
                "source_csv": path.name, "pulled_at": stamp,
            })

    # One manifest for the whole open tree — it now spans damage groups, and a
    # per-group file would fragment the very history the rename logic relies on.
    man_path = IMAGES_ROOT / "open" / "manifest_open.csv"
    man_path.parent.mkdir(parents=True, exist_ok=True)
    # Append new snapshots, but replace every prior row for the SAME source CSV.
    # A resumed run may narrow its final selection (for example after a stricter
    # body-style audit), so retaining lots that disappeared from that snapshot
    # would leave stale manifest entries.  Distinct source CSV snapshots still
    # preserve history.
    existing = []
    if man_path.exists():
        try:
            with man_path.open(encoding="utf-8", newline="") as stream:
                existing = list(csv.DictReader(stream))
        except (OSError, csv.Error) as e:
            print(f"  !! could not read existing manifest ({e}); "
                  f"writing a .bak rather than overwriting")
            man_path.replace(man_path.with_suffix(".bak.csv"))
    current_sources = {row.get("source_csv", "") for row in manifest}
    retained = [
        row for row in existing
        if row.get("source_csv", "") not in current_sources
    ]
    with open(man_path, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS,
                            restval="", extrasaction="ignore")
        wr.writeheader()
        wr.writerows(retained + manifest)
    replaced = len(existing) - len(retained)
    print(f"  manifest: {len(retained)} prior + {len(manifest)} current entries"
          f" ({replaced} resumed row(s) replaced)")

    print("\n" + "=" * 78)
    print(f"Done. {tot_dl} downloaded, {tot_skip} already present, {tot_fail} failed")
    print(f"  images   -> {IMAGES_ROOT / 'open' / model_folder}/<group>/{platform}")
    print(f"  manifest -> {man_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
