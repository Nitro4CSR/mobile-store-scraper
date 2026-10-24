"""High-level async Google Play scraper: web listing + Android API for exact versions."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Literal, Optional, Union

from ..countries import PLAY_COUNTRIES, normalize_country
from ..dates import to_iso
from ..errors import AppNotFound, ParseError, RateLimited, StoreScraperError
from ..http import HttpClient
from ..models import DeviceVersion, GooglePlayApp, VersionSource
from .api import PlayApiClient, PlayApiDetails
from .device import DEFAULT_DEVICE, REPRESENTATIVE_DEVICES, DeviceProfile, get_profile
from .web import parse_details_html

DETAILS_URL = "https://play.google.com/store/apps/details"
VersionMode = Literal["auto", "web", "api", "all_devices"]


def default_cache_dir() -> Path:
    """Where Play API sessions are cached unless ``session_dir`` says otherwise."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "mobile_store_scraper"


def _downloads(real: Optional[int], minimum: Optional[int], label: Optional[str]) -> Union[int, str, None]:
    if isinstance(real, int) and real > 0:
        return real
    if isinstance(minimum, int) and minimum > 0:
        return f"{minimum}+"
    return label


class GooglePlay:
    """Async Google Play scraper.

    ``version_mode``:

    * ``"auto"`` (default) - version from the web listing; when the listing says "Varies with
      device" (multi-APK / app-bundle builds) it is resolved through the Android Play API for
      ``device``, which also fills ``min_os_ver``, ``target_sdk``, ``download_size``.
    * ``"web"`` - web listing only, never log in (``version`` may be ``None``).
    * ``"api"`` - always ask the Android API too (``version_code`` for every app).
    * ``"all_devices"`` - ask the API once per profile in ``devices``; every build is in
      ``device_versions`` and ``version`` is the highest version code found.

    Pace: unlimited by default (``concurrency=None, rate_limit=None``). Pass ``concurrency``
    (max simultaneous requests) and/or ``rate_limit`` (requests/second) to slow down.

    ``api_fallback_on_block=True``: if the web listing is throttled / captcha-blocked, still
    return version + dates from the Android API (country availability then unverified).

    Credentials: by default an anonymous token is borrowed from Aurora Store's dispenser and
    the session is cached in ``session_dir`` (default: the user cache dir; ``False`` disables).
    Pass ``email`` + ``aas_token`` (or ``auth_token``) to use your own Google account.
    """

    def __init__(
        self,
        *,
        version_mode: VersionMode = "auto",
        device: Union[str, DeviceProfile] = DEFAULT_DEVICE,
        devices: Iterable[str] = REPRESENTATIVE_DEVICES,
        lang: str = "en",
        email: Optional[str] = None,
        aas_token: Optional[str] = None,
        auth_token: Optional[str] = None,
        dispenser_url: Optional[str] = None,
        session_dir: Union[str, Path, None, bool] = None,
        concurrency: Optional[int] = None,
        rate_limit: Optional[float] = None,
        api_concurrency: Optional[int] = None,
        api_cache_ttl: float = 600.0,
        api_fallback_on_block: bool = False,
        http: Optional[HttpClient] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.version_mode = version_mode
        self.device = get_profile(device).name
        self.devices = tuple(devices)
        self.lang = lang
        self._creds = dict(email=email, aas_token=aas_token, auth_token=auth_token)
        if dispenser_url:
            self._creds["dispenser_url"] = dispenser_url
        if session_dir is False:
            self.session_dir: Optional[Path] = None
        else:
            self.session_dir = Path(session_dir) if session_dir else default_cache_dir()
        self._own_http = http is None
        self.http = http or HttpClient(concurrency=concurrency, rate_limit=rate_limit, proxy=proxy)
        self._api_http = HttpClient(concurrency=api_concurrency, proxy=proxy)
        self._apis: dict[str, PlayApiClient] = {}
        self._api_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._api_inflight: dict[tuple[str, str], asyncio.Future] = {}
        self.api_cache_ttl = api_cache_ttl
        self.api_fallback_on_block = api_fallback_on_block

    # ------------------------------------------------------------------ lifecycle
    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()
        await self._api_http.aclose()

    async def __aenter__(self) -> "GooglePlay":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ API plumbing
    def api(self, device: Optional[str] = None) -> PlayApiClient:
        name = device or self.device
        if name not in self._apis:
            sf = None
            if self.session_dir:
                kind = "anon" if not (self._creds["aas_token"] or self._creds["auth_token"]) else "account"
                sf = self.session_dir / f"play_session_{kind}_{name}.json"
            self._apis[name] = PlayApiClient(device=name, session_file=sf, http=self._api_http, **self._creds)
        return self._apis[name]

    async def api_details(self, app_id: str, device: Optional[str] = None) -> PlayApiDetails:
        """Android API details for one device, cached for ``api_cache_ttl`` seconds and de-duplicated
        across concurrent callers (so an all-country sweep costs one API call, not 225)."""
        key = (app_id, device or self.device)
        hit = self._api_cache.get(key)
        if hit and time.monotonic() - hit[0] < self.api_cache_ttl:
            if isinstance(hit[1], BaseException):
                raise hit[1]
            return hit[1]  # type: ignore[return-value]
        if key in self._api_inflight:
            return await asyncio.shield(self._api_inflight[key])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._api_inflight[key] = fut
        try:
            res = await self.api(key[1]).details(app_id)
            self._api_cache[key] = (time.monotonic(), res)
            fut.set_result(res)
            return res
        except AppNotFound as exc:
            self._api_cache[key] = (time.monotonic(), exc)
            fut.set_exception(exc)
            raise
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._api_inflight.pop(key, None)
            if fut.done() and not fut.cancelled():
                fut.exception()  # mark retrieved

    async def versions_by_device(self, app_id: str, devices: Optional[Iterable[str]] = None) -> list[DeviceVersion]:
        """Exact version for each device profile (different ABIs/SDKs/form factors)."""
        names = list(devices or self.devices)

        async def one(name: str) -> DeviceVersion:
            prof = get_profile(name)
            try:
                d = await self.api_details(app_id, name)
                err = None if d.version else "not offered to this device (incompatible or not available)"
                return DeviceVersion(name, d.version, d.version_code, prof.sdk, ",".join(prof.platforms), error=err)
            except StoreScraperError as exc:
                return DeviceVersion(name, None, None, prof.sdk, ",".join(prof.platforms), error=str(exc))

        return list(await asyncio.gather(*(one(n) for n in names)))

    # ------------------------------------------------------------------ web
    async def _web(self, app_id: str, country: str, lang: str, attempts: int = 3) -> dict:
        for attempt in range(attempts):
            resp = await self.http.get(DETAILS_URL, params={"id": app_id, "hl": lang, "gl": country})
            if resp.status_code == 404:
                raise AppNotFound(app_id, country, "google_play")
            if resp.status_code != 200:
                raise StoreScraperError(f"Play listing {app_id}/{country} -> HTTP {resp.status_code}")
            try:
                return parse_details_html(resp.text)
            except ParseError:
                # Play occasionally serves a degraded 200 page without the data blocks; it is transient.
                if attempt == attempts - 1:
                    raise
                self.http.stats["retries"] += 1
                await asyncio.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ public
    async def app(
        self,
        app_id: str,
        country: str = "US",
        *,
        lang: Optional[str] = None,
        version_mode: Optional[VersionMode] = None,
    ) -> GooglePlayApp:
        """Details for one app in one country. Raises ``AppNotFound`` if it is not listed there."""
        country = normalize_country(country, PLAY_COUNTRIES, "Google Play")
        lang = lang or self.lang
        mode = version_mode or self.version_mode
        try:
            web = await self._web(app_id, country, lang)
        except RateLimited as exc:
            if not self.api_fallback_on_block or mode == "web":
                raise
            return await self._api_only(app_id, country, lang, mode, exc)

        fields = {k: v for k, v in web.items() if k != "app_id"}
        info = GooglePlayApp(
            app_id=app_id, country=country, language=lang,
            url=f"{DETAILS_URL}?id={app_id}&hl={lang}&gl={country}",
            version_source=VersionSource.PLAY_WEB.value if web["version"] else None,
            downloads=_downloads(web["real_installs"], web["min_installs"], web["installs"]),
            raw={"web": web},
            **fields,
        )
        # "auto": the API fills what the listing leaves as "Varies with device" (version and/or
        # "Requires Android"); one cached call per app, shared by every country.
        need_api = mode in ("api", "all_devices") or (mode == "auto" and not (info.version and info.min_os_ver))
        if need_api:
            await self._apply_api(info, mode)
        return info

    async def _api_only(self, app_id: str, country: str, lang: str, mode: str, web_exc: Exception) -> GooglePlayApp:
        """Web listing unavailable (throttled/blocked): answer from the Android API alone.

        ``last_updated`` then has day precision and availability in ``country`` is NOT
        verified (``raw["country_verified"] is False``).
        """
        d = await self.api_details(app_id)  # AppNotFound propagates
        info = GooglePlayApp(
            app_id=app_id, country=country, language=lang,
            url=f"{DETAILS_URL}?id={app_id}&hl={lang}&gl={country}",
            title=d.title, publisher=d.developer, developer=d.developer,
            last_updated=to_iso(d.updated), released=to_iso(d.released),
            raw={"web_error": f"{type(web_exc).__name__}: {web_exc}", "country_verified": False},
        )
        await self._apply_api(info, "api" if mode == "auto" else mode)
        return info

    async def _apply_api(self, info: GooglePlayApp, mode: str) -> None:
        if mode == "all_devices":
            dvs = await self.versions_by_device(info.app_id)
            info.device_versions = dvs
            ok = [d for d in dvs if d.version_code is not None]
            if ok:
                best = max(ok, key=lambda d: d.version_code or 0)
                info.version, info.version_code = best.version, best.version_code
                info.version_source = VersionSource.PLAY_API.value
                info.varies_with_device = info.varies_with_device or len({d.version_code for d in ok}) > 1
            else:
                info.raw["api_error"] = "; ".join(d.error or "" for d in dvs)
            try:
                self._merge_api_details(info, await self.api_details(info.app_id))
            except StoreScraperError:
                pass
            return
        try:
            d = await self.api_details(info.app_id)
        except StoreScraperError as exc:
            info.raw["api_error"] = f"{type(exc).__name__}: {exc}"
            return
        prof = get_profile(self.device)
        info.device_versions = [DeviceVersion(d.device, d.version, d.version_code, prof.sdk, ",".join(prof.platforms))]
        if d.version and (not info.version or mode == "api"):
            info.version, info.version_code = d.version, d.version_code
            info.version_source = VersionSource.PLAY_API.value
        elif d.version == info.version:
            info.version_code = d.version_code
        self._merge_api_details(info, d)

    @staticmethod
    def _merge_api_details(info: GooglePlayApp, d: PlayApiDetails) -> None:
        """Fill gaps the web listing left (typical for "Varies with device" apps)."""
        info.raw["api"] = d
        info.version_last_updated = to_iso(d.updated)
        info.target_sdk = info.target_sdk or d.target_sdk
        info.download_size = info.download_size or d.download_size
        info.min_os_ver = info.min_os_ver or d.min_os_ver
        info.real_installs = info.real_installs or d.real_installs
        info.min_installs = info.min_installs or d.min_installs
        info.downloads = _downloads(info.real_installs, info.min_installs, info.installs or d.installs_label)
        info.last_updated = info.last_updated or to_iso(d.updated)
        info.released = info.released or to_iso(d.released)
        info.title = info.title or d.title
        info.publisher = info.publisher or d.developer
        info.developer = info.developer or d.developer
        info.developer_email = info.developer_email or d.developer_email
        info.developer_website = info.developer_website or d.developer_website
        info.developer_address = info.developer_address or d.developer_address
        info.in_app_product_price = info.in_app_product_price or d.in_app_product_price
        info.offered_by = info.offered_by or d.offered_by
        if info.contains_ads is None:
            info.contains_ads = d.contains_ads

    async def app_all_countries(
        self,
        app_id: str,
        countries: Optional[Iterable[str]] = None,
        *,
        lang: Optional[str] = None,
        version_mode: Optional[VersionMode] = None,
        return_exceptions: bool = True,
    ) -> dict[str, Union[GooglePlayApp, Exception]]:
        """Fetch the listing in every Play country concurrently.

        Returns ``{country: GooglePlayApp | exception}``; ``AppNotFound`` marks countries where the
        app is not available. The Android API is hit at most once per device thanks to caching.
        """
        codes = [normalize_country(c, PLAY_COUNTRIES, "Google Play") for c in (countries or PLAY_COUNTRIES)]
        results = await asyncio.gather(
            *(self.app(app_id, c, lang=lang, version_mode=version_mode) for c in codes),
            return_exceptions=return_exceptions,
        )
        return dict(zip(codes, results))

    async def apps(
        self, app_ids: Iterable[str], country: str = "US", *, lang: Optional[str] = None,
        version_mode: Optional[VersionMode] = None,
    ) -> dict[str, Union[GooglePlayApp, Exception]]:
        ids = list(app_ids)
        res = await asyncio.gather(*(self.app(a, country, lang=lang, version_mode=version_mode) for a in ids),
                                   return_exceptions=True)
        return dict(zip(ids, res))


__all__ = ["GooglePlay", "default_cache_dir"]
