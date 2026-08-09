"""
Sold-lot photo extraction + tiered archive builder.

Input: test_run/apibara_sold_*.json -- the raw output of the live probe
scripts (test/test_apibara_sold_iaai_01.py, test/test_apibara_sold_copart_01.py,
and their _02 siblings), run manually since each call spends live Apibara
quota. This module does NOT call Apibara itself.

Two outputs, per the two-part spec:

  1. Flat drop -- every sold VIN's full-res photos, one folder per VIN:
         images/flat/IAAI/{VIN}/*.jpg
         images/flat/Copart/{VIN}/*.jpg

  2. Curated archive -- only VINs matching a want-list tier (app/tier.py),
     organized as:
         images/tiered/{Tier}/{Make-Model}/{IAAI|Copart}/{distance}mi/
             {YYYY-MM}/{VIN}/images/*.jpg
     where {distance} buckets road-miles from Federal Way 98003
     (app/branch_geo.py) and {YYYY-MM} is the VIN's LAST sold date -- a VIN
     sold more than once across input files is deduped to its latest
     last_sold_day before either output is built.

     A CSV manifest (images/manifest.csv) records every sold VIN found, its
     resolved tier/distance/month/folder path, and whether it was placed in
     the tiered archive or skipped (make/model not on the want-list).

Run:
    python -m app.image_pipeline                # download photos + build both outputs
    python -m app.image_pipeline --table-only    # just (re)build the manifest, no downloads
"""
import csv
import json
import re
import sys
from pathlib import Path

import httpx

from app.branch_geo import distance_bucket
from app.tier import classify

ROOT = Path(__file__).resolve().parent.parent
TEST_RUN_DIR = ROOT / "test_run"
IMAGES_ROOT = ROOT / "images"
FLAT_DIR = IMAGES_ROOT / "flat"
TREE_DIR = IMAGES_ROOT / "tiered"
MANIFEST_PATH = IMAGES_ROOT / "manifest.csv"

PLATFORM_DIR = {"iaai": "IAAI", "copart": "Copart"}
MONTH_FLOOR = "2025-08"  # archive's intended lower bound, per spec; out-of-range is flagged, not dropped


def _large_urls(rec: dict) -> list:
    media = rec.get("media") or {}
    items = media.get("items") or []
    urls = [it.get("large") for it in items if it.get("large")]
    return urls or list(media.get("thumbs") or [])


def _engine_raw(rec: dict) -> str:
    engine = (rec.get("vehicle_specs") or {}).get("engine") or {}
    return engine.get("raw") or ""


def _extract(rec: dict, source_file: str):
    vin = (rec.get("vin") or "").strip().upper()
    if not vin:
        return None
    platform = "iaai" if "iaa" in (rec.get("platform") or "").lower() else "copart"
    sold_day = (rec.get("auction") or {}).get("last_sold_day")
    if not sold_day:
        return None  # not a confirmed sale in this record
    urls = _large_urls(rec)
    if not urls:
        return None
    return {
        "vin": vin,
        "platform": platform,
        "lot_number": str(rec.get("lot_number") or ""),
        "year": rec.get("year"),
        "make": (rec.get("make") or "").title(),
        "model": rec.get("model") or "",
        "location": (rec.get("location") or {}).get("display") or "",
        "sold_day": sold_day,
        "engine_raw": _engine_raw(rec),
        "image_urls": urls,
        "source_file": source_file,
    }


