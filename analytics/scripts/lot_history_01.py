"""
Cross-snapshot lot history — what changed about a lot between pulls.

Used by `data_pull_01.py --history`; also runnable standalone to inspect or
materialise the history artifact:

    python analytics/scripts/lot_history_01.py --all
    python analytics/scripts/lot_history_01.py --all --cache
    python analytics/scripts/lot_history_01.py ARCHIVE.json ARCHIVE2.json

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

import apibara_json2csv_iaai_01 as F   # noqa: E402  (single source of truth for state)

DATA_DIR = ROOT / "analytics" / "data"
BUCKETS = ("sold", "open")
PLATFORM = "iaai"

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
]


# --------------------------------------------------------------------------
# cohort identity
# --------------------------------------------------------------------------
def cohort_key(doc):
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
    source = doc.get("source") or "apibara"
    if source in ("iaai-web", "iaai-web-adapted"):
        desc = "iaai"
    else:
        params = dict(doc.get("server_params") or {})
        for noise in ("per_page", "cursor"):
            params.pop(noise, None)
        desc = "|".join(f"{k}={params[k]}" for k in sorted(params)) or "?"
    return f"{'web' if 'web' in source else 'apibara'}::{desc}"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:80]


def snapshot_meta(path):
    """Envelope facts about one archive, without loading its records twice."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    truncated = bool((doc.get("counts") or {}).get("truncated")) or bool(
        (doc.get("search_params") or {}).get("truncated"))
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
    return {
        "file": Path(path).name,
        "pulled_at": doc.get("generated_at") or "",
        "cohort": cohort_key(doc),
        "truncated": truncated,
        "markets": markets,
        "keywords": kws,
        "mode": doc.get("mode", "open"),
        "source": doc.get("source") or "apibara",
    }


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------
def observe(v):
    """One lot at one moment, reduced to the fields history cares about."""
    return {
        "auction_at": F.g(v, "auction", "auction_at") or v.get("ad"),
        "listing_state": F.listing_state(v),
        "buy_now_usd": F.money_num(F.g(v, "pricing", "buy_now_usd")),
        # Apibara's sold_buy_now is not trustworthy alone: on 2 of 5 ended A5
        # lots it was True while is_buy_now was False, buy_now_usd was null and
        # the sale price was $0. Corroborate it with an actual buy-now price or
        # the is_buy_now flag before calling an exit a buy-now sale.
        "buy_now_sold": bool(F.g(v, "auction", "sold_buy_now")),
        "is_buy_now": bool(F.g(v, "auction", "is_buy_now")),
        "timed_close_at": F.g(v, "auction", "timed_end_at"),
        # Only an Apibara `ended` pull carries these. iaai.com never publishes a
        # sale result: a lot simply disappears, which is why a web-only history
        # can say "gone" but never "gone for $7,850".
        # usd(), not money_num(): a CAD sale price must not become exit_price_usd.
        "sold_price": F.usd(v, F.g(v, "pricing", "last_sold_price_usd")),
        "sold_day": F.g(v, "auction", "last_sold_day"),
        "sold_status": F.g(v, "auction", "last_sold_status"),
        # Enrichment: an `Auction Not Assigned` lot is not a finished record.
        # IAAI back-fills photos and the insurer's ACV after listing, so a lot
        # can be un-analysable one day and complete the next.
        "image_count": F.g(v, "media", "thumbs_count") or 0,
        "acv": F.money_num(F.sale_info(v).get("ActualCashValue")),
        # IAAI's own edit counter. CreatedDateTime/ModifiedDateTime are NOT
        # arrival timestamps — they are rewritten on every record update (12/12
        # observed lots moved their "created" date between two pulls while
        # VersionId incremented), so this is the only honest change signal, and
        # first_seen_at below is the only honest age.
        "version": F.attrs(v).get("VersionId"),
        "detail_level": v.get("_detail_level") or "full",
        # 'US' / 'CA', from the item id. Decides which later snapshots are
        # entitled to call this lot absent.
        "tenant": (str(F.clean(F.attrs(v).get("Id"))
                       or F.clean(v.get("_web_item_id")) or "")
                   .rsplit("~", 1)[-1].upper() or None),
        "keyword": (str(v.get("_web_keyword")).strip().lower()
                    if v.get("_web_keyword") else None),
    }


def _days(a, b):
    """Whole-ish days between two ISO timestamps, or None."""
    try:
        d0 = dt.datetime.fromisoformat(str(a))
        d1 = dt.datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return None
    return round((d1 - d0).total_seconds() / 86400, 1)


