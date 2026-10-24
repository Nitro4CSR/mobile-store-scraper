"""The simple front door.

    import mobile_store_scraper as mss

    app = mss.scrape(store="gp", app="com.naturalmotion.customstreetracer2", country="DE")
    app.version, app.last_updated, app.min_os_ver, app.downloads, app.released, app.publisher

    app = await mss.scrape_async(store="as", app="net.whatsapp.WhatsApp", country="JP")

    async with mss.Scraper(concurrency=20, rate_limit=10) as s:      # your own pace
        everywhere = await s.scrape_countries("gp", "com.whatsapp")   # all 225 countries
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .appstore import AppStore
from .countries import APP_STORE_COUNTRIES, PLAY_COUNTRIES
from .models import AppStoreApp, GooglePlayApp, Store

AppResult = Union[GooglePlayApp, AppStoreApp]

STORE_ALIASES = {
    "gp": Store.GOOGLE_PLAY, "google_play": Store.GOOGLE_PLAY, "googleplay": Store.GOOGLE_PLAY,
    "google": Store.GOOGLE_PLAY, "play": Store.GOOGLE_PLAY, "playstore": Store.GOOGLE_PLAY,
    "play_store": Store.GOOGLE_PLAY, "android": Store.GOOGLE_PLAY,
    "as": Store.APP_STORE, "app_store": Store.APP_STORE, "appstore": Store.APP_STORE, "apple": Store.APP_STORE,
    "ios": Store.APP_STORE, "itunes": Store.APP_STORE,
}


def resolve_store(store: Union[str, Store]) -> Store:
    if isinstance(store, Store):
        return store
    key = str(store).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in STORE_ALIASES:
        raise ValueError(f"unknown store {store!r}; use 'gp' (Google Play) or 'as' (Apple App Store)")
    return STORE_ALIASES[key]


class Scraper:
    """Both stores behind one object; reuse it for many requests (connection pooling, caches).

    Pace (applies per store, chosen by you, default = as fast as possible):
        ``concurrency``  max simultaneous requests, ``None`` = unlimited
        ``rate_limit``   requests per second per host, ``None`` = unlimited

    Google Play options: ``version_mode`` ("auto" | "web" | "api" | "all_devices"), ``device``,
    ``email`` + ``aas_token`` / ``auth_token`` (own account instead of anonymous),
    ``session_dir`` (Play API session cache, ``False`` disables), ``api_fallback_on_block``.
    App Store options: ``verify_with_web``.
    """

    def __init__(
        self,
        *,
        concurrency: Optional[int] = None,
        rate_limit: Optional[float] = None,
        proxy: Optional[str] = None,
        lang: Optional[str] = None,
        version_mode: str = "auto",
        device: Optional[str] = None,
        email: Optional[str] = None,
        aas_token: Optional[str] = None,
        auth_token: Optional[str] = None,
        session_dir: Union[str, Path, None, bool] = None,
        api_fallback_on_block: bool = False,
        verify_with_web: bool = False,
    ) -> None:
        self.lang = lang
        self._gp_kwargs: dict[str, Any] = dict(
            version_mode=version_mode, lang=lang or "en", email=email, aas_token=aas_token,
            auth_token=auth_token, session_dir=session_dir, concurrency=concurrency, rate_limit=rate_limit,
            api_fallback_on_block=api_fallback_on_block, proxy=proxy,
        )
        if device:
            self._gp_kwargs["device"] = device
        self._as_kwargs: dict[str, Any] = dict(verify_with_web=verify_with_web, concurrency=concurrency,
                                               rate_limit=rate_limit, proxy=proxy)
        self._gp: Optional[Any] = None
        self._as: Optional[AppStore] = None

    @property
    def google_play(self):
        if self._gp is None:
            from .play import GooglePlay
            self._gp = GooglePlay(**self._gp_kwargs)
        return self._gp

    @property
    def app_store(self) -> AppStore:
        if self._as is None:
            self._as = AppStore(**self._as_kwargs)
        return self._as

    async def aclose(self) -> None:
        if self._gp is not None:
            await self._gp.aclose()
        if self._as is not None:
            await self._as.aclose()

    async def __aenter__(self) -> "Scraper":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ calls
    async def scrape(self, store: Union[str, Store], app: Union[str, int], country: str = "US",
                     lang: Optional[str] = None) -> AppResult:
        """One app in one country. Raises ``AppNotFound`` when it isn't listed there."""
        if resolve_store(store) is Store.GOOGLE_PLAY:
            return await self.google_play.app(str(app), country, lang=lang or self.lang)
        return await self.app_store.app(app, country, lang=lang or self.lang)

    async def scrape_countries(self, store: Union[str, Store], app: Union[str, int],
                               countries: Union[Iterable[str], str, None] = None,
                               lang: Optional[str] = None) -> dict[str, Union[AppResult, Exception]]:
        """``{country: result | AppNotFound | error}``; ``countries=None`` or ``"all"`` = every storefront."""
        if isinstance(countries, str):
            countries = None if countries.lower() == "all" else [c.strip() for c in countries.split(",")]
        if resolve_store(store) is Store.GOOGLE_PLAY:
            return await self.google_play.app_all_countries(str(app), countries, lang=lang or self.lang)
        return await self.app_store.app_all_countries(app, countries, lang=lang or self.lang)

    async def scrape_many(self, store: Union[str, Store], apps: Iterable[Union[str, int]], country: str = "US",
                          lang: Optional[str] = None) -> dict[str, Union[AppResult, Exception]]:
        """Many apps, one country: ``{app: result | exception}``."""
        if resolve_store(store) is Store.GOOGLE_PLAY:
            return await self.google_play.apps([str(a) for a in apps], country, lang=lang or self.lang)
        return await self.app_store.apps(apps, country, lang=lang or self.lang)


