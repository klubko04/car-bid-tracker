"""P4 — enrich canonical Copart JSON with complete, explicit lot media.

Copart's public search and ``/public/data/lotdetails/solr/{lot}`` contracts expose
only ``tims``, the first thumbnail.  APIBara already supplies complete media for
matched lots.  For web-only lots this stage can read either a signed-in browser
HAR captured from the Copart lot gallery or the server-rendered lot payload
published by AutoBidMaster, a Copart-registered broker.

The browser route is the primary one: open ``View all photos``, load the
complete gallery, export "HAR with content", then:

    python analytics/scripts/copart_image_enrich_01.py ADAPTED.json \
        --har 64982206=/tmp/copart-64982206.har

When present, the parser prefers Copart's structured ``lot-images`` response
over URL discovery from the rendered page.  That response distinguishes normal
photos, 360 panoramas, and engine video without guessing filename sequences.
HAR request headers and cookies are never copied into the output.  Keep the HAR
outside version control because the source file can contain session metadata.

The broker page parser reads:

    window.__REACT_QUERY_STATE__ -> query state.data.lot.images

Every image object contains explicit ``thumbnail``, ``full`` and ``hdr`` URLs.
This script never constructs a CDN URL or changes a filename suffix. It accepts
only HTTPS URLs on Copart's media hosts (``cs`` and ``c-static``) that are
present in the source payload, validates Copart lot number plus
year/make/model/VIN prefix, and replaces media only when the feed is richer. It
does not copy the broker's full VIN or other vehicle facts.

THE BROKER HTTP ROUTE IS RETIRED
--------------------------------
This stage no longer makes network requests. It previously fetched broker lot
pages directly, and that route did not work at cohort scale: the one full run
scored ``enriched: 16`` against ``http_error: 46`` — a 74% failure rate — while
the browser route returned complete galleries for 190/204 (A5) and 31/32 (S4).
A fallback that fails three times out of four is not a fallback; it is a way to
spend an afternoon discovering the browser route was needed anyway.

Media therefore comes from exactly three explicit, operator-supplied places:

    --har LOT=FILE        signed-in Copart gallery capture (the primary route)
    --html LOT=FILE       saved broker page, for offline regression fixtures
    --reuse-from JSON     validated media from a prior adapted archive

A candidate lot with none of the three is counted ``no_capture_supplied`` and
left untouched. Nothing is guessed and nothing is fetched.

Examples:

    # Reuse validated galleries after regenerating the upstream JSON
    python analytics/scripts/copart_image_enrich_01.py ADAPTED.json \
        --reuse-from images_ADAPTED_previous.json --reuse-only

    # Signed-in browser capture (see copart_browser_enrich_01.py for the driver)
    python analytics/scripts/copart_image_enrich_01.py ADAPTED.json \
        --har 64982206=/tmp/copart-64982206.har

    # Offline saved-page regression
    python analytics/scripts/copart_image_enrich_01.py ADAPTED.json \
        --html 64982206=/tmp/autobidmaster_64982206_page.html

The output remains json-adapted and is accepted unchanged by
``apibara_json2csv_copart_01.py`` and ``pull_images_01.py``.
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import datetime as dt
import hashlib
import html as html_lib
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "analytics" / "data"
PLATFORM = "copart"
SOURCE = "autobidmaster-authorized-copart-broker"
BROWSER_SOURCE = "copart-authorized-browser-har"
BROKER_BASE = "https://www.autobidmaster.com"

# NOTE: these seven constants were lost to an editing accident on 2026-08-19 in
# an untracked file and were RECONSTRUCTED from their call sites, then verified
# functionally by replaying real archived galleries through
# media_from_explicit_urls() and diffing against the stored media blocks
# (see test_copart_image_enrich_01.py::ReconstructedConstantTests).
# LOT_PAGE_RE / EXPLICIT_COPART_URL_RE / COPART_MEDIA_HOSTS are byte-exact from
# the pre-accident source; the remaining four are behaviourally equivalent.
LOT_PAGE_RE = re.compile(r"https://(?:www\.)?copart\.com/lot/(\d+)(?:[/?#]|$)", re.I)
EXPLICIT_COPART_URL_RE = re.compile(
    r"https://cs\.copart\.com/[^\s\"'<>\\]+", re.I
)
COPART_MEDIA_HOSTS = {"cs.copart.com", "c-static.copart.com"}

# Copart names assets <hash>_<variant>.<ext>. The variant token is what
# separates a thumbnail from a full-size photo from a video poster, and
# image_asset_key() truncates at the token so every variant of one photo
# collapses to a single asset.
MEDIA_SUFFIX_RE = re.compile(r"_([A-Za-z]+)(?=\.[A-Za-z0-9]+$)")

# Suffixes as returned by Path(...).suffix, i.e. leading dot, lowercased.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm"}

# A decoded 17-character VIN. Copart's public surface masks these; a broker or
# browser feed may carry the full value, which is only ever used to detect an
# identity conflict, never copied into the record.
FULL_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_lot(value):
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def norm_identity(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def visible_vin_prefix(value):
    match = re.match(r"[A-HJ-NPR-Z0-9]+", str(value or "").strip().upper())
    return match.group(0) if match else ""


def broker_lot_url(lot):
    # This is the broker's documented lot route. Only the page URL uses the lot
    # number; image paths are always copied verbatim from its embedded payload.
    query = urllib.parse.urlencode({"fallback": "true"})
    return f"{BROKER_BASE}/en/search/lot/{normalize_lot(lot)}/?{query}"


def parse_embedded_state(html):
    marker = re.search(r"window\.__REACT_QUERY_STATE__\s*=\s*", html or "")
    if not marker:
        raise ValueError("window.__REACT_QUERY_STATE__ was not present")
    try:
        state, _ = json.JSONDecoder().raw_decode(html, marker.end())
    except json.JSONDecodeError as exc:
        raise ValueError(f"embedded React query state was invalid JSON: {exc}") from None
    return state


def lot_payload(html):
    state = parse_embedded_state(html)
    candidates = []
    for query in state.get("queries") or []:
        data = ((query.get("state") or {}).get("data"))
        lot = data.get("lot") if isinstance(data, dict) else None
        if not isinstance(lot, dict):
            continue
        if norm_identity(lot.get("inventoryAuction")) != "copart":
            continue
        candidates.append((query.get("queryKey"), lot))
    if len(candidates) != 1:
        raise ValueError(f"expected one Copart lot payload, found {len(candidates)}")
    return candidates[0]


def https_copart_url(value):
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in COPART_MEDIA_HOSTS:
        return None
    return parsed.geturl()


def copart_lot_media_url(value):
    """Accept an exact Copart lot-media URL, never a constructed variant."""
    url = https_copart_url(value)
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.casefold()
    if "/lpp/" not in path and "-lpp/" not in path:
        return None
    extension = Path(path).suffix
    if extension not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        return None
    return url


def unescape_capture_text(value):
    """Decode URL escaping commonly found in JSON response bodies."""
    text = html_lib.unescape(str(value or ""))
    # HAR content can contain JSON nested inside JSON, so tolerate one or more
    # escape backslashes without decoding unrelated response-body content.
    text = re.sub(r"\\+(?:/|u002[fF])", "/", text)
    text = re.sub(r"\\+u003[aA]", ":", text)
    text = re.sub(r"\\+u002[eE]", ".", text)
    text = re.sub(r"\\+u0026", "&", text)
    return text


def explicit_media_urls(text):
    """Return unique, ordered URLs that were explicitly present in text."""
    output = []
    seen = set()
    for match in EXPLICIT_COPART_URL_RE.finditer(unescape_capture_text(text)):
        candidate = match.group(0).rstrip(",.;)]}")
        url = copart_lot_media_url(candidate)
        if url and url not in seen:
            seen.add(url)
            output.append(url)
    return output


def har_response_text(entry):
    content = ((entry.get("response") or {}).get("content") or {})
    text = content.get("text")
    if not isinstance(text, str):
        return ""
    if str(content.get("encoding") or "").casefold() != "base64":
        return text
    try:
        return base64.b64decode(text, validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


def image_asset_key(url):
    """Group only variants that the browser actually received."""
    parsed = urllib.parse.urlparse(url)
    match = MEDIA_SUFFIX_RE.search(parsed.path)
    path = parsed.path[:match.start()] if match else parsed.path
    return parsed.netloc.casefold(), path


def media_from_explicit_urls(urls):
    images = {}
    videos = []
    for url in urls:
        extension = Path(urllib.parse.urlparse(url).path.casefold()).suffix
        if extension in VIDEO_EXTENSIONS:
            videos.append(url)
            continue
        suffix = MEDIA_SUFFIX_RE.search(urllib.parse.urlparse(url).path)
        variant = suffix.group(1).casefold() if suffix else None
        # Copart's vthb asset is a video poster, not another vehicle photo.
        if variant == "vthb":
            continue
        key = image_asset_key(url)
        item = images.setdefault(key, {
            "type": "image", "thumb": None, "full": None, "large": None,
            "sequence": len(images),
        })
        if variant == "thb":
            item["thumb"] = url
        elif variant in {"ful", "vful"}:
            item["full"] = url
        elif variant in {"hrs", "vhrs"}:
            # vhrs wins only when both explicit variants were captured.
            if variant == "vhrs" or not item["large"]:
                item["large"] = url
        elif not item["full"]:
            item["full"] = url

    thumb_only_count = sum(
        not item["full"] and not item["large"] for item in images.values()
    )
    usable_images = {
        key: item for key, item in images.items()
        if item["full"] or item["large"]
    }
    # Once the browser has exposed high-resolution gallery assets, do not
    # publish unrelated recommendation thumbnails or video posters as photos.
    if usable_images:
        images = usable_images
    items = list(images.values())
    for url in videos:
        items.append({"type": "video", "url": url, "video_type": "captured"})
    return {
        "thumbs_count": len(images),
        "has_video": bool(videos),
        "has_360": False,
        "thumbs": [
            item["thumb"] or item["full"] or item["large"]
            for item in images.values()
        ],
        "items": items,
        "_capture_thumb_only_count": thumb_only_count,
    }


def first_valid_media_url(*values):
    """Return the first exact first-party lot-media URL in field order."""
    for value in values:
        url = copart_lot_media_url(value)
        if url:
            return url
    return None


def media_from_lot_images_response(body):
    """Parse Copart's first-party structured gallery response.

    Normal photos remain ``media.items[type=image]`` so the current CSV and
    downloader use their explicit high-resolution URLs unchanged. Panoramas
    are retained separately because a 360 frame-set is not one ordinary photo.
    """
    payload = json.loads(body)
    image_lists = (((payload.get("data") or {}).get("imagesList"))
                   if isinstance(payload, dict) else None)
    if not isinstance(image_lists, dict):
        raise ValueError("lot-images response did not contain data.imagesList")

    observed_lots = set()

    def note_lot(item):
        lot = normalize_lot(item.get("lotNumberStr") or item.get("ln"))
        if lot:
            observed_lots.add(lot)

    normal_images = []
    for item in image_lists.get("IMAGE") or []:
        if not isinstance(item, dict):
            continue
        note_lot(item)
        thumb = first_valid_media_url(item.get("thumbnailUrl"))
        full = first_valid_media_url(item.get("fullUrl"), item.get("solrFullUrl"))
        large = first_valid_media_url(
            item.get("highResUrl"), item.get("solrHighResUrl")
        )
        if not any((thumb, full, large)):
            continue
        normal_images.append({
            "type": "image", "thumb": thumb, "full": full, "large": large,
            "sequence": item.get("imageSeqNumber"),
            "label": item.get("imageLabelCode"),
            "description": item.get("imageTypeDescription"),
        })
    normal_images.sort(key=lambda item: (
        item.get("sequence") is None, item.get("sequence") or 0
    ))

    panoramas = []
    for image_type in ("EXTERIOR_360", "INTERIOR_360"):
        for item in image_lists.get(image_type) or []:
            if not isinstance(item, dict):
                continue
            note_lot(item)
            panorama = {
                "type": image_type.casefold(),
                "thumb": first_valid_media_url(item.get("thumbnailUrl")),
                "full": first_valid_media_url(
                    item.get("fullUrl"), item.get("solrFullUrl")
                ),
                "large": first_valid_media_url(
                    item.get("highResUrl"), item.get("solrHighResUrl")
                ),
                "frame_url": first_valid_media_url(item.get("image360Url")),
                "frame_count": item.get("frameCount"),
                "sequence": item.get("imageSeqNumber"),
            }
            if any(value for key, value in panorama.items()
                   if key not in {"type", "sequence", "frame_count"}):
                panoramas.append(panorama)

    videos = []
    for image_type, candidates in image_lists.items():
        if "VIDEO" not in str(image_type).upper():
            continue
        for item in candidates or []:
            if not isinstance(item, dict):
                continue
            note_lot(item)
            url = first_valid_media_url(
                item.get("highResUrl"), item.get("fullUrl"),
                item.get("solrHighResUrl"), item.get("solrFullUrl"),
            )
            extension = Path(urllib.parse.urlparse(url or "").path.casefold()).suffix
            if not url or extension not in VIDEO_EXTENSIONS:
                continue
            videos.append({
                "type": "video", "url": url,
                "video_type": str(image_type).casefold(),
                "sequence": item.get("imageSeqNumber"),
            })

    media = {
        "thumbs_count": len(normal_images),
        "has_video": bool(videos),
        "has_360": bool(panoramas),
        "thumbs": [
            item["thumb"] or item["full"] or item["large"]
            for item in normal_images
        ],
        "items": normal_images + videos,
    }
    if panoramas:
        media["panoramas"] = panoramas
    return media, sorted(observed_lots)


def parse_browser_har(path, record):
    """Extract explicit Copart gallery URLs from a browser HAR with content."""
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = ((document.get("log") or {}).get("entries") or [])
    if not isinstance(entries, list):
        raise ValueError("HAR log.entries was not a list")

    requested_lot = normalize_lot(record.get("lot_number"))
    document_lots = []
    urls = []
    seen = set()
    response_bodies_with_media = 0
    structured_media = []
    structured_lots = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request_url = str((entry.get("request") or {}).get("url") or "")
        resource_type = str(entry.get("_resourceType") or "").casefold()
        mime = str(((entry.get("response") or {}).get("content") or {}).get(
            "mimeType") or "").casefold()
        lot_match = LOT_PAGE_RE.match(request_url)
        if lot_match and (resource_type == "document" or "text/html" in mime):
            document_lots.append(lot_match.group(1))

        response_body = har_response_text(entry)
        if "/public/data/lotdetails/solr/lot-images/" in request_url and response_body:
            try:
                contract_media, contract_lots = media_from_lot_images_response(
                    response_body
                )
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                structured_media.append(contract_media)
                structured_lots.update(contract_lots)

        body_urls = explicit_media_urls(response_body)
        if body_urls:
            response_bodies_with_media += 1
        for value in explicit_media_urls(request_url) + body_urls:
            if value not in seen:
                seen.add(value)
                urls.append(value)

    unique_document_lots = sorted(set(document_lots))
    conflicts = []
    if not unique_document_lots:
        conflicts.append({
            "field": "lot_number", "record": requested_lot,
            "feed": None, "reason": "no Copart lot document request in HAR",
        })
    elif requested_lot not in unique_document_lots:
        conflicts.append({
            "field": "lot_number", "record": requested_lot,
            "feed": unique_document_lots,
        })
    if structured_lots and requested_lot not in structured_lots:
        conflicts.append({
            "field": "lot_number", "record": requested_lot,
            "feed": sorted(structured_lots),
            "reason": "lot-images response belonged to another lot",
        })

    fallback_media = media_from_explicit_urls(urls)
    thumb_only_count = fallback_media.pop("_capture_thumb_only_count", 0)
    media = (max(structured_media, key=lambda value: value["thumbs_count"])
             if structured_media else fallback_media)
    return {
        "source": BROWSER_SOURCE,
        "page_url": f"https://www.copart.com/lot/{requested_lot}",
        "lot_number": requested_lot,
        "identity_conflicts": conflicts,
        "image_count": media["thumbs_count"],
        "explicit_url_count": len(urls),
        "thumb_only_image_count": thumb_only_count,
        "har_entry_count": len(entries),
        "response_bodies_with_media": response_bodies_with_media,
        "capture_scope": "HAR request URLs plus response content",
        "capture_completeness": (
            "first_party_lot_images_response" if structured_media
            else "operator-dependent_unverified"
        ),
        "structured_gallery_response_count": len(structured_media),
        "media": media,
    }


def identity_conflicts(record, lot):
    conflicts = []
    for field in ("year", "make", "model"):
        left, right = record.get(field), lot.get(field)
        if left in (None, "") or right in (None, ""):
            continue
        matches = int(left) == int(right) if field == "year" else (
            norm_identity(left) == norm_identity(right)
        )
        if not matches:
            conflicts.append({"field": field, "record": left, "feed": right})
    prefix = visible_vin_prefix(record.get("vin"))
    full = str(lot.get("vin") or "").strip().upper()
    if prefix and FULL_VIN_RE.fullmatch(full) and not full.startswith(prefix):
        conflicts.append({"field": "vin_prefix", "record": prefix, "feed": full})
    return conflicts


def media_from_lot(lot):
    items = []
    thumbs = []
    rejected = []
    images = sorted(
        (item for item in lot.get("images") or [] if isinstance(item, dict)),
        key=lambda item: (item.get("sequence") is None, item.get("sequence") or 0),
    )
    for image in images:
        thumb = https_copart_url(image.get("thumbnail"))
        full = https_copart_url(image.get("full"))
        large = https_copart_url(image.get("hdr"))
        if not any((thumb, full, large)):
            rejected.append(image)
            continue
        if thumb:
            thumbs.append(thumb)
        items.append({
            "type": "image",
            "thumb": thumb,
            "full": full,
            "large": large,
            "sequence": image.get("sequence"),
            "label": image.get("label"),
            "damage_description": image.get("damageDescription"),
        })

    video_keys = ("engineVideo", "walkThroughVideo", "externalVideo", "goLotVideo")
    for key in video_keys:
        video = lot.get(key)
        if not isinstance(video, dict):
            continue
        url = https_copart_url(video.get("url"))
        if not url:
            continue
        items.append({
            "type": "video", "url": url, "video_type": video.get("type") or key,
            "thumb": https_copart_url(video.get("thumbnail")),
            "full": https_copart_url(video.get("full")),
        })
    return {
        "thumbs_count": len([item for item in items if item["type"] == "image"]),
        "has_video": any(item["type"] == "video" for item in items),
        "has_360": bool(
            lot.get("internalPanoramas") or lot.get("externalPanoramas") or
            lot.get("spincarPanoramas")
        ),
        "thumbs": thumbs,
        "items": items,
    }, rejected


def parse_feed(html, record, page_url=None):
    query_key, lot = lot_payload(html)
    requested_lot = normalize_lot(record.get("lot_number"))
    actual_lot = normalize_lot(lot.get("lotNumber") or lot.get("id"))
    conflicts = []
    if actual_lot != requested_lot:
        conflicts.append({"field": "lot_number", "record": requested_lot,
                          "feed": actual_lot})
    conflicts.extend(identity_conflicts(record, lot))
    media, rejected = media_from_lot(lot)
    return {
        "source": SOURCE,
        "page_url": page_url,
        "query_key": query_key,
        "lot_number": actual_lot,
        "source_marker": lot.get("source"),
        "identity_conflicts": conflicts,
        "image_count": media["thumbs_count"],
        "rejected_media_count": len(rejected),
        "media": media,
    }


# The broker HTTP transport used to live here. It is intentionally gone: see
# "THE BROKER HTTP ROUTE IS RETIRED" in the module docstring. Do not reintroduce
# a fetcher in this stage — captures are supplied by the operator.


def records(document):
    for page in document.get("pages") or []:
        if page.get("status") != 200:
            continue
        for record in ((page.get("raw") or {}).get("data") or []):
            if isinstance(record, dict):
                yield record


def image_count(record):
    return sum(
        isinstance(item, dict) and item.get("type") == "image"
        for item in ((record.get("media") or {}).get("items") or [])
    )


def gallery_is_complete(record):
    """True when Copart's structured gallery contract was captured for the lot.

    A complete gallery can legitimately contain one image.  Image count alone
    therefore cannot distinguish a thin search thumbnail from a verified
    one-photo lot.
    """
    provenance = ((record.get("enrichment") or {}).get(
        "copart_authorized_image_feed"
    ) or {})
    return provenance.get("capture_completeness") == "first_party_lot_images_response"


def needs_gallery_capture(record):
    return image_count(record) <= 1 and not gallery_is_complete(record)


def lot_numbers_from_csv(path):
    """Ordered, unique Copart lot allowlist from a csv-cut artifact."""
    source = Path(path).expanduser()
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "lot_number" not in (reader.fieldnames or []):
            raise ValueError(f"{source}: missing lot_number column")
        output = []
        seen = set()
        for row in reader:
            lot = normalize_lot(row.get("lot_number"))
            if lot and lot not in seen:
                output.append(lot)
                seen.add(lot)
    if not output:
        raise ValueError(f"{source}: no lot numbers")
    return output


def reuse_media(document, paths, allowed_lots=None):
    """Reuse a prior explicit-media result after upstream JSON regeneration."""
    available = {}
    for path in paths:
        prior = json.loads(Path(path).read_text(encoding="utf-8"))
        for record in records(prior):
            lot = normalize_lot(record.get("lot_number"))
            rank = (gallery_is_complete(record), image_count(record))
            current_rank = (gallery_is_complete(available.get(lot, {})),
                            image_count(available.get(lot, {})))
            if lot and rank > current_rank:
                available[lot] = record
    reused = 0
    for record in records(document):
        lot = normalize_lot(record.get("lot_number"))
        if allowed_lots is not None and lot not in allowed_lots:
            continue
        prior = available.get(lot)
        prior_rank = (gallery_is_complete(prior or {}), image_count(prior or {}))
        current_rank = (gallery_is_complete(record), image_count(record))
        if not prior or prior_rank <= current_rank:
            continue
        if identity_conflicts(record, prior):
            continue
        record["media"] = copy.deepcopy(prior["media"])
        provenance = ((prior.get("enrichment") or {}).get(
            "copart_authorized_image_feed"
        ))
        if provenance:
            record.setdefault("enrichment", {})[
                "copart_authorized_image_feed"
            ] = copy.deepcopy(provenance)
        reused += 1
    return reused


def parse_lot_file_args(values, option):
    output = {}
    for value in values or []:
        lot, separator, filename = value.partition("=")
        if not separator or not normalize_lot(lot) or not filename:
            raise ValueError(f"{option} wants LOT=FILE")
        path = Path(filename).expanduser()
        if not path.is_file():
            raise ValueError(f"{option} file not found: {path}")
        output[normalize_lot(lot)] = path
    return output


def parse_html_args(values):
    return parse_lot_file_args(values, "--html")


def output_path(source, document, explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else source.parent / path
    return source.parent / f"images_{source.name}"


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Enrich canonical Copart JSON from explicit authorized media captures."
    )
    parser.add_argument("file", help="canonical Copart json-adapted archive")
    parser.add_argument("--html", action="append", default=[], metavar="LOT=FILE",
                        help="saved broker page for that lot (offline regression fixture)")
    parser.add_argument("--har", action="append", default=[], metavar="LOT=FILE",
                        help="use a signed-in Copart gallery HAR with content")
    parser.add_argument("--reuse-from", action="append", default=[], metavar="JSON",
                        help="reuse richer validated media from a prior adapted archive")
    parser.add_argument("--reuse-only", action="store_true",
                        help="apply --reuse-from only; ignore --har/--html captures")
    parser.add_argument("--lots-from-csv", metavar="CSV",
                        help="only enrich lots selected by this csv-cut")
    parser.add_argument("--max-lots", type=int, default=0,
                        help="limit candidate lots processed (0 = all)")
    parser.add_argument("--force", action="store_true",
                        help="also inspect lots already carrying multiple images")
    parser.add_argument("--out", help="output JSON (default: images_INPUT.json)")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    source = Path(args.file).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        saved_pages = parse_html_args(args.html)
        browser_hars = parse_lot_file_args(args.har, "--har")
        allowed_order = (lot_numbers_from_csv(args.lots_from_csv)
                         if args.lots_from_csv else None)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if str(document.get("platform") or "").casefold() != PLATFORM:
        raise SystemExit(f"{source.name}: expected platform='copart'")

    output = copy.deepcopy(document)
    allowed_lots = set(allowed_order) if allowed_order is not None else None
    try:
        reused = reuse_media(output, args.reuse_from, allowed_lots=allowed_lots)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    candidates = [record for record in records(output)
                  if (allowed_lots is None or
                      normalize_lot(record.get("lot_number")) in allowed_lots) and
                  (args.force or needs_gallery_capture(record))]
    offline_lots = set(saved_pages) | set(browser_hars)
    if offline_lots:
        candidates = [record for record in candidates
                      if normalize_lot(record.get("lot_number")) in offline_lots]
    if args.max_lots:
        candidates = candidates[:args.max_lots]
    if args.reuse_only:
        candidates = []

    audit = []
    counts = Counter({"reused": reused}) if reused else Counter()
    for index, record in enumerate(candidates):
        lot = normalize_lot(record.get("lot_number"))
        page_url = broker_lot_url(lot)
        saved = saved_pages.get(lot)
        har = browser_hars.get(lot)
        feed = None
        if har:
            page_url = f"https://www.copart.com/lot/{lot}"
            raw = har.read_bytes()
            status, source_kind = 200, "browser_har"
            response_hash = hashlib.sha256(raw).hexdigest()
            try:
                feed = parse_browser_har(har, record)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                status, raw = 0, str(exc).encode("utf-8", "replace")
                response_hash = hashlib.sha256(raw).hexdigest()
        elif saved:
            html = saved.read_text(encoding="utf-8")
            status, source_kind = 200, "saved_html"
            response_hash = hashlib.sha256(
                html.encode("utf-8", "replace")
            ).hexdigest()
        else:
            # No capture for this lot and no fetcher any more. Record it as an
            # explicit gap so the operator knows exactly which lots still need
            # a browser capture, rather than silently emitting a thumbnail-only
            # gallery that looks enriched.
            counts["no_capture_supplied"] += 1
            audit.append({
                "lot_number": lot, "page_url": page_url,
                "status": "no_capture_supplied",
                "hint": "supply --har LOT=FILE (browser) or --reuse-from JSON",
            })
            continue
        entry = {
            "lot_number": lot, "page_url": page_url, "http_status": status,
            "source_kind": source_kind,
            "response_sha256": response_hash,
        }
        if status != 200:
            error = raw.decode("utf-8", "replace") if har else html
            error_kind = "capture_error" if har else "http_error"
            entry.update(status=error_kind, error=error[:300])
            counts[error_kind] += 1
            audit.append(entry)
            continue
        if feed is None:
            try:
                feed = parse_feed(html, record, page_url=page_url)
            except ValueError as exc:
                entry.update(status="parse_error", error=str(exc))
                counts["parse_error"] += 1
                audit.append(entry)
                continue
        if feed["identity_conflicts"]:
            entry.update(status="identity_conflict",
                         identity_conflicts=feed["identity_conflicts"])
            counts["identity_conflict"] += 1
            audit.append(entry)
            continue
        prior = image_count(record)
        verified_equal = (
            feed["image_count"] == prior and
            feed.get("capture_completeness") == "first_party_lot_images_response"
        )
        if feed["image_count"] < prior or (
            feed["image_count"] == prior and not verified_equal
        ):
            entry.update(status="not_richer", prior_image_count=prior,
                         feed_image_count=feed["image_count"])
            counts["not_richer"] += 1
            audit.append(entry)
            continue
        record["media"] = feed.pop("media")
        record.setdefault("enrichment", {})["copart_authorized_image_feed"] = {
            **feed, "retrieved_at": now_iso(),
            "response_sha256": entry["response_sha256"],
        }
        status = "verified_refresh" if verified_equal else "enriched"
        entry.update(status=status, prior_image_count=prior,
                     feed_image_count=record["media"]["thumbs_count"])
        counts[status] += 1
        audit.append(entry)

    output["adapted_at"] = now_iso()
    # Derive the file-level label from the provenance the records actually
    # carry. The previous rule appended the broker source whenever no HAR was
    # supplied, so a pure --reuse-from run advertised media as broker-sourced
    # when nothing had been fetched from the broker at all.
    capture_sources = []
    for record in records(output):
        feed = (record.get("enrichment") or {}).get("copart_authorized_image_feed")
        origin = (feed or {}).get("source")
        if origin and origin not in capture_sources:
            capture_sources.append(origin)
    capture_sources.sort()
    output["image_enrichment"] = {
        "stage": "copart_image_enrich_01",
        "source": (capture_sources[0] if len(capture_sources) == 1
                   else ("mixed" if capture_sources else None)),
        "sources": capture_sources,
        "network": "retired_no_http_requests",
        "policy": "explicit_urls_only_media_only_identity_validated",
        "input": source.name, "reused_from": list(args.reuse_from),
        "lot_allowlist_csv": args.lots_from_csv,
        "lot_allowlist_count": len(allowed_lots) if allowed_lots is not None else None,
        "browser_hars": [path.name for path in browser_hars.values()],
        "candidate_count": len(candidates),
        "counts": dict(counts), "audit": audit,
    }
    destination = output_path(source, output, args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    print(f"Copart complete-image enrichment: {len(candidates)} candidate(s)")
    print(f"  counts: {dict(counts)}")
    print(f"  JSON -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