def build_history(records, paths):
    """-> {lot_number: {history columns}}.

    `records` must already carry `_source_file` (stage 2's load_records does
    this). `paths` supplies the envelope facts those filenames refer to.
    """
    meta = {}
    for p in paths:
        m = snapshot_meta(p)
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
            (m["pulled_at"], m, observe(v)))
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

        # Enrichment milestones. Only `full` observations can speak to images or
        # ACV — a search-only pull has neither, and treating its silence as
        # "no images" would reset the milestone on every cheap pull.
        full = [(t, o) for t, o in zip(seen_at, obs) if o["detail_level"] == "full"]
        first_img = next((t for t, o in full if o["image_count"]), None)
        first_acv = next((t for t, o in full if o["acv"]), None)
        first_assigned = next(
            (t for t, o in zip(seen_at, obs)
             if o["listing_state"] and o["listing_state"] != "Auction Not Assigned"), None)
        versions = len({o["version"] for o in obs if o["version"] is not None})

        # gone = some cohort this lot was seen in has a LATER, non-truncated
        # snapshot that does not contain it. Truncated snapshots cannot prove
        # absence, and a cohort with no later snapshot simply says nothing.
        last_seen = seen_at[-1]
        tenant = next((o["tenant"] for o in reversed(obs) if o["tenant"]), None)
        kw = next((o["keyword"] for o in reversed(obs) if o["keyword"]), None)

        def can_prove_absent(m):
            """A snapshot may only call THIS lot absent if it was in scope.

            Three ways a snapshot can be disqualified: it was truncated, it
            searched a different market, or it ran different keywords. The last
            is what lets single-year and multi-year archives share one history
            without inventing departures.
            """
            if m["truncated"]:
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
            return True

        gone = False
        for cohort, runs_c in by_cohort.items():
            last_here = runs_c[-1][0]
            if any(m["pulled_at"] > last_here and can_prove_absent(m)
                   for m in cohort_snaps.get(cohort, [])):
                gone = True
                break

        # An Apibara `ended` sighting is authoritative and outranks absence:
        # the lot demonstrably concluded, and it carries the price. Absence only
        # ever supports "gone, reason unknown".
        sold = next((o for o in reversed(obs) if o["sold_price"] or o["sold_day"]), None)
        if sold:
            gone = True

        exit_reason = ""
        exit_price = ""
        exit_price_source = ""
        bought_now = any(o["buy_now_sold"] and (o["buy_now_usd"] or o["is_buy_now"])
                         for o in obs)
        if gone:
            if bought_now:
                exit_reason = "sold_buy_now"
            elif sold:
                exit_reason = SOLD_REASON.get(
                    str(sold["sold_status"] or "").strip().lower(), "sold_at_auction")
            else:
                exit_reason = "unknown"
        if sold and sold["sold_price"]:
            exit_price, exit_price_source = sold["sold_price"], "apibara_ended"
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
        if any(str(o["sold_status"] or "").strip().lower() == "sold on approval"
               for o in obs):
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
        }
    return out


def blank_history():
    return {c: "" for c in HISTORY_COLUMNS}


# --------------------------------------------------------------------------
# input discovery
# --------------------------------------------------------------------------
def all_archives():
    return sorted((p for b in BUCKETS
                   for layer in ("json-raw", "json-adapted")
                   for p in (DATA_DIR / b / layer / PLATFORM).glob("*.json")
                   if not p.name.startswith("iaaiweb_")),
                  key=lambda p: p.stat().st_mtime)


def expand_to_cohorts(paths):
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
    metas = [snapshot_meta(p) for p in paths]
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
    for p in all_archives():
        m = snapshot_meta(p)
        if m["cohort"] not in want:
            continue
        kws = m.get("keywords")
        if kws is not None and want_kws and not (kws & want_kws):
            continue
        extra.append(p.resolve())
    return sorted(set(paths) | set(extra), key=lambda p: p.stat().st_mtime)


def sold_context(exclude=()):
    """Apibara `ended` archives — the only place a sale PRICE exists.

    Deliberately not cohort-matched: a sale result resolves a lot whatever
    search found it, and `ended` archives are keyed by their own date window.
    These archives contribute history CONTEXT only. Feeding them into the row
    set instead would dump every sold Lexus into an Audi A5 CSV.
    """
    skip = {Path(p).resolve() for p in exclude}
    out = []
    for p in all_archives():
        if p.resolve() in skip:
            continue
        try:
            if snapshot_meta(p)["mode"] == "ended":
                out.append(p.resolve())
        except (ValueError, OSError):
            continue
    return out


