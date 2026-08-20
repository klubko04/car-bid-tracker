"""Drive a persistent signed-in Chrome profile and ingest Copart galleries.

This WSL/Windows bridge removes the manual HAR handoff.  A Windows-local
PowerShell collector controls only the dedicated Chrome debugging profile,
writes a sanitized HAR-shaped capture under ``tmp/``, and this runner passes
that capture into ``copart_image_enrich_01.py``.

The first run opens a visible Chrome window.  Sign into Copart in that dedicated
window once if the desired gallery requires membership; the profile persists
under Windows LocalAppData for later runs.  Authentication cookies and request
headers never enter WSL or the generated JSON.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "analytics" / "scripts"
START_BROWSER = SCRIPTS / "start_copart_browser_01.ps1"
CAPTURE_BROWSER = SCRIPTS / "copart_browser_capture_01.ps1"
DEFAULT_CAPTURE_DIR = ROOT / "tmp" / "copart-browser-captures"

sys.path.insert(0, str(SCRIPTS))
import copart_image_enrich_01 as images  # noqa: E402


def windows_path(path):
    result = subprocess.run(
        ["wslpath", "-w", str(Path(path).resolve())],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def powershell_file(script, *args):
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", windows_path(script), *map(str, args),
    ]
    return subprocess.run(command, check=True)


def record_index(document):
    return {
        images.normalize_lot(record.get("lot_number")): record
        for record in images.records(document)
        if images.normalize_lot(record.get("lot_number"))
    }


def timestamp():
    return dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Capture Copart galleries through persistent Windows Chrome and enrich JSON."
    )
    parser.add_argument("file", help="canonical Copart json-adapted archive")
    parser.add_argument("--lot", action="append", default=[],
                        help="lot number to capture; repeat for multiple lots")
    parser.add_argument("--all-incomplete", action="store_true",
                        help="select every record currently carrying at most one image")
    parser.add_argument("--lots-from-csv", metavar="CSV",
                        help="restrict captures to lots selected by this csv-cut")
    parser.add_argument("--max-lots", type=int, default=1,
                        help="maximum captures this run; 0 means all selected (default: 1)")
    parser.add_argument("--capture-seconds", type=int, default=35,
                        help="seconds allowed for page and gallery capture (default: 35)")
    parser.add_argument("--delay", type=float, default=10.0,
                        help="seconds between lot navigations in each worker lane (default: 10)")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4, 5), default=1,
                        help="parallel isolated Chrome tabs (default: 1; maximum: 5)")
    parser.add_argument("--worker-stagger", type=float, default=2.0,
                        help="seconds between starting worker lanes (default: 2)")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--capture-dir", default=str(DEFAULT_CAPTURE_DIR))
    parser.add_argument("--force", action="store_true",
                        help="capture lots that already have multiple images")
    parser.add_argument("--out", help="enriched JSON destination")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    source = Path(args.file).expanduser().resolve()
    document = json.loads(source.read_text(encoding="utf-8"))
    if str(document.get("platform") or "").casefold() != images.PLATFORM:
        raise SystemExit(f"{source.name}: expected platform='copart'")
    indexed = record_index(document)
    try:
        allowed_order = (images.lot_numbers_from_csv(args.lots_from_csv)
                         if args.lots_from_csv else None)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    allowed_lots = set(allowed_order) if allowed_order is not None else None

    selected = []
    seen = set()
    for lot in args.lot:
        normalized = images.normalize_lot(lot)
        if normalized not in indexed:
            raise SystemExit(f"lot {normalized} was not present in {source.name}")
        if normalized not in seen:
            if allowed_lots is not None and normalized not in allowed_lots:
                raise SystemExit(
                    f"lot {normalized} was not selected by {args.lots_from_csv}"
                )
            selected.append(normalized)
            seen.add(normalized)
    if args.all_incomplete:
        lot_order = allowed_order if allowed_order is not None else indexed
        for lot in lot_order:
            record = indexed.get(lot)
            if record is None:
                raise SystemExit(
                    f"lot {lot} from {args.lots_from_csv} was not present in {source.name}"
                )
            if images.needs_gallery_capture(record) and lot not in seen:
                selected.append(lot)
                seen.add(lot)
    if not selected:
        raise SystemExit("select at least one --lot or use --all-incomplete")
    if not args.force:
        selected = [lot for lot in selected
                    if images.needs_gallery_capture(indexed[lot])]
    if args.max_lots:
        selected = selected[:args.max_lots]
    if not selected:
        raise SystemExit("selected lots already have multiple images; use --force to recapture")

    powershell_file(
        START_BROWSER, "-Port", args.port,
        "-StartUrl", f"https://www.copart.com/lot/{selected[0]}",
    )

    capture_dir = Path(args.capture_dir).expanduser().resolve()
    capture_dir.mkdir(parents=True, exist_ok=True)

    def capture_one(lot):
        capture = capture_dir / f"copart-{lot}-{timestamp()}.har"
        command = [
            "-Lot", lot,
            "-Out", windows_path(capture),
            "-Port", args.port,
            "-CaptureSeconds", args.capture_seconds,
        ]
        # Parallel workers must never navigate the same reusable Copart tab.
        # Dedicated targets share the signed-in Chrome cookie jar but have
        # independent CDP sockets and are closed by the PowerShell collector.
        if args.workers > 1:
            command.append("-DedicatedTab")
        powershell_file(
            CAPTURE_BROWSER,
            *command,
        )
        feed = images.parse_browser_har(capture, indexed[lot])
        if feed["identity_conflicts"]:
            raise SystemExit(
                f"lot {lot}: capture identity conflict {feed['identity_conflicts']}"
            )
        print(
            f"  lot {lot}: {feed['image_count']} image(s), "
            f"{feed['explicit_url_count']} explicit media URL(s)"
        )
        return lot, capture

    def capture_lane(lane_number, lots):
        if lane_number and args.worker_stagger > 0:
            time.sleep(lane_number * args.worker_stagger)
        completed = []
        for index, lot in enumerate(lots):
            if index and args.delay > 0:
                time.sleep(args.delay)
            completed.append(capture_one(lot))
        return completed

    worker_count = min(args.workers, len(selected))
    lanes = [selected[index::worker_count] for index in range(worker_count)]
    if worker_count == 1:
        captures = capture_lane(0, lanes[0])
    else:
        print(
            f"Copart browser capture: {len(selected)} lot(s) across "
            f"{worker_count} isolated tab worker(s)"
        )
        captures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(capture_lane, lane_number, lots)
                for lane_number, lots in enumerate(lanes)
            ]
            for future in concurrent.futures.as_completed(futures):
                captures.extend(future.result())
        order = {lot: index for index, lot in enumerate(selected)}
        captures.sort(key=lambda item: order[item[0]])

    destination = (
        Path(args.out).expanduser().resolve() if args.out else
        source.parent / f"browser_{source.name}"
    )
    enrich_args = [str(source)]
    for lot, capture in captures:
        enrich_args.extend(["--har", f"{lot}={capture}"])
    if args.force:
        enrich_args.append("--force")
    enrich_args.extend(["--out", str(destination)])
    images.main(enrich_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
