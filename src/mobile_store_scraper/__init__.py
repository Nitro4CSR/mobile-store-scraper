"""mobile-store-scraper - async Google Play + Apple App Store scraper with reliable versions.

>>> import mobile_store_scraper as mss
>>> app = mss.scrape(store="gp", app="com.naturalmotion.customstreetracer2", country="DE")
>>> app.version, app.last_updated, app.min_os_ver, app.downloads, app.released, app.publisher
('6.8.0', '2026-10-14T07:10:38Z', 'Android 7.0', 82008679, '2016-06-29T15:01:05Z', 'Zynga')
"""
from .appstore import AppStore
from .countries import ALL_COUNTRIES, APP_STORE_COUNTRIES, PLAY_COUNTRIES
from .dates import to_iso
from .errors import (AppNotFound, InvalidCountry, IpBlocked, ParseError, PlayApiError, PlayAuthError, RateLimited,
                     StoreScraperError)
from .http import HttpClient
from .models import AppData, AppStoreApp, DeviceVersion, GooglePlayApp, Store, VersionSource
from .play import (DEFAULT_DEVICE, REPRESENTATIVE_DEVICES, GooglePlay, PlayApiClient, PlaySession,
                   available_devices)
from .scrape import (Scraper, resolve_store, scrape, scrape_async, scrape_countries, scrape_countries_async,
                     scrape_many, scrape_many_async)

__version__ = "0.1.0"

__all__ = [
    "scrape", "scrape_async", "scrape_countries", "scrape_countries_async", "scrape_many", "scrape_many_async",
    "Scraper", "resolve_store",
    "GooglePlay", "AppStore", "PlayApiClient", "PlaySession", "HttpClient",
    "AppData", "GooglePlayApp", "AppStoreApp", "DeviceVersion", "Store", "VersionSource",
    "PLAY_COUNTRIES", "APP_STORE_COUNTRIES", "ALL_COUNTRIES",
    "DEFAULT_DEVICE", "REPRESENTATIVE_DEVICES", "available_devices", "to_iso",
    "StoreScraperError", "AppNotFound", "IpBlocked", "InvalidCountry", "ParseError", "RateLimited",
    "PlayAuthError", "PlayApiError", "__version__",
]
