"""
Flat photo-drop test for the sold-lot image pipeline — Part 1 only.

Standalone copy of the extract + download half of app/image_pipeline.py, kept
in test/ so app/ stays untouched while the pipeline is being shaken out. It
imports nothing from app/ and makes **zero Apibara calls** — it reads probe
dumps already sitting in test_run/ and only GETs image CDN URLs found inside
them, so it spends no API quota.

Scope
-----
Builds ONLY the flat drop, at ORIGINAL resolution:

    images/flat/{IAAI|Copart}/{VIN}/01.jpg …

IAAI's `media.items[].large` is a resized render (845x633); stripping the
`&width=…&height=…` tail off the resizer URL yields the original (~2576x1932).
See _full_res_url(). Use --force to replace resized files already on disk.

The tiered archive (images/tiered/{Tier}/{Make-Model}/…) is deliberately NOT
built here — build_tiered_tree() copies whole files rather than hardlinking, so
every archived VIN's photos would exist twice on disk. That is post-test work.

Input shapes
------------
Probe dumps come in two shapes and this handles both, which app/image_pipeline.py
currently does not:

    "pages" list   apibara_sold_iaai_01.json, apibara_sold_copart_01.json
    top-level raw  apibara_sold_iaai_02.json, apibara_sold_results01.json

Run
---
    python test/test_image_pipeline.py                          # every test_run/apibara_sold_*.json
    python test/test_image_pipeline.py test_run/apibara_sold_iaai_02.json
    python test/test_image_pipeline.py --dry-run                # resolve + plan, no HTTP
    python test/test_image_pipeline.py --limit 3 --max-images 4 # cap a smoke run
    python test/test_image_pipeline.py --out /tmp/flat_probe    # write somewhere disposable
    python test/test_image_pipeline.py --force                  # replace files already on disk

    --progress overall    one pooled line for the whole run, with ETA (default)
    --progress detailed   one line per VIN, live while it downloads
    --progress none       no progress output, just the final summary

Checks (exit code 1 if any FAIL)
--------------------------------
  * every planned file exists on disk and is non-empty
  * magic bytes match the extension family (JPEG/PNG/WebP/GIF), so an HTML
    error page saved as .jpg is caught
  * layout is exactly {IAAI|Copart}/{VIN}/NN.ext
  * no IAAI URL still carries a `&width=` tail (the resize fix held)
  * a second pass re-downloads nothing (skip-if-exists actually works)

VINs that are not 17 characters are reported as WARN, not FAIL — bad VINs in
the upstream data are a data problem, not a pipeline problem.
"""
import argparse
import json
import re
import shutil
import struct
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent   # repo root
TEST_RUN_DIR = ROOT / "test_run"
DEFAULT_OUT = ROOT / "images" / "flat"

PLATFORM_DIR = {"iaai": "IAAI", "copart": "Copart"}

# Both CDNs (vis.iaai.com, cs.copart.com) serve fine on httpx's default UA as of
# 2026-08; a browser UA is sent anyway so a future bot filter doesn't break the run.
USER_AGENT =("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MAGIC = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".webp": [b"RIFF"],          # RIFF....WEBP — checked with the offset-8 tag below
}


# --------------------------------------------------------------------------
# extraction — mirrors app/image_pipeline.py, plus the flat-shape handling
# --------------------------------------------------------------------------
def _full_res_url(url: str) -> str:
    """IAAI's `large` URL is a *resized* render, not the original photo:

        https://vis.iaai.com/resizer?imageKeys=44190715~SID~I4&width=845&height=633

    Dropping everything from the first `&` leaves the bare imageKeys lookup,
    which serves the untouched original — measured 2576x1932 / 722 KB against
    845x633 / 116 KB for the resized form, ~9x the pixels.

    Only vis.iaai.com is rewritten. Copart's cs.copart.com URLs carry no query
    string at all (408/408 observed across the test_run dumps), so they are
    already originals and are passed through untouched.
    """
    if "vis.iaai.com" in url and "&" in url:
        return url.split("&", 1)[0]
    return url