def cache_path(cohort, mode="open"):
    """One file per cohort, name guaranteed unique.

    The slug alone collides: every sold cohort begins with the same
    auction_date_from/to and lot_sub_status, so `make`/`model` fall past the
    80-char cut and six different models all hashed to one filename, silently
    overwriting each other. The digest is what actually keeps them apart.
    """
    bucket = {"ended": "sold", "open": "open", "live": "open"}.get(mode, "open")
    digest = hashlib.sha1(cohort.encode("utf-8")).hexdigest()[:8]
    return (DATA_DIR / bucket / "history" / PLATFORM /
            f"{slug(cohort)[:60]}_{digest}.json")


def write_cache(history, paths, cohort, mode="open"):
    p = cache_path(cohort, mode)
    p.parent.mkdir(parents=True, exist_ok=True)

    def plain(m):
        """snapshot_meta carries sets (markets, keywords) — JSON needs lists."""
        return {k: (sorted(v) if isinstance(v, set) else v) for k, v in m.items()}

    p.write_text(json.dumps({
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cohort": cohort,
        "note": "Derived cache — rebuildable from the archives listed below.",
        "snapshots": [plain(snapshot_meta(x)) for x in paths],
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


def pipeline(records, paths, history):
    """The incoming-supply view: what `Auction Not Assigned` inventory looks like
    right now, and how complete it is.

    Unassigned lots are the majority of inventory (56 of 65 on one A5 pull) and
    the only forward-looking part of it — everything else already has a sale
    date. They are also the least finished: IAAI back-fills photos and the
    insurer's ACV after listing, so a lot can be un-analysable today and ready
    tomorrow. This report is the daily read on that.
    """
    meta = {snapshot_meta(p)["file"]: snapshot_meta(p) for p in paths}
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

    unassigned = {k: v for k, v in now.items()
                  if F.listing_state(v) == "Auction Not Assigned"}
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
        no_img = [k for k, v in full.items() if not (F.g(v, "media", "thumbs_count") or 0)]
        no_acv = [k for k, v in full.items()
                  if not F.money_num(F.sale_info(v).get("ActualCashValue"))]
        no_rep = [k for k, v in full.items()
                  if not F.money_num(F.attrs(v).get("EstRepairCost")
                                     or F.sale_info(v).get("EstimatedRepairCost"))]
        print(f"\n  completeness of {len(full)} fully-pulled unassigned lot(s):")
        print(f"      no images       {len(no_img):>4}   {', '.join(sorted(no_img)[:6])}")
        print(f"      no ACV          {len(no_acv):>4}")
        print(f"      no repair est   {len(no_rep):>4}")

    if prev:
        arrived = sorted(set(unassigned) - before)
        newly_assigned = sorted(k for k in now
                                if k in before and F.listing_state(now[k]) != "Auction Not Assigned"
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
    ap.add_argument("--all", action="store_true", help="every archive (default)")
    ap.add_argument("--cache", action="store_true",
                    help="also write the derived history artifact under "
                         "data/<bucket>/history/<platform>/")
    ap.add_argument("--pipeline", action="store_true",
                    help="incoming-supply view: how complete the Auction Not "
                         "Assigned inventory is, and what changed since the "
                         "previous snapshot")
    args = ap.parse_args(argv)

    paths = [F.resolve_one(f) for f in args.files] if args.files else all_archives()
    paths = expand_to_cohorts(paths)
    if not paths:
        raise SystemExit("no archives found")

    metas = [snapshot_meta(p) for p in paths]
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

    records = F.load_records(paths)
    history = build_history(records, paths)
    summarize(history)
    if args.pipeline:
        pipeline(records, paths, history)

    if args.cache:
        print()
        for c, ms in cohorts.items():
            sub = [p for p in paths if snapshot_meta(p)["cohort"] == c]
            # Recompute per cohort rather than slicing the global result: a
            # cohort's cache must describe only the lots that cohort observed,
            # and `gone` is meaningless across cohort boundaries anyway.
            files = {m["file"] for m in ms}
            recs = [r for r in records if r.get("_source_file") in files]
            lots = build_history(recs, sub)
            out = write_cache(lots, sub, c, ms[0]["mode"])
            print(f"  cache -> {out.name}  ({len(lots)} lots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