# ---------------------------------------------------------------------- module-level helpers
async def scrape_async(store: Union[str, Store], app: Union[str, int], country: str = "US", *,
                       lang: Optional[str] = None, **options: Any) -> AppResult:
    """``await scrape_async("gp", "com.whatsapp", "DE")``; ``options`` go to :class:`Scraper`."""
    async with Scraper(lang=lang, **options) as s:
        return await s.scrape(store, app, country)


async def scrape_countries_async(store: Union[str, Store], app: Union[str, int],
                                 countries: Union[Iterable[str], str, None] = None, *,
                                 lang: Optional[str] = None, **options: Any):
    async with Scraper(lang=lang, **options) as s:
        return await s.scrape_countries(store, app, countries)


async def scrape_many_async(store: Union[str, Store], apps: Iterable[Union[str, int]], country: str = "US", *,
                            lang: Optional[str] = None, **options: Any):
    async with Scraper(lang=lang, **options) as s:
        return await s.scrape_many(store, apps, country)


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("scrape() is the synchronous helper; inside async code use "
                       "`await scrape_async(...)` or `async with Scraper() as s: await s.scrape(...)`")


def scrape(store: Union[str, Store], app: Union[str, int], country: str = "US", *,
           lang: Optional[str] = None, **options: Any) -> AppResult:
    """Synchronous one-shot: ``scrape(store="gp", app="com.whatsapp", country="DE")``."""
    return _run(scrape_async(store, app, country, lang=lang, **options))


def scrape_countries(store: Union[str, Store], app: Union[str, int],
                     countries: Union[Iterable[str], str, None] = None, *,
                     lang: Optional[str] = None, **options: Any):
    """Synchronous all-country sweep (``countries=None`` or ``"all"``)."""
    return _run(scrape_countries_async(store, app, countries, lang=lang, **options))


def scrape_many(store: Union[str, Store], apps: Iterable[Union[str, int]], country: str = "US", *,
                lang: Optional[str] = None, **options: Any):
    """Synchronous many-apps helper."""
    return _run(scrape_many_async(store, apps, country, lang=lang, **options))


__all__ = ["Scraper", "scrape", "scrape_async", "scrape_countries", "scrape_countries_async",
           "scrape_many", "scrape_many_async", "resolve_store", "STORE_ALIASES",
           "PLAY_COUNTRIES", "APP_STORE_COUNTRIES"]