def _large_urls(rec: dict) -> list:
    media = rec.get("media") or {}
    items = media.get("items") or []
    urls = [it.get("large") for it in items if it.get("large")]
    urls = urls or list(media.get("thumbs") or [])
    return [_full_res_url(u) for u in urls]


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
        "image_urls": urls,
        "source_file": source_file,
    }


def _pages(data: dict) -> list:
    """Normalize both dump shapes to a list of {"status", "raw"} pages."""
    pages = data.get("pages")
    if isinstance(pages, list):
        return pages
    if "raw" in data:
        return [{"status": data.get("status", 200), "raw": data.get("raw")}]
    return []


def load_from_paths(paths) -> tuple:
    """-> (records, per-file stats). Stats make a zero-record file diagnosable."""
    records, stats = [], []
    for path in paths:
        path = Path(path)
        st = {"file": path.name, "shape": "?", "rows": 0, "kept": 0, "note": ""}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            st["note"] = f"unreadable: {e}"
            stats.append(st)
            continue
        st["shape"] = "pages" if isinstance(data.get("pages"), list) else "flat"
        pages = _pages(data)
        if not pages:
            st["note"] = "no pages / no raw block"
        for page in pages:
            if page.get("status") != 200:
                st["note"] = f"HTTP {page.get('status')} page skipped"
                continue
            for rec in (page.get("raw") or {}).get("data") or []:
                st["rows"] += 1
                extracted = _extract(rec, path.name)
                if extracted:
                    records.append(extracted)
                    st["kept"] += 1
        if st["rows"] and not st["kept"] and not st["note"]:
            st["note"] = "all rows lacked vin / last_sold_day / photos"
        stats.append(st)
    return records, stats


def dedup_by_vin(records: list) -> list:
    best = {}
    for rec in records:
        cur = best.get(rec["vin"])
        if cur is None or rec["sold_day"] > cur["sold_day"]:
            best[rec["vin"]] = rec
    return list(best.values())


# --------------------------------------------------------------------------
# progress reporting
# --------------------------------------------------------------------------
def _fmt_bytes(n: float) -> str:
    return f"{n / 1e6:.1f}MB" if n >= 1e6 else f"{n / 1e3:.0f}KB"


def _fmt_secs(s: float) -> str:
    if s < 0 or s != s or s > 86400:          # negative / NaN / absurd
        return "--"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"


def _bar(done: int, total: int, width: int) -> str:
    filled = int(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)


