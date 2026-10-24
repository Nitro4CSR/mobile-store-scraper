"""Exception hierarchy for mobile_store_scraper."""
from __future__ import annotations


class StoreScraperError(Exception):
    """Base class for every error raised by mobile_store_scraper."""


class AppNotFound(StoreScraperError):
    """The app does not exist, or is not available in the requested country."""

    def __init__(self, app_id: str, country: str | None = None, store: str | None = None):
        self.app_id = app_id
        self.country = country
        self.store = store
        where = f" in {country}" if country else ""
        super().__init__(f"{store or 'store'}: app {app_id!r} not found{where}")


class InvalidCountry(StoreScraperError):
    """The country code is not a storefront of the given store."""


class RateLimited(StoreScraperError):
    """The store kept answering 429/503 after all retries."""


class IpBlocked(RateLimited):
    """Google redirected to its "unusual traffic" (``google.com/sorry``) captcha page.

    Retrying immediately does not help; the block usually lifts after some minutes. Lower
    ``concurrency`` / set ``rate_limit`` or use a ``proxy`` for large sweeps.
    """


class NetworkError(StoreScraperError):
    """The host could not be reached (DNS, TLS, proxy, timeout...) after all retries."""


class ParseError(StoreScraperError):
    """The store answered, but the payload did not have the expected shape."""


class PlayAuthError(StoreScraperError):
    """Logging in to the Google Play Android API failed."""


class PlayApiError(StoreScraperError):
    """The Google Play Android API returned an error for a request."""
