"""Decode platform-specific csv-cut image columns into downloader inputs."""


def image_urls(row, width, height):
    """Return ``[(stable numeric key, usable URL), ...]`` for IAAI or Copart."""
    copart = [url.strip() for url in
              (row.get("copart_image_urls") or "").split("|") if url.strip()]
    if copart:
        return [(str(index), url) for index, url in enumerate(copart, 1)]

    prefix = (row.get("iaai_image_url_prefix") or "").strip()
    keys = [key for key in (row.get("iaai_image_keys") or "").split("|")
            if key.strip()]
    if not prefix or not keys:
        return []
    return [(key, f"{prefix}{key}&width={width}&height={height}") for key in keys]