class Progress:
    """Live text progress for the download pass.

    mode "none"      nothing but the final summary — for logs and CI
    mode "overall"   one line for the whole run, all VINs pooled
    mode "detailed"  one line per VIN, live while it downloads then finalized

    Redraws in place with \\r on a TTY. When stdout is redirected (a pipe, a log
    file) \\r would produce one unreadable mega-line, so it falls back to plain
    appended lines: every 10% for "overall", one per completed VIN for
    "detailed".
    """
    REDRAW_INTERVAL = 0.1                      # seconds between TTY repaints

    def __init__(self, mode: str, total_files: int, total_vins: int, stream=None):
        self.mode = mode
        self.total_files = total_files
        self.total_vins = total_vins
        self.out = stream or sys.stdout
        self.tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.started = time.monotonic()
        self.done = self.ok = self.skipped = self.failed = self.nbytes = 0
        self._last_paint = 0.0
        self._last_decile = -1
        self._line_len = 0
        # per-VIN state
        self.vin_idx = 0
        self.vin_label = ""
        self.vin_total = self.vin_done = self.vin_bytes = 0
        self.vin_ok = self.vin_skipped = self.vin_failed = 0
        self.vin_started = 0.0

    # -- painting ----------------------------------------------------------
    def _write(self, text: str, newline: bool = False) -> None:
        if self.tty:
            width = shutil.get_terminal_size((100, 24)).columns - 1
            text = text[:width]
            pad = " " * max(0, self._line_len - len(text))
            self.out.write("\r" + text + pad + ("\n" if newline else ""))
            self._line_len = 0 if newline else len(text)
        else:
            self.out.write(text + "\n")
        self.out.flush()

    def _overall_line(self) -> str:
        pct = 100 * self.done / self.total_files if self.total_files else 100
        elapsed = time.monotonic() - self.started
        rate = self.nbytes / elapsed if elapsed > 0.5 else 0
        remaining = self.total_files - self.done
        eta = remaining * (elapsed / self.done) if self.done else -1
        tail = f"{_fmt_bytes(self.nbytes)}"
        if rate:
            tail += f" {_fmt_bytes(rate)}/s"
        tail += f" eta {_fmt_secs(eta)}"
        return (f"  [{self.done:>4d}/{self.total_files}] {pct:3.0f}% "
                f"{_bar(self.done, self.total_files, 24)} "
                f"vin {self.vin_idx}/{self.total_vins} {tail}"
                + (f" fail:{self.failed}" if self.failed else ""))

    def _vin_line(self, final: bool = False) -> str:
        pct = 100 * self.vin_done / self.vin_total if self.vin_total else 100
        counts = f"ok:{self.vin_ok}"
        if self.vin_skipped:
            counts += f" skip:{self.vin_skipped}"
        if self.vin_failed:
            counts += f" fail:{self.vin_failed}"
        tail = f"{_fmt_bytes(self.vin_bytes):>7s}  {counts}"
        if final:
            tail += f"  {_fmt_secs(time.monotonic() - self.vin_started)}"
        return (f"  [{self.vin_idx:>3d}/{self.total_vins}] {self.vin_label} "
                f"[{self.vin_done:>2d}/{self.vin_total:<2d}] "
                f"{_bar(self.vin_done, self.vin_total, 16)} {pct:3.0f}% {tail}")

    def _repaint(self, force: bool = False) -> None:
        if self.mode == "none":
            return
        now = time.monotonic()
        if self.tty:
            if not force and now - self._last_paint < self.REDRAW_INTERVAL:
                return
            self._last_paint = now
            self._write(self._vin_line() if self.mode == "detailed"
                        else self._overall_line())
        elif self.mode == "overall":
            decile = int(10 * self.done / self.total_files) if self.total_files else 10
            if decile > self._last_decile:
                self._last_decile = decile
                self._write(self._overall_line())

    # -- events ------------------------------------------------------------
    def start_vin(self, rec: dict, n_files: int) -> None:
        self.vin_idx += 1
        self.vin_label = f"{PLATFORM_DIR[rec['platform']]:<6s} {rec['vin']}"
        self.vin_total, self.vin_done, self.vin_bytes = n_files, 0, 0
        self.vin_ok = self.vin_skipped = self.vin_failed = 0
        self.vin_started = time.monotonic()
        if self.mode == "detailed" and self.tty:
            self._repaint(force=True)

    def file_done(self, kind: str, nbytes: int = 0) -> None:
        self.done += 1
        self.vin_done += 1
        self.nbytes += nbytes
        self.vin_bytes += nbytes
        setattr(self, kind, getattr(self, kind) + 1)
        setattr(self, f"vin_{kind}", getattr(self, f"vin_{kind}") + 1)
        self._repaint()

    def end_vin(self) -> None:
        if self.mode == "detailed":
            self._write(self._vin_line(final=True), newline=self.tty)

    def close(self) -> None:
        if self.mode != "overall":
            return
        if not self.tty and self._last_decile >= 10:
            return          # the final decile line already printed 100%
        self._write(self._overall_line(), newline=self.tty)


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------
def _ext(url: str) -> str:
    m = re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"


def plan(records: list, out_root: Path, max_images: int) -> list:
    """[(record, [(url, dest_path), …]), …] — resolved before any HTTP."""
    planned = []
    for rec in records:
        dest = out_root / PLATFORM_DIR[rec["platform"]] / rec["vin"]
        urls = rec["image_urls"][:max_images] if max_images else rec["image_urls"]
        planned.append((rec, [(u, dest / f"{i:02d}{_ext(u)}")
                              for i, u in enumerate(urls, 1)]))
    return planned


