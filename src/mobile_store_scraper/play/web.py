"""Parser for the public Google Play listing (``play.google.com/store/apps/details``).

The page embeds its data as ``AF_initDataCallback({key: 'ds:N', ..., data: [...]})`` blocks;
``ds:5`` holds the app details. Positions follow google-play-scraper's element map and were
verified against live pages in Oct 2026.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Optional

from ..dates import from_timestamp, to_iso
from ..errors import ParseError

_DS_RE = re.compile(r"AF_initDataCallback\(\{\s*key:\s*'(ds:\d+)'")
_VERSION_RE = re.compile(r"\d")
_TAG_RE = re.compile(r"<[^>]+>")


def extract_datasets(html: str, keys: Optional[set[str]] = None) -> dict[str, Any]:
    """Return ``{"ds:5": <decoded json>, ...}`` for every AF_initDataCallback block."""
    out: dict[str, Any] = {}
    dec = json.JSONDecoder()
    for m in _DS_RE.finditer(html):
        key = m.group(1)
        if keys and key not in keys:
            continue
        i = html.find("data:", m.end())
        if i < 0:
            continue
        try:
            out[key], _ = dec.raw_decode(html, i + 5)
        except ValueError:
            continue
    return out


def g(obj: Any, *path: Any) -> Any:
    """Safe nested indexing: ``g(data, 1, 2, 140, 0)`` -> value or ``None``."""
    for p in path:
        try:
            obj = obj[p]
        except (IndexError, KeyError, TypeError):
            return None
        if obj is None:
            return None
    return obj


def html_to_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = re.sub(r"<br\s*/?>", "\n", s)
    return _html.unescape(_TAG_RE.sub("", s))


def _categories(d: list) -> list[dict[str, Optional[str]]]:
    out: list[dict[str, Optional[str]]] = []

    def walk(s: Any) -> None:
        if not isinstance(s, list) or not s:
            return
        if len(s) >= 4 and isinstance(s[0], str):
            out.append({"name": s[0], "id": s[2]})
            return
        for sub in s:
            walk(sub)

    walk(g(d, 118))
    if not out and g(d, 79, 0, 0, 0):
        out.append({"name": g(d, 79, 0, 0, 0), "id": g(d, 79, 0, 0, 2)})
    return out


def _is_review(o: Any) -> bool:
    # [review_id, [user_name, avatar...], stars(1-5), None, text, [timestamp, nanos], ...]
    return (isinstance(o, list) and len(o) > 5 and isinstance(o[0], str) and isinstance(o[1], list)
            and isinstance(o[2], int) and 1 <= o[2] <= 5 and isinstance(o[4], str))


def _find_comments(ds: dict[str, Any], limit: int = 50) -> list[str]:
    """Review texts shown on the listing. Google moves them between ``ds:N`` blocks, so search by shape."""
    found: list[str] = []

    def walk(o: Any, depth: int) -> None:
        if len(found) >= limit or depth > 6 or not isinstance(o, list):
            return
        if _is_review(o):
            found.append(o[4])
            return
        for v in o:
            walk(v, depth + 1)

    for key, value in ds.items():
        if key != "ds:5":
            walk(value, 0)
    return found


def _min_os(d: list) -> tuple[Optional[str], Optional[int]]:
    """``[140][1][1][0][0] = [24, "7.0"]`` when the listing has a single minimum."""
    pair = g(d, 140, 1, 1, 0, 0)
    if isinstance(pair, list) and len(pair) >= 2 and isinstance(pair[1], str):
        return f"Android {pair[1]}", pair[0] if isinstance(pair[0], int) else None
    return None, None


def parse_datasets(ds: dict[str, Any]) -> dict[str, Any]:
    """All listing fields, already named like the ``GooglePlayApp`` attributes."""
    d = g(ds.get("ds:5"), 1, 2)
    if not isinstance(d, list):
        raise ParseError("ds:5 has no app block at [1][2]")
    version = g(d, 140, 0, 0, 0)
    if isinstance(version, str) and not _VERSION_RE.search(version):
        version = None  # a localised "Varies with device" label
    if not isinstance(version, str):
        version = None
    min_os, min_sdk = _min_os(d)
    price_micros = g(d, 57, 0, 0, 0, 0, 1, 0, 0)
    price = (price_micros / 1e6) if isinstance(price_micros, (int, float)) else None
    desc_html = g(d, 12, 0, 0, 1) or g(d, 72, 0, 1)
    hist = g(d, 51, 1)
    histogram = [g(hist, i, 1) or 0 for i in range(1, 6)] if isinstance(hist, list) else []
    dev_link = g(d, 68, 1, 4, 2)
    ads = g(d, 48)
    sale_ds = ds.get("ds:4")
    comments = _find_comments(ds)
    original_micros = g(ds.get("ds:3"), 0, 2, 0, 0, 0, 1, 1, 0)
    updated = from_timestamp(g(d, 145, 0, 1, 0))
    released = from_timestamp(g(d, 10, 1, 0))
    return {
        "title": g(d, 0, 0),
        "version": version,
        "varies_with_device": version is None,
        "last_updated": to_iso(updated),
        "last_updated_on": g(d, 145, 0, 0),
        "released": to_iso(released),
        "min_os_ver": min_os,
        "min_sdk": min_sdk,
        "publisher": g(d, 68, 0),
        "developer": g(d, 68, 0),
        "developer_id": dev_link.split("id=", 1)[1] if isinstance(dev_link, str) and "id=" in dev_link else None,
        "developer_email": g(d, 69, 1, 0),
        "developer_website": g(d, 69, 0, 5, 2),
        "developer_address": html_to_text(g(d, 69, 2, 0)),
        "privacy_policy": g(d, 99, 0, 5, 2),
        "description": html_to_text(desc_html),
        "description_html": desc_html,
        "summary": html_to_text(g(d, 73, 0, 1)),
        "release_notes": html_to_text(g(d, 144, 1, 1)),
        "installs": g(d, 13, 0),
        "min_installs": g(d, 13, 1),
        "real_installs": g(d, 13, 2),
        "score": g(d, 51, 0, 1),
        "ratings": g(d, 51, 2, 1),
        "reviews": g(d, 51, 3, 1),
        "histogram": histogram,
        "price": price if price is not None else None,
        "free": (price_micros == 0) if isinstance(price_micros, (int, float)) else None,
        "currency": g(d, 57, 0, 0, 0, 0, 1, 0, 1),
        "sale": bool(g(sale_ds, 0, 2, 0, 0, 0, 14, 0, 0)),
        "sale_time": g(sale_ds, 0, 2, 0, 0, 0, 14, 0, 0),
        "original_price": (original_micros / 1e6) if isinstance(original_micros, (int, float)) else None,
        "sale_text": g(sale_ds, 0, 2, 0, 0, 0, 14, 1),
        "offers_iap": bool(g(d, 19, 0)),
        "in_app_product_price": g(d, 19, 0),
        "genre": g(d, 79, 0, 0, 0),
        "genre_id": g(d, 79, 0, 0, 2),
        "categories": _categories(d),
        "icon": g(d, 95, 0, 3, 2),
        "header_image": g(d, 96, 0, 3, 2),
        "screenshots": [s for s in (g(i, 3, 2) for i in (g(d, 78, 0) or [])) if s],
        "video": g(d, 100, 0, 0, 3, 2),
        "video_image": g(d, 100, 1, 0, 3, 2),
        "content_rating": g(d, 9, 0),
        "content_rating_description": g(d, 9, 2, 1),
        "ad_supported": bool(ads),
        "contains_ads": bool(ads),
        "comments": comments,
        "app_id": g(d, 77, 0),
    }


def parse_details_html(html: str) -> dict[str, Any]:
    ds = extract_datasets(html)
    if "ds:5" not in ds:
        raise ParseError("Play listing did not contain the ds:5 data block")
    return parse_datasets(ds)


# backwards-compatible name
def parse_ds5(ds5: Any) -> dict[str, Any]:
    return parse_datasets({"ds:5": ds5})
