"""Result objects. Every value is a plain attribute: ``app.version``, ``app.last_updated``, ...

Dates are ISO-8601 UTC strings (``"2026-10-14T16:30:38Z"``); the matching ``datetime`` objects
are available as ``*_dt`` properties. Fields a store does not expose are simply not defined on
that store's class (e.g. the App Store has no ``downloads``); fields a store *can* expose but
didn't for this app are ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union

from .dates import from_iso, to_iso


class Store(str, Enum):
    GOOGLE_PLAY = "google_play"
    APP_STORE = "app_store"


class VersionSource(str, Enum):
    PLAY_WEB = "play_web"            # version printed on the public Play listing
    PLAY_API = "play_api"            # Android Play API, resolved for a concrete device profile
    ITUNES_LOOKUP = "itunes_lookup"  # itunes.apple.com/lookup
    APP_STORE_WEB = "app_store_web"  # apps.apple.com listing (fallback when lookup lags)


@dataclass
class DeviceVersion:
    """The build Google Play serves to one specific device profile."""
    device: str
    version: Optional[str]
    version_code: Optional[int]
    sdk: Optional[int] = None
    abis: Optional[str] = None
    error: Optional[str] = None


@dataclass(repr=False, kw_only=True)
class AppData:
    """Fields both stores provide."""

    store: str
    app_id: str
    country: str
    language: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    version: Optional[str] = None
    version_source: Optional[str] = None
    last_updated: Optional[str] = None
    released: Optional[str] = None
    min_os_ver: Optional[str] = None
    publisher: Optional[str] = None
    developer_id: Optional[str] = None
    developer_website: Optional[str] = None
    description: Optional[str] = None
    release_notes: Optional[str] = None
    genre: Optional[str] = None
    genre_id: Optional[str] = None
    icon: Optional[str] = None
    screenshots: list[str] = field(default_factory=list)
    score: Optional[float] = None
    ratings: Optional[int] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    free: Optional[bool] = None
    content_rating: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    # ------------------------------------------------------------------ datetime views
    @property
    def last_updated_dt(self) -> Optional[datetime]:
        return from_iso(self.last_updated)

    @property
    def released_dt(self) -> Optional[datetime]:
        return from_iso(self.released)

    @property
    def last_updated_timestamp(self) -> Optional[int]:
        dt = self.last_updated_dt
        return int(dt.timestamp()) if dt else None

    # ------------------------------------------------------------------ helpers
    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "raw" and not include_raw:
                continue
            v = getattr(self, f.name)
            if isinstance(v, list):
                v = [x.__dict__.copy() if isinstance(x, DeviceVersion) else x for x in v]
            out[f.name] = v
        return out

    def __getitem__(self, key: str) -> Any:  # app["version"] works too
        return getattr(self, key)

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(app_id={self.app_id!r}, country={self.country!r}, title={self.title!r}, "
                f"version={self.version!r}, last_updated={self.last_updated!r}, publisher={self.publisher!r})")


@dataclass(repr=False, kw_only=True)
class GooglePlayApp(AppData):
    """Google Play result. Includes every key google-play-scraper exposes (snake_case)."""

    store: str = Store.GOOGLE_PLAY.value
    # versions -------------------------------------------------------------------------
    version_code: Optional[int] = None
    varies_with_device: bool = False
    device_versions: list[DeviceVersion] = field(default_factory=list)
    version_last_updated: Optional[str] = None   # upload date of exactly this build (Play API)
    # compatibility --------------------------------------------------------------------
    min_sdk: Optional[int] = None
    target_sdk: Optional[int] = None
    download_size: Optional[int] = None           # bytes, for the resolved device (Play API)
    # reach ----------------------------------------------------------------------------
    downloads: Union[int, str, None] = None       # exact count if known, else "50000000+"
    installs: Optional[str] = None                # as displayed, e.g. "50,000,000+"
    min_installs: Optional[int] = None
    real_installs: Optional[int] = None
    reviews: Optional[int] = None
    histogram: list[int] = field(default_factory=list)   # 1..5 stars
    # store page -----------------------------------------------------------------------
    summary: Optional[str] = None
    description_html: Optional[str] = None
    header_image: Optional[str] = None
    video: Optional[str] = None
    video_image: Optional[str] = None
    categories: list[dict[str, Optional[str]]] = field(default_factory=list)
    content_rating_description: Optional[str] = None
    ad_supported: Optional[bool] = None
    contains_ads: Optional[bool] = None
    offers_iap: Optional[bool] = None
    in_app_product_price: Optional[str] = None
    sale: Optional[bool] = None
    sale_time: Optional[int] = None
    original_price: Optional[float] = None
    sale_text: Optional[str] = None
    comments: list[str] = field(default_factory=list)
    # developer ------------------------------------------------------------------------
    developer: Optional[str] = None               # same as publisher (google-play-scraper name)
    developer_email: Optional[str] = None
    developer_address: Optional[str] = None
    privacy_policy: Optional[str] = None
    offered_by: Optional[str] = None
    last_updated_on: Optional[str] = None         # the store's own localized text, e.g. "14.10.2026"

    @property
    def version_last_updated_dt(self) -> Optional[datetime]:
        return from_iso(self.version_last_updated)


@dataclass(repr=False, kw_only=True)
class AppStoreApp(AppData):
    """Apple App Store result (iTunes lookup fields, snake_case)."""

    store: str = Store.APP_STORE.value
    track_id: Optional[int] = None
    bundle_id: Optional[str] = None
    seller: Optional[str] = None
    seller_url: Optional[str] = None
    developer_url: Optional[str] = None
    file_size_bytes: Optional[int] = None
    supported_devices: list[str] = field(default_factory=list)
    ipad_screenshots: list[str] = field(default_factory=list)
    appletv_screenshots: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    genre_ids: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    content_advisory_rating: Optional[str] = None
    score_current_version: Optional[float] = None
    ratings_current_version: Optional[int] = None
    formatted_price: Optional[str] = None
    is_game_center_enabled: Optional[bool] = None
    kind: Optional[str] = None


def iso(dt: Optional[datetime]) -> Optional[str]:
    return to_iso(dt)