def download(planned: list, client: httpx.Client, force: bool = False,
             progress: "Progress | None" = None) -> dict:
    """-> {"ok", "skipped", "failed", "errors"}. Skip-if-exists, like the app;
    `force` overwrites, which is what re-fetching originals over previously
    downloaded resized renders needs (the filenames are identical)."""
    tally = {"ok": 0, "skipped": 0, "failed": 0, "errors": []}
    for rec, files in planned:
        if files:
            files[0][1].parent.mkdir(parents=True, exist_ok=True)
        if progress:
            progress.start_vin(rec, len(files))
        for url, out in files:
            if out.exists() and not force:
                tally["skipped"] += 1
                if progress:
                    progress.file_done("skipped", out.stat().st_size)
                continue
            try:
                resp = client.get(url, timeout=30, follow_redirects=True)
                resp.raise_for_status()
                out.write_bytes(resp.content)
                tally["ok"] += 1
                if progress:
                    progress.file_done("ok", len(resp.content))
            except httpx.HTTPError as e:
                tally["failed"] += 1
                tally["errors"].append(f"{rec['vin']} {out.name}: {e}")
                if progress:
                    progress.file_done("failed")
        if progress:
            progress.end_vin()
    if progress:
        progress.close()
    return tally


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def _magic_ok(suffix: str, head: bytes) -> bool:
    expected = MAGIC.get(suffix.lower())
    if not expected:
        return True
    if suffix.lower() == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return any(head.startswith(sig) for sig in expected)


