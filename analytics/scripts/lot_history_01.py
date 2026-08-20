"""
Cross-snapshot lot history — what changed about a lot between pulls.

Used by `data_pull_01.py --history`; also runnable standalone to inspect or
materialise the history artifact:

    python analytics/scripts/lot_history_01.py --all
    python analytics/scripts/lot_history_01.py --all --cache
    python analytics/scripts/lot_history_01.py ARCHIVE.json ARCHIVE2.json
    python analytics/scripts/lot_history_01.py --platform copart --all --cache

WHY THIS EXISTS
---------------
Every column in the CSV describes a lot at ONE moment. The things that actually
move a bid decision are changes between moments:

    a relist            the lot ran, did not sell, and was rescheduled
    a buy-now appearing the seller blinked after a failed sale
    a disappearance     it sold, or was withdrawn

None of that is visible in a single archive, and stage 3's newest-wins de-dupe
actively destroys it — that is why history is computed BEFORE de-dupe and then
attached to the surviving row.

Observed on two pulls 18 hours apart:

    lot 45704693  08-13  Prebid         auction 08-14 08:30
                  08-14  Prebid/BuyNow  auction 08-21 08:30   + $7,600 buy-now
    lot 45250068  08-13  Prebid/BuyNow  buy_now_sold=True
                  08-14  absent — sold and removed from the site

THE COHORT RULE (the part that is easy to get wrong)
-----------------------------------------------------
Absence is only evidence of departure **within the same search**. A lot missing
from an archive of a different make/model/source proves nothing, so every
snapshot is tagged with a cohort key and a lot is only ever compared against
snapshots of its own cohort.

Two sources are never one cohort even for the same car: Apibara does not return
`Auction Not Assigned` (`InventoryStatus=WC`) lots at all — 1 of 1,349 in the
corpus versus 56 of 65 on a web pull — so a WC lot is "missing" from every
Apibara archive by design.

The Copart equivalent is stricter: only a complete exact-model first-party web
snapshot may prove site-wide disappearance. APIBara Open and Live are state
slices, so an absent lot may simply be in the other state. They contribute bid,
sale-date and VIN observations but never authorize an open -> sold image move.
Offline raw/adapted/image-enriched variants of one Copart pull are collapsed to
one logical snapshot before history is built.

TRUNCATED SNAPSHOTS ARE EXCLUDED FROM ABSENCE
----------------------------------------------
The iaai.com search caps at 100 rows with no paging, and Apibara pages can hit
`--max-pages`. A lot missing from a capped query may simply be past the ceiling.
Such snapshots still contribute observations (a lot seen there was really
there); they are only barred from proving a lot GONE.

REBUILDABLE, THEREFORE A CACHE
-------------------------------
Everything here is derived from json-raw / json-adapted. `--cache` writes
`analytics/data/<bucket>/history/<platform>/<cohort>.json` for inspection and
speed, but that file is never a source of truth — delete it and the next run
rebuilds it identically.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import apibara_json2csv_iaai_01 as IAAI  # noqa: E402
import apibara_json2csv_copart_01 as COPART  # noqa: E402
import copart_web_adapt_01 as COPART_WEB  # noqa: E402

DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
PLATFORM = "iaai"
PLATFORMS = ("iaai", "copart")


def platform_of(document=None, record=None, explicit=None):
    value = explicit or (record or {}).get("_platform") or \
        (record or {}).get("platform") or (document or {}).get("platform") or PLATFORM
    value = str(value).strip().lower()
    if value not in PLATFORMS:
        raise ValueError(f"unsupported history platform: {value or '(blank)'}")
    return value


def flattener(platform):
    return COPART if platform == "copart" else IAAI

# Apibara's last_sold_status -> exit_reason. "Sold on Approval" is worth keeping
# distinct: the seller had not accepted, and 26% of the sold corpus sits in that
# state — it is the population most likely to come back as a relist.
SOLD_REASON = {
    "sold": "sold_at_auction",
    "sold on approval": "sold_on_approval",
}

HISTORY_COLUMNS = [
    "first_seen_at",
    "last_seen_at",
    "snapshots",
    "days_listed",
    "relist_count",
    "auction_at_prior",
    "buy_now_first_seen",
    "exit_state",
    "exit_reason",
    "exit_price_usd",
    "images_first_seen",
    "acv_first_seen",
    "assigned_first_seen",
    "record_versions",
    "exit_price_source",
    "declined_approval",
    "buy_now_at_relist",
    "bid_condition_first_seen",
    "bid_condition_prior",
    "bid_condition_changes",
]


# --------------------------------------------------------------------------
# cohort identity
# --------------------------------------------------------------------------
def cohort_key(doc, platform=None):
    """Which search space a snapshot covered. Absence only counts within one.

    ALL web archives share one cohort, and every scoping question — which
    keywords, which market — is answered PER LOT in build_history instead.

    Putting scope in the cohort key looks tidier and is worse. A
    `--year-range 2018-2023` archive would form a different cohort from the
    single-year "2018 Audi A5" archives already on disk, silently restarting the
    history of every 2018 lot; the same trap as `--market us`. Scope belongs on
    the snapshot, where it can be compared against the individual lot.

    Apibara archives still key on the server params they sent, date range
    INCLUDED — a date-limited pull genuinely covers a different space, so a lot
    outside it is not missing, it is out of scope.
    """
    platform = platform_of(doc, explicit=platform)
    source = str(doc.get("source") or "apibara").lower()
    if platform == "copart" and source in ("copart-web", "copart-web-adapted"):
        params = doc.get("search_params") or {}
        desc = "|".join((
            "copart",
            f"make={str(params.get('make') or '?').strip().lower()}",
            f"model={str(params.get('model') or '?').strip().lower()}",
        ))
        return f"web::{desc}"
    if source in ("iaai-web", "iaai-web-adapted"):
        desc = "iaai"
    else:
        params = dict(doc.get("server_params") or {})
        for noise in ("per_page", "cursor", "platform"):
            params.pop(noise, None)
        desc = "|".join(f"{k}={params[k]}" for k in sorted(params)) or "?"
    return f"{'web' if 'web' in source else 'apibara'}::{desc}"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:80]


def optional_year(value):
    """Normalize integral year values without letting malformed scope crash history."""
    try:
        parsed = float(str(value).strip())
        return int(parsed) if parsed.is_integer() else None
    except (TypeError, ValueError):
        return None


def snapshot_meta(path, platform=None):
    """Envelope facts about one archive, without loading its records twice."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    platform = platform_of(doc, explicit=platform)
    counts = doc.get("counts") or {}
    truncated = bool(counts.get("truncated")) or bool(
        (doc.get("search_params") or {}).get("truncated")) or bool(
        counts.get("failed_queries"))
    # Which tenants this snapshot could have contained. None = no restriction.
    # A `us` snapshot excluded Canadian lots by construction, so it can never
    # prove one absent.
    sp = doc.get("search_params") or {}
    scope = str(sp.get("market") or "").lower()
    markets = {"us": {"US"}, "ca": {"CA"}}.get(scope)
    # Which searches this snapshot ran. A "2019 Audi A5" pull cannot speak to a
    # 2018 lot, so it may not call one absent. None = not a keyword search
    # (Apibara), which is scoped by its cohort instead.
    kws = {str(k).strip().lower() for k in (sp.get("keywords") or [])} or None
    server = doc.get("server_params") or {}
    source = str(doc.get("source") or "apibara").lower()
    if platform == "copart":
        # Every canonical Copart adapter is deliberately US-only. Raw web
        # snapshots are filtered to US by load_records() below before history
        # sees them, so their absence scope is US as well.
        markets = {"US"}
        year_min = sp.get("year_min", server.get("year_from"))
        year_max = sp.get("year_max", server.get("year_to"))
        make = sp.get("make", server.get("make"))
        model = sp.get("model", server.get("model"))
    else:
        year_min = year_max = make = model = None
    mode = str(doc.get("mode", "open")).lower()
    # Copart APIBara Open/Live endpoints are state slices (8 Open records vs
    # 70 in the matching web search in the current S5 cohort). They contribute
    # observations but cannot prove a vehicle left Copart. Only a complete,
    # exact, first-party web search can do that.
    absence_capable = not truncated and (
        platform != "copart" or (
            source in ("copart-web", "copart-web-adapted") and mode == "open"
        )
    )
    return {
        "file": Path(path).name,
        "pulled_at": doc.get("generated_at") or "",
        "cohort": cohort_key(doc, platform),
        "platform": platform,
        "truncated": truncated,
        "absence_capable": absence_capable,
        "markets": markets,
        "keywords": kws,
        "year_min": optional_year(year_min),
        "year_max": optional_year(year_max),
        "make": str(make).strip().lower() if make else None,
        "model": str(model).strip().lower() if model else None,
        "mode": mode,
        "source": source,
    }


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------
def observe(v, platform=None):
    """One lot at one moment, reduced to the fields history cares about."""
    platform = platform_of(record=v, explicit=platform)
    flat = flattener(platform)
    auction_at = flat.g(v, "auction", "auction_at") or v.get("ad")
    listing_state = flat.listing_state(v)
    image_count = flat.g(v, "media", "thumbs_count") or len(
        flat.g(v, "media", "items", default=[]) or []
    )
    if platform == "iaai":
        acv = flat.money_num(flat.sale_info(v).get("ActualCashValue"))
        version = flat.attrs(v).get("VersionId")
        tenant = (str(flat.clean(flat.attrs(v).get("Id"))
                      or flat.clean(v.get("_web_item_id")) or "")
                  .rsplit("~", 1)[-1].upper() or None)
    else:
        acv = flat.csv_acv(v)
        tenant = "US" if flat.market(v) == "UnitedStates" else None
        bid_condition = flat.bid_condition(v)
        # Copart has no VersionId. A stable fingerprint counts actual record
        # state changes without counting duplicate raw/adapted envelopes as
        # different versions.
        fingerprint = {
            "auction_at": auction_at,
            "listing_state": listing_state,
            "current_bid": flat.g(v, "pricing", "current_bid_usd"),
            "buy_now": flat.g(v, "pricing", "buy_now_usd"),
            "sold_day": flat.g(v, "auction", "last_sold_day"),
            "sold_status": flat.g(v, "auction", "last_sold_status"),
            "image_count": image_count,
            "vin": v.get("vin"),
            "bid_condition": bid_condition,
        }
        version = hashlib.sha1(json.dumps(
            fingerprint, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()[:12]
    return {
        "platform": platform,
        "auction_at": auction_at,
        "listing_state": listing_state,
        "active": listing_state != "Ended" and str(v.get("_mode") or "").lower() != "ended",
        "buy_now_usd": flat.money_num(flat.g(v, "pricing", "buy_now_usd")),
        # Apibara's sold_buy_now is not trustworthy alone: on 2 of 5 ended A5
        # lots it was True while is_buy_now was False, buy_now_usd was null and
        # the sale price was $0. Corroborate it with an actual buy-now price or
        # the is_buy_now flag before calling an exit a buy-now sale.
        "buy_now_sold": bool(flat.g(v, "auction", "sold_buy_now")),
        "is_buy_now": bool(flat.g(v, "auction", "is_buy_now")),
        "timed_close_at": flat.g(v, "auction", "timed_end_at"),
        # Only an Apibara `ended` pull carries these. iaai.com never publishes a
        # sale result: a lot simply disappears, which is why a web-only history
        # can say "gone" but never "gone for $7,850".
        # usd(), not money_num(): a CAD sale price must not become exit_price_usd.
        "sold_price": flat.usd(v, flat.g(v, "pricing", "last_sold_price_usd")),
        "sold_day": flat.g(v, "auction", "last_sold_day"),
        "sold_status": flat.g(v, "auction", "last_sold_status"),
        # Enrichment: an `Auction Not Assigned` lot is not a finished record.
        # IAAI back-fills photos and the insurer's ACV after listing, so a lot
        # can be un-analysable one day and complete the next.
        "image_count": image_count,
        "acv": acv,
        # IAAI's own edit counter. CreatedDateTime/ModifiedDateTime are NOT
        # arrival timestamps — they are rewritten on every record update (12/12
        # observed lots moved their "created" date between two pulls while
        # VersionId incremented), so this is the only honest change signal, and
        # first_seen_at below is the only honest age.
        "version": version,
        "detail_level": v.get("_detail_level") or "full",
        # 'US' / 'CA', from the item id. Decides which later snapshots are
        # entitled to call this lot absent.
        "tenant": tenant,
        "keyword": (str(v.get("_web_keyword")).strip().lower()
                    if v.get("_web_keyword") else None),
        "year": v.get("year"),
        "make": str(v.get("make") or "").strip().lower() or None,
        "model": str(v.get("model") or "").strip().lower() or None,
        "assigned": bool(auction_at) if platform == "copart" else (
            bool(listing_state) and listing_state != "Auction Not Assigned"
        ),
        "bid_condition": bid_condition if platform == "copart" else "",
    }


def _days(a, b):
    """Whole-ish days between two ISO timestamps, or None."""
    try:
        d0 = dt.datetime.fromisoformat(str(a))
        d1 = dt.datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return None
    return round((d1 - d0).total_seconds() / 86400, 1)


def build_history(records, paths, platform=None):
    """-> {lot_number: {history columns}}.

    `records` must already carry `_source_file` (stage 2's load_records does
    this). `paths` supplies the envelope facts those filenames refer to.
    """
    if platform is None:
        platform = platform_of(record=records[0] if records else None)
    meta = {}
    for p in paths:
        m = snapshot_meta(p, platform)
        meta[m["file"]] = m

    # cohort -> ordered list of snapshot files, oldest first
    cohort_snaps = {}
    for m in meta.values():
        cohort_snaps.setdefault(m["cohort"], []).append(m)
    for snaps in cohort_snaps.values():
        snaps.sort(key=lambda m: m["pulled_at"])

    # lot -> cohort -> [(pulled_at, snapshot_meta, observation)], oldest first
    timeline = {}
    for v in records:
        m = meta.get(v.get("_source_file"))
        if not m:
            continue
        lot = str(v.get("lot_number") or "")
        if not lot:
            continue
        timeline.setdefault(lot, {}).setdefault(m["cohort"], []).append(
            (m["pulled_at"], m, observe(v, platform)))
    for by_cohort in timeline.values():
        for runs in by_cohort.values():
            runs.sort(key=lambda r: r[0])

    out = {}
    for lot, by_cohort in timeline.items():
        runs = sorted((r for rs in by_cohort.values() for r in rs),
                      key=lambda r: r[0])
        seen_at = [r[0] for r in runs]
        obs = [r[2] for r in runs]

        # A relist moves the sale to a different DAY. Comparing full timestamps
        # instead turns every clock wobble into a phantom relist: lot 45625127
        # was reported by two Apibara pulls five hours apart as 2026-08-20
        # 02:30 then 2026-08-20 13:30 — same day, a corrected time, not a
        # reschedule. Computed per cohort as well, so two sources disagreeing
        # about one lot can never manufacture a relist either.
        best_days, best_prior, best_split = [], "", None
        for runs_c in by_cohort.values():
            days, prior, split = [], "", None
            for idx, (_, _, o) in enumerate(runs_c):
                d = str(o["auction_at"] or "")[:10]
                if d and (not days or days[-1][0] != d):
                    if days:
                        prior = days[-1][1]
                        split = idx        # first observation carrying the NEW date
                    days.append((d, o["auction_at"]))
            if len(days) - 1 > len(best_days) - 1:
                best_days, best_prior, best_split = days, prior, (runs_c, split)
        relist_count = max(0, len(best_days) - 1)

        # Did the seller attach a Buy Now when the lot came back? That is the
        # readable response to a bid they refused — on lot 45704693 a $7,600
        # buy-now appeared at the same moment the sale moved 08-14 -> 08-21,
        # $1,400 above the $6,200 they had just declined.
        buy_now_at_relist = ""
        if best_split and best_split[1] is not None:
            runs_c, split = best_split
            before = any(o["buy_now_usd"] for _, _, o in runs_c[:split])
            after = any(o["buy_now_usd"] for _, _, o in runs_c[split:])
            buy_now_at_relist = ("added" if after and not before else
                                 "removed" if before and not after else
                                 "kept" if after else "none")

        first_bn = next((t for t, o in zip(seen_at, obs) if o["buy_now_usd"]), None)

        # Enrichment milestones. IAAI search-only pulls cannot speak to images
        # or ACV. Copart observations can: web records carry verified gallery
        # URLs/thumbnails and APIBara carries the richer media/ACV fields.
        full = (list(zip(seen_at, obs)) if platform == "copart" else
                [(t, o) for t, o in zip(seen_at, obs)
                 if o["detail_level"] == "full"])
        first_img = next((t for t, o in full if o["image_count"]), None)
        first_acv = next((t for t, o in full if o["acv"]), None)
        first_assigned = next(
            (t for t, o in zip(seen_at, obs) if o["assigned"]), None)
        versions = len({o["version"] for o in obs if o["version"] is not None})
        bid_conditions = []
        for o in obs:
            condition = o.get("bid_condition")
            if condition and (not bid_conditions or bid_conditions[-1] != condition):
                bid_conditions.append(condition)

        # gone = some cohort this lot was seen in has a LATER, non-truncated
        # snapshot that does not contain it. Truncated snapshots cannot prove
        # absence, and a cohort with no later snapshot simply says nothing.
        last_seen = seen_at[-1]
        tenant = next((o["tenant"] for o in reversed(obs) if o["tenant"]), None)
        kw = next((o["keyword"] for o in reversed(obs) if o["keyword"]), None)
        year = next((o["year"] for o in reversed(obs) if o["year"]), None)
        make = next((o["make"] for o in reversed(obs) if o["make"]), None)
        model = next((o["model"] for o in reversed(obs) if o["model"]), None)

        def can_prove_absent(m):
            """A snapshot may only call THIS lot absent if it was in scope.

            Three ways a snapshot can be disqualified: it was truncated, it
            searched a different market, or it ran different keywords. The last
            is what lets single-year and multi-year archives share one history
            without inventing departures.
            """
            if m["truncated"] or not m.get("absence_capable", True):
                return False
            markets = m.get("markets")
            if markets is not None and tenant is not None and tenant not in markets:
                return False
            kws = m.get("keywords")
            if kws is not None:
                # Unknown scope must never license an absence claim. If we do
                # not know which search found this lot, a keyword-scoped
                # snapshot cannot speak to it — that is how lot 45490663, a
                # 2018 A5 present in every 2018 pull including the newest, got
                # reported `gone` by a 2019-2023 pull that never looked for it.
                if kw is None or kw not in kws:
                    return False
            scoped_year = optional_year(year)
            if m.get("year_min") is not None:
                if scoped_year is None or scoped_year < m["year_min"]:
                    return False
            if m.get("year_max") is not None:
                if scoped_year is None or scoped_year > m["year_max"]:
                    return False
            if m.get("make") and (not make or make != m["make"]):
                return False
            if m.get("model") and (not model or model != m["model"]):
                return False
            return True

        # A complete missing snapshot bracketed by two sightings is a relist
        # even when Copart republishes the same auction date. This complements
        # the date-change signal above and, importantly, is evaluated only in
        # the cohort whose scope is entitled to prove absence.
        gap_relists = 0
        gap_split = None
        for cohort, runs_c in by_cohort.items():
            snaps = cohort_snaps.get(cohort, [])
            for idx in range(1, len(runs_c)):
                before_t, after_t = runs_c[idx - 1][0], runs_c[idx][0]
                if any(before_t < m["pulled_at"] < after_t and can_prove_absent(m)
                       for m in snaps):
                    gap_relists += 1
                    gap_split = gap_split or (runs_c, idx)
        if gap_relists > relist_count:
            relist_count = gap_relists
            if gap_split:
                runs_c, split = gap_split
                best_prior = runs_c[split - 1][2].get("auction_at") or ""
                best_split = gap_split
                before = any(o["buy_now_usd"] for _, _, o in runs_c[:split])
                after = any(o["buy_now_usd"] for _, _, o in runs_c[split:])
                buy_now_at_relist = ("added" if after and not before else
                                     "removed" if before and not after else
                                     "kept" if after else "none")

        departure_times = []
        for cohort, runs_c in by_cohort.items():
            last_here = runs_c[-1][0]
            departure_times.extend(
                m["pulled_at"] for m in cohort_snaps.get(cohort, [])
                if m["pulled_at"] > last_here and can_prove_absent(m)
            )
        gone_at = min(departure_times) if departure_times else None
        latest_active_seen = max(
            (t for t, o in zip(seen_at, obs) if o["active"]), default=""
        )
        gone = bool(gone_at and latest_active_seen < gone_at)

        # Reconcile every ended observation by sale event date, not archive
        # load order. Wide historical pulls are frequently generated after a
        # newer open snapshot but describe an older auction attempt.
        sold_events = [
            (t, o) for t, o in zip(seen_at, obs)
            if o["sold_price"] or o["sold_day"]
        ]
        sold_pair = max(sold_events, key=lambda pair: (
            str(pair[1]["sold_day"] or ""),
            int(str(pair[1]["sold_status"] or "").strip().lower() == "sold"),
            pair[0],
        ), default=None)
        sold_time, sold = sold_pair if sold_pair else ("", None)

        active_after_sale = False
        if sold:
            if platform == "copart":
                sold_day = str(sold["sold_day"] or "")[:10]
                for t, o in zip(seen_at, obs):
                    if not o["active"]:
                        continue
                    auction_day = str(o["auction_at"] or "")[:10]
                    if t > sold_time or (sold_day and auction_day > sold_day):
                        active_after_sale = True
                        break
                if active_after_sale:
                    gone = False
                    relist_count = max(relist_count, 1)
                else:
                    gone = True
            else:
                gone = True

        exit_reason = ""
        exit_price = ""
        exit_price_source = ""
        ended_buy_now = bool(sold and sold["buy_now_sold"] and sold["sold_price"])
        bought_now = ended_buy_now or any(
            o["buy_now_sold"] and (o["buy_now_usd"] or o["is_buy_now"])
            for o in obs
        )
        if gone:
            if bought_now:
                exit_reason = "sold_buy_now"
            elif sold:
                exit_reason = SOLD_REASON.get(
                    str(sold["sold_status"] or "").strip().lower(), "sold_at_auction")
            else:
                exit_reason = ("disappeared_from_copart" if platform == "copart"
                               else "unknown")
        if gone and sold and sold["sold_price"]:
            exit_price = sold["sold_price"]
            exit_price_source = (
                "apibara_ended_approval_bid"
                if platform == "copart" and
                str(sold["sold_status"] or "").strip().lower() == "sold on approval"
                else "apibara_ended"
            )
        elif exit_reason == "sold_buy_now":
            # A buy-now sale executes AT the buy-now price by definition, so the
            # figure is already in hand — no API call needed. Confirmed on lot
            # 45250068: we recorded buy_now $6,875 and the auction history later
            # reported the sale as $6,875.
            #
            # This matters because such lots never reach lot_sub_status=Ended:
            # searched across 2026-08-01..08-31 the lot was absent from every
            # ended page, so waiting for an `ended` pull to price it waits
            # forever.
            bn = next((o["buy_now_usd"] for o in reversed(obs) if o["buy_now_usd"]), None)
            if bn:
                exit_price, exit_price_source = bn, "buy_now_price"

        # Declined approval: the bid was not accepted, so the lot is coming back.
        #   confirmed  Apibara reported status 'Sold on Approval'
        #   inferred   we watched it relist, which means the prior run did not
        #              stick — decline or no-sale, indistinguishable from outside
        approval = any(
            str(o["sold_status"] or "").strip().lower() == "sold on approval"
            for o in obs
        )
        if platform == "copart" and approval and active_after_sale:
            declined = "confirmed"
        elif platform == "iaai" and approval:
            declined = "confirmed"
        elif relist_count:
            declined = "inferred"
        else:
            declined = ""

        out[lot] = {
            "first_seen_at": seen_at[0],
            "last_seen_at": last_seen,
            "snapshots": len(runs),
            "days_listed": _days(seen_at[0], last_seen),
            "relist_count": relist_count,
            "auction_at_prior": best_prior if relist_count else "",
            "buy_now_first_seen": first_bn or "",
            "exit_state": "gone" if gone else "still_listed",
            "exit_reason": exit_reason,
            "exit_price_usd": exit_price,
            "images_first_seen": first_img or "",
            "acv_first_seen": first_acv or "",
            "assigned_first_seen": first_assigned or "",
            "record_versions": versions,
            "exit_price_source": exit_price_source,
            "declined_approval": declined,
            "buy_now_at_relist": buy_now_at_relist,
            "bid_condition_first_seen": bid_conditions[0] if bid_conditions else "",
            "bid_condition_prior": bid_conditions[-2] if len(bid_conditions) > 1 else "",
            "bid_condition_changes": max(0, len(bid_conditions) - 1),
        }
    return out


def blank_history():
    return {c: "" for c in HISTORY_COLUMNS}


# --------------------------------------------------------------------------
# input discovery
# --------------------------------------------------------------------------
def _archive_rank(path):
    """Prefer the richest derivative of one logical Copart snapshot."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    media = 0
    for page in doc.get("pages") or []:
        for record in ((page.get("raw") or {}).get("data") or []):
            media += len(((record.get("media") or {}).get("items") or []))
    return (
        int(bool(doc.get("pages"))),
        int(bool(doc.get("adapted_at"))),
        media,
        doc.get("adapted_at") or "",
        Path(path).stat().st_mtime,
    )


def all_archives(platform=PLATFORM):
    platform = platform_of(explicit=platform)
    paths = sorted((p for b in BUCKETS
                    for layer in ("json-raw", "json-adapted")
                    for p in (DATA_DIR / b / layer / platform).glob("*.json")
                    if not p.name.startswith("iaaiweb_")
                    and not p.name.startswith("apiauctions_")),
                   key=lambda p: p.stat().st_mtime)
    if platform != "copart":
        return paths

    # Raw APIBara, vPIC-adapted APIBara, canonical web, and image-enriched web
    # can all describe the same pull. One generated_at + cohort + mode is one
    # snapshot; retaining every derivative would inflate snapshots and invent
    # changes that happened only during offline enrichment.
    best = {}
    for path in paths:
        try:
            meta = snapshot_meta(path, platform)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        key = (meta["cohort"], meta["mode"], meta["pulled_at"])
        if key not in best or _archive_rank(path) > _archive_rank(best[key]):
            best[key] = path
    return sorted(best.values(), key=lambda p: (
        snapshot_meta(p, platform)["pulled_at"], p.stat().st_mtime
    ))


def load_records(paths, platform=PLATFORM):
    """Load canonical records plus raw Copart web snapshots for history only."""
    platform = platform_of(explicit=platform)
    output = []
    flat = flattener(platform)
    for path in paths:
        path = Path(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        source = str(document.get("source") or "").lower()
        if platform == "copart" and source == "copart-web":
            pulled_at = document.get("generated_at")
            mode = str(document.get("mode") or "open").lower()
            for wrapper in document.get("records") or []:
                if not isinstance(wrapper, dict):
                    continue
                record = COPART_WEB.adapt_web_record(wrapper)
                if not COPART.is_us(record):
                    continue
                record["_source_file"] = path.name
                record["_pulled_at"] = pulled_at
                record["_mode"] = mode
                record["_platform"] = platform
                output.append(record)
            continue
        output.extend(flat.load_records([path]))
    return output


def expand_to_cohorts(paths, platform=None):
    """Widen a selection to every archive sharing a cohort with it.

    History over a single archive is trivially empty, and `--history` on the
    newest file alone is the easy mistake — so the cohorts of whatever was
    selected are filled in from everything else on disk.
    """
    # Resolve first: resolve_one() hands back a RELATIVE path when the file
    # exists relative to cwd, while all_archives() is absolute, and the two
    # forms of one file do not compare equal — which silently loaded the
    # selected archive twice and inflated `snapshots` by one.
    paths = [Path(p).resolve() for p in paths]
    if platform is None:
        doc = json.loads(paths[0].read_text(encoding="utf-8")) if paths else {}
        platform = platform_of(doc)
    metas = [snapshot_meta(p, platform) for p in paths]
    want = {m["cohort"] for m in metas}
    # Keyword overlap, not just cohort. All web archives share one cohort now
    # (scope is judged per lot), so matching on cohort alone would widen an
    # "Audi A5" run to every web archive on disk and drop 133 Mazda3 lots into
    # the CSV. Rows must stay inside the search that produced them; the broader
    # cohort is only for absence reasoning.
    want_kws = set()
    for m in metas:
        want_kws |= (m.get("keywords") or set())

    extra = []
    for p in all_archives(platform):
        m = snapshot_meta(p, platform)
        if m["cohort"] not in want:
            continue
        kws = m.get("keywords")
        if kws is not None and want_kws and not (kws & want_kws):
            continue
        extra.append(p.resolve())
    return sorted(set(paths) | set(extra), key=lambda p: p.stat().st_mtime)


def sold_context(exclude=(), platform=PLATFORM):
    """Apibara `ended` archives — the only place a sale PRICE exists.

    Deliberately not cohort-matched: a sale result resolves a lot whatever
    search found it, and `ended` archives are keyed by their own date window.
    These archives contribute history CONTEXT only. Feeding them into the row
    set instead would dump every sold Lexus into an Audi A5 CSV.
    """
    skip = {Path(p).resolve() for p in exclude}
    out = []
    for p in all_archives(platform):
        if p.resolve() in skip:
            continue
        try:
            if snapshot_meta(p, platform)["mode"] == "ended":
                out.append(p.resolve())
        except (ValueError, OSError):
            continue
    return out


def cache_path(cohort, mode="open", platform=PLATFORM):
    """One file per cohort, name guaranteed unique.

    The slug alone collides: every sold cohort begins with the same
    auction_date_from/to and lot_sub_status, so `make`/`model` fall past the
    80-char cut and six different models all hashed to one filename, silently
    overwriting each other. The digest is what actually keeps them apart.
    """
    bucket = {"ended": "sold", "open": "open", "live": "open"}.get(mode, "open")
    digest = hashlib.sha1(cohort.encode("utf-8")).hexdigest()[:8]
    return (DATA_DIR / bucket / "history" / platform /
            f"{slug(cohort)[:60]}_{digest}.json")


def write_cache(history, paths, cohort, mode="open", platform=PLATFORM):
    p = cache_path(cohort, mode, platform)
    p.parent.mkdir(parents=True, exist_ok=True)

    def plain(m):
        """snapshot_meta carries sets (markets, keywords) — JSON needs lists."""
        return {k: (sorted(v) if isinstance(v, set) else v) for k, v in m.items()}

    p.write_text(json.dumps({
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cohort": cohort,
        "note": "Derived cache — rebuildable from the archives listed below.",
        "platform": platform,
        "snapshots": [plain(snapshot_meta(x, platform)) for x in paths],
        "lots": history,
    }, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
def summarize(history):
    relisted = {k: v for k, v in history.items() if v["relist_count"]}
    gone = {k: v for k, v in history.items() if v["exit_state"] == "gone"}
    bn = {k: v for k, v in history.items() if v["buy_now_first_seen"]}
    multi = sum(1 for v in history.values() if v["snapshots"] > 1)
    print(f"\n  lots tracked:     {len(history)}  ({multi} seen in >1 snapshot)")
    print(f"  relisted:         {len(relisted)}")
    for lot, v in sorted(relisted.items()):
        print(f"      lot {lot}  {v['auction_at_prior'][:16]} -> "
              f"(x{v['relist_count']})  buy_now_first_seen={v['buy_now_first_seen'][:16] or '-'}")
    print(f"  left the site:    {len(gone)}")
    for lot, v in sorted(gone.items()):
        print(f"      lot {lot}  last seen {v['last_seen_at'][:16]}  reason={v['exit_reason']}")
    print(f"  ever had buy-now: {len(bn)}")


def pipeline(records, paths, history, platform=PLATFORM):
    """The incoming-supply view: what `Auction Not Assigned` inventory looks like
    right now, and how complete it is.

    Unassigned lots are the majority of inventory (56 of 65 on one A5 pull) and
    the only forward-looking part of it — everything else already has a sale
    date. They are also the least finished: IAAI back-fills photos and the
    insurer's ACV after listing, so a lot can be un-analysable today and ready
    tomorrow. This report is the daily read on that.
    """
    flat = flattener(platform)
    meta = {snapshot_meta(p, platform)["file"]: snapshot_meta(p, platform)
            for p in paths}
    latest = max((m["pulled_at"] for m in meta.values()), default="")
    prev = max((m["pulled_at"] for m in meta.values()
                if m["pulled_at"] < latest), default="")

    now = {}
    before = set()
    for v in records:
        m = meta.get(v.get("_source_file"))
        if not m:
            continue
        lot = str(v.get("lot_number") or "")
        if m["pulled_at"] == latest:
            now[lot] = v
        elif m["pulled_at"] <= prev:
            before.add(lot)

    if platform == "copart":
        print("\n" + "=" * 78)
        print(f"PIPELINE — Copart active inventory as of {latest[:16]}")
        print("=" * 78)
        states = {}
        for record in now.values():
            state = flat.listing_state(record) or "unknown"
            states[state] = states.get(state, 0) + 1
        full_vin = sum(bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}",
                                        str(v.get("vin") or "").upper()))
                       for v in now.values())
        multi_image = sum((flat.g(v, "media", "thumbs_count") or 0) > 1
                          for v in now.values())
        print(f"  {len(now)} lot(s) in view: {states}")
        print(f"  full VIN: {full_vin}/{len(now)}   "
              f"multi-image gallery: {multi_image}/{len(now)}")
        if prev:
            arrived = sorted(set(now) - before)
            print(f"  newly observed since {prev[:16]}: {len(arrived)}  "
                  f"{', '.join(arrived[:6])}")
        return

    unassigned = {k: v for k, v in now.items()
                  if flat.listing_state(v) == "Auction Not Assigned"}
    print("\n" + "=" * 78)
    print(f"PIPELINE — unassigned inventory as of {latest[:16]}")
    print("=" * 78)
    if not now:
        print("  nothing in the latest snapshot")
        return

    assigned = len(now) - len(unassigned)
    print(f"  {len(now)} lot(s) in view: {len(unassigned)} awaiting a sale date, "
          f"{assigned} scheduled")

    thin = [k for k, v in unassigned.items() if v.get("_detail_level") == "search"]
    if thin:
        print(f"  !! {len(thin)} unassigned lot(s) came from a search-only pull — "
              f"completeness below is unknown for them.\n"
              f"     Re-run pull_iaai_web_01.py with --details for the full picture.")

    full = {k: v for k, v in unassigned.items()
            if v.get("_detail_level") != "search"}
    if full:
        no_img = [k for k, v in full.items()
                  if not (flat.g(v, "media", "thumbs_count") or 0)]
        no_acv = [k for k, v in full.items()
                  if not flat.money_num(flat.sale_info(v).get("ActualCashValue"))]
        no_rep = [k for k, v in full.items()
                  if not flat.money_num(flat.attrs(v).get("EstRepairCost")
                                        or flat.sale_info(v).get("EstimatedRepairCost"))]
        print(f"\n  completeness of {len(full)} fully-pulled unassigned lot(s):")
        print(f"      no images       {len(no_img):>4}   {', '.join(sorted(no_img)[:6])}")
        print(f"      no ACV          {len(no_acv):>4}")
        print(f"      no repair est   {len(no_rep):>4}")

    if prev:
        arrived = sorted(set(unassigned) - before)
        newly_assigned = sorted(k for k in now
                                if k in before and flat.listing_state(now[k]) != "Auction Not Assigned"
                                and (history.get(k) or {}).get("assigned_first_seen") == latest)
        newly_img = sorted(k for k, h in history.items()
                           if h.get("images_first_seen") == latest and k in before)
        newly_acv = sorted(k for k, h in history.items()
                           if h.get("acv_first_seen") == latest and k in before)
        print(f"\n  since {prev[:16]}:")
        print(f"      newly listed        {len(arrived):>4}   {', '.join(arrived[:6])}")
        print(f"      gained images       {len(newly_img):>4}   {', '.join(newly_img[:6])}")
        print(f"      gained an ACV       {len(newly_acv):>4}   {', '.join(newly_acv[:6])}")
        print(f"      got a sale date     {len(newly_assigned):>4}   {', '.join(newly_assigned[:6])}")
    else:
        print("\n  (only one snapshot — re-run after the next pull for day-over-day change)")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(
        prog="lot_history_01.py",
        description="Cross-snapshot lot history (relists, buy-now, departures). "
                    "Offline — derived entirely from existing archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="archives (default: all)")
    ap.add_argument("--platform", choices=PLATFORMS, default=PLATFORM,
                    help="history platform (default: iaai)")
    ap.add_argument("--all", action="store_true", help="every archive (default)")
    ap.add_argument("--cache", action="store_true",
                    help="also write the derived history artifact under "
                         "data/<bucket>/history/<platform>/")
    ap.add_argument("--pipeline", action="store_true",
                    help="incoming-supply view: how complete the Auction Not "
                         "Assigned inventory is, and what changed since the "
                         "previous snapshot")
    args = ap.parse_args(argv)

    flat = flattener(args.platform)
    paths = ([flat.resolve_one(f) for f in args.files]
             if args.files else all_archives(args.platform))
    paths = expand_to_cohorts(paths, args.platform)
    if not paths:
        raise SystemExit("no archives found")

    metas = [snapshot_meta(p, args.platform) for p in paths]
    cohorts = {}
    for m in metas:
        cohorts.setdefault(m["cohort"], []).append(m)

    print("=" * 78)
    print(f"Lot history — {len(paths)} snapshot(s) across {len(cohorts)} cohort(s)")
    print("=" * 78)
    for c, ms in sorted(cohorts.items()):
        print(f"  {c}")
        for m in sorted(ms, key=lambda x: x["pulled_at"]):
            flag = "  [TRUNCATED — cannot prove absence]" if m["truncated"] else ""
            print(f"      {m['pulled_at'][:16]}  {m['file']}{flag}")

    records = load_records(paths, args.platform)
    history = build_history(records, paths, args.platform)
    summarize(history)
    if args.pipeline:
        pipeline(records, paths, history, args.platform)

    if args.cache:
        print()
        for c, ms in cohorts.items():
            sub = [p for p in paths
                   if snapshot_meta(p, args.platform)["cohort"] == c]
            # Recompute per cohort rather than slicing the global result: a
            # cohort's cache must describe only the lots that cohort observed,
            # and `gone` is meaningless across cohort boundaries anyway.
            files = {m["file"] for m in ms}
            recs = [r for r in records if r.get("_source_file") in files]
            lots = build_history(recs, sub, args.platform)
            out = write_cache(lots, sub, c, ms[0]["mode"], args.platform)
            print(f"  cache -> {out.name}  ({len(lots)} lots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