def load_sold_records(test_run_dir: Path = TEST_RUN_DIR) -> list:
    records = []
    for path in sorted(test_run_dir.glob("apibara_sold_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! skipping {path.name}: {e}")
            continue
        for page in data.get("pages") or []:
            if page.get("status") != 200:
                continue
            for rec in (page.get("raw") or {}).get("data") or []:
                extracted = _extract(rec, path.name)
                if extracted:
                    records.append(extracted)
    return records


def dedup_by_vin(records: list) -> list:
    """A VIN can appear sold in more than one auction run; keep the record
    whose last_sold_day is latest -- the 'sold action date' the spec asks
    to treat as authoritative when a car sold more than once."""
    best = {}
    for rec in records:
        cur = best.get(rec["vin"])
        if cur is None or rec["sold_day"] > cur["sold_day"]:
            best[rec["vin"]] = rec
    return list(best.values())


def _ext(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"


def _download(client: httpx.Client, url: str, out: Path) -> bool:
    try:
        resp = client.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        out.write_bytes(resp.content)
        return True
    except httpx.HTTPError as e:
        print(f"  ! failed {url}: {e}")
        return False


def download_flat(records: list, client: httpx.Client) -> None:
    """Part 1: IAAI/{VIN}/ + Copart/{VIN}/ folders of full-res photos."""
    for rec in records:
        dest = FLAT_DIR / PLATFORM_DIR[rec["platform"]] / rec["vin"]
        dest.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(rec["image_urls"], 1):
            out = dest / f"{i:02d}{_ext(url)}"
            if out.exists():
                continue
            _download(client, url, out)


def build_manifest(records: list) -> list:
    rows = []
    for rec in records:
        tier = classify(rec["make"], rec["model"], rec["year"], rec["engine_raw"])
        bucket = distance_bucket(rec["location"])
        month = rec["sold_day"][:7] if rec["sold_day"] else "unknown"
        make_model = f"{rec['make']}-{rec['model']}".replace(" ", "")
        platform_dir = PLATFORM_DIR[rec["platform"]]
        placed = tier is not None
        rel_path = (str(Path(tier, make_model, platform_dir, bucket, month, rec["vin"], "images"))
                    if placed else "")
        rows.append({
            "vin": rec["vin"],
            "platform": platform_dir,
            "make": rec["make"],
            "model": rec["model"],
            "year": rec["year"],
            "tier": tier or "Unclassified",
            "distance_bucket": bucket,
            "sold_month": month,
            "sold_day": rec["sold_day"],
            "location": rec["location"],
            "num_images": len(rec["image_urls"]),
            "placed_in_tiered_archive": placed,
            "out_of_month_floor": month != "unknown" and month < MONTH_FLOOR,
            "folder_path": rel_path,
        })
    rows.sort(key=lambda r: (r["tier"], r["make"], r["model"], r["vin"]))
    return rows


def write_manifest_csv(rows: list, path: Path = MANIFEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "vin", "platform", "make", "model", "year", "tier", "distance_bucket",
        "sold_month", "sold_day", "location", "num_images",
        "placed_in_tiered_archive", "out_of_month_floor", "folder_path",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_tiered_tree(records: list, rows_by_vin: dict) -> None:
    """Part 2: materialize the Tier/Make-Model/Platform/Distance/Month/VIN/images
    tree and copy each placed VIN's flat images into its leaf folder."""
    for rec in records:
        row = rows_by_vin.get(rec["vin"])
        if row is None or not row["placed_in_tiered_archive"]:
            continue
        dest = TREE_DIR / row["folder_path"]
        dest.mkdir(parents=True, exist_ok=True)
        src = FLAT_DIR / PLATFORM_DIR[rec["platform"]] / rec["vin"]
        if not src.is_dir():
            continue
        for img in src.iterdir():
            target = dest / img.name
            if not target.exists():
                target.write_bytes(img.read_bytes())


def main(table_only: bool = False) -> None:
    records = dedup_by_vin(load_sold_records())
    print(f"Loaded {len(records)} sold VIN(s) with photos from {TEST_RUN_DIR}")
    if not records:
        print(f"No apibara_sold_*.json found in {TEST_RUN_DIR}. Run the probe scripts "
              f"in test/ first (each spends live Apibara quota), e.g.:\n"
              f"  python test/test_apibara_sold_iaai_01.py\n"
              f"  python test/test_apibara_sold_copart_01.py")
        return

    rows = build_manifest(records)
    rows_by_vin = {r["vin"]: r for r in rows}
    placed = sum(1 for r in rows if r["placed_in_tiered_archive"])
    out_of_floor = sum(1 for r in rows if r["out_of_month_floor"])
    print(f"{placed}/{len(rows)} VIN(s) matched a want-list tier and will be archived")
    if out_of_floor:
        print(f"  note: {out_of_floor} sold before the {MONTH_FLOOR} floor -- still placed, flagged in the manifest")

    write_manifest_csv(rows)
    print(f"Manifest -> {MANIFEST_PATH}")

    if table_only:
        return

    with httpx.Client() as client:
        download_flat(records, client)
    print(f"Flat images -> {FLAT_DIR}")

    build_tiered_tree(records, rows_by_vin)
    print(f"Tiered archive -> {TREE_DIR}")


if __name__ == "__main__":
    main(table_only="--table-only" in sys.argv)