def _jpeg_dims(data: bytes):
    """(width, height) from a JPEG's SOF marker, or None. Stdlib-only so the
    script keeps its single httpx dependency."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def verify(planned: list, out_root: Path) -> tuple:
    """-> (failures, warnings, total_bytes, dims)."""
    fails, warns, total, dims = [], [], 0, []
    for rec, files in planned:
        if len(rec["vin"]) != 17:
            warns.append(f"{rec['vin']}: VIN is {len(rec['vin'])} chars, expected 17")
        for url, out in files:
            if "vis.iaai.com" in url and "&" in url:
                fails.append(f"resized URL not stripped: {url}")
            if not out.exists():
                fails.append(f"missing: {out.relative_to(out_root)}")
                continue
            size = out.stat().st_size
            total += size
            if size == 0:
                fails.append(f"empty: {out.relative_to(out_root)}")
                continue
            blob = out.read_bytes()
            if not _magic_ok(out.suffix, blob[:12]):
                fails.append(f"not an image: {out.relative_to(out_root)} "
                             f"(starts {blob[:16]!r})")
            elif out.suffix.lower() in (".jpg", ".jpeg"):
                wh = _jpeg_dims(blob)
                if wh:
                    dims.append((wh, rec["platform"], out))
            parts = out.relative_to(out_root).parts
            if len(parts) != 3 or parts[0] not in PLATFORM_DIR.values():
                fails.append(f"bad layout: {'/'.join(parts)}")
    return fails, warns, total, dims


def verify_idempotent(planned: list, client: httpx.Client) -> list:
    """Second pass must touch nothing. Compares mtime_ns across the re-run."""
    before = {out: out.stat().st_mtime_ns
              for _rec, files in planned for _u, out in files if out.exists()}
    tally = download(planned, client)
    fails = []
    if tally["ok"]:
        fails.append(f"second pass re-downloaded {tally['ok']} file(s) — "
                     f"skip-if-exists is not working")
    for out, mtime in before.items():
        if out.exists() and out.stat().st_mtime_ns != mtime:
            fails.append(f"rewritten on second pass: {out.name}")
    return fails


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path,
                    help="probe-dump JSON files (default: test_run/apibara_sold_*.json)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"flat-drop root (default: {DEFAULT_OUT})")
    ap.add_argument("--limit", type=int, default=0, help="cap number of VINs")
    ap.add_argument("--max-images", type=int, default=0, help="cap images per VIN")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and print the plan, make no HTTP requests")
    ap.add_argument("--force", action="store_true",
                    help="re-download files that already exist (needed to replace "
                         "resized IAAI renders fetched before the full-res fix)")
    ap.add_argument("--progress", choices=("overall", "detailed", "none"),
                    default="overall",
                    help="download progress display: one pooled line for the whole "
                         "run (default), one line per VIN, or nothing")
    args = ap.parse_args(argv)

    paths = args.files or sorted(TEST_RUN_DIR.glob("apibara_sold_*.json"))
    if not paths:
        print(f"No probe dumps found in {TEST_RUN_DIR}. Run the scripts in test/ "
              f"first (each spends live Apibara quota), e.g.:\n"
              f"  ./test/run_sold.sh iaai")
        return 1
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        print("Not found: " + ", ".join(str(p) for p in missing))
        return 1

    print("=" * 78)
    print("FLAT PHOTO DROP TEST — images/flat/{IAAI|Copart}/{VIN}/NN.jpg")
    print(f"  out: {args.out}")
    print("=" * 78)

    records, stats = load_from_paths(paths)
    print(f"\n  {'source file':<35s} {'shape':>5s} {'rows':>5s} {'kept':>5s}  note")
    for st in stats:
        print(f"  {st['file']:<35s} {st['shape']:>5s} {st['rows']:>5d} "
              f"{st['kept']:>5d}  {st['note']}")

    deduped = dedup_by_vin(records)
    dropped = len(records) - len(deduped)
    print(f"\n  {len(records)} sold record(s) with photos -> {len(deduped)} unique VIN(s)"
          + (f" ({dropped} duplicate VIN(s) collapsed to latest sold_day)" if dropped else ""))
    if not deduped:
        print("\n  Nothing to download.")
        return 1

    deduped.sort(key=lambda r: (r["platform"], r["vin"]))
    if args.limit:
        deduped = deduped[:args.limit]
        print(f"  --limit {args.limit} -> {len(deduped)} VIN(s)")

    planned = plan(deduped, args.out, args.max_images)
    n_files = sum(len(f) for _r, f in planned)
    print(f"  {n_files} image file(s) planned\n")

    print(f"  {'platform':<8s} {'VIN':<19s} {'year make model':<28s} {'imgs':>4s}  location")
    for rec, files in planned:
        ymm = f"{rec['year'] or '—'} {rec['make']} {rec['model']}".strip()
        print(f"  {PLATFORM_DIR[rec['platform']]:<8s} {rec['vin']:<19s} "
              f"{ymm[:28]:<28s} {len(files):>4d}  {rec['location']}")

    if args.dry_run:
        print(f"\n  --dry-run: no HTTP made. Would write {n_files} file(s) under {args.out}")
        return 0

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        print(f"\n  downloading{' (--force: overwriting existing)' if args.force else ''} …")
        bar = Progress(args.progress, n_files, len(planned))
        tally = download(planned, client, force=args.force, progress=bar)
        print(f"  downloaded {tally['ok']}, already present {tally['skipped']}, "
              f"failed {tally['failed']}")
        for err in tally["errors"][:10]:
            print(f"    ! {err}")
        if len(tally["errors"]) > 10:
            print(f"    ! … and {len(tally['errors']) - 10} more")

        fails, warns, total, dims = verify(planned, args.out)
        idem = [] if fails else verify_idempotent(planned, client)

    print(f"\n  {total / 1e6:.1f} MB on disk across {n_files} planned file(s)"
          f"  ({total / max(len(dims), 1) / 1e3:.0f} KB avg)")
    for plat in sorted({p for _wh, p, _o in dims}):
        px = sorted((wh for wh, p, _o in dims if p == plat), key=lambda d: d[0] * d[1])
        small, large = px[0], px[-1]
        print(f"  {PLATFORM_DIR[plat]:<7s} resolution: {small[0]}x{small[1]} min … "
              f"{large[0]}x{large[1]} max  ({len(px)} JPEG(s))")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails + idem:
        print(f"  FAIL  {f}")

    print("\n" + "=" * 78)
    if fails or idem or tally["failed"]:
        print(f"FAILED — {len(fails) + len(idem)} check failure(s), "
              f"{tally['failed']} download error(s)")
        return 1
    print(f"PASSED — {n_files} file(s) verified under {args.out}")
    print("  (tiered archive intentionally not built; see module docstring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
