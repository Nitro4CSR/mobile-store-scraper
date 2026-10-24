"""Async Apple App Store scraper.

Primary source is the iTunes lookup API (fast, JSON, up to 150 ids per call). The public
``apps.apple.com`` listing is used as a fallback when lookup has no result, and optionally
as a cross-check (``verify_with_web=True``) because lookup's CDN cache sometimes lags a
fresh release by a few hours.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Iterable, Optional, Union

from .countries import APP_STORE_COUNTRIES, normalize_country
from .dates import from_iso, from_js_date, to_iso
from .errors import AppNotFound, InvalidCountry, ParseError, StoreScraperError
from .http import HttpClient
from .models import AppStoreApp, VersionSource

LOOKUP_URL = "https://itunes.apple.com/lookup"
WEB_URL = "https://apps.apple.com/{cc}/app/id{id}"
_SSD_RE = re.compile(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S)
_VER_RE = re.compile(r"^\d+(\.\d+)*[\w.\-]*$")


def _version_key(v: Optional[str]) -> tuple:
    if not v:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))


def _is_numeric_id(app_id: Union[str, int]) -> bool:
    return _clean_id(app_id).isdigit()


def _clean_id(app_id: Union[str, int]) -> str:
    s = str(app_id).strip()
    return s[2:] if s.startswith("id") and s[2:].isdigit() else s


def parse_lookup_result(r: dict[str, Any], country: str, lang: Optional[str] = None) -> AppStoreApp:
    price = r.get("price")
    min_os = r.get("minimumOsVersion")
    return AppStoreApp(
        app_id=str(r.get("trackId")),
        country=country,
        language=lang,
        url=r.get("trackViewUrl"),
        title=r.get("trackName"),
        version=r.get("version"),
        version_source=VersionSource.ITUNES_LOOKUP.value if r.get("version") else None,
        last_updated=to_iso(from_iso(r.get("currentVersionReleaseDate"))),
        released=to_iso(from_iso(r.get("releaseDate"))),
        min_os_ver=f"iOS {min_os}" if min_os else None,
        publisher=r.get("artistName") or r.get("sellerName"),
        developer_id=str(r["artistId"]) if r.get("artistId") else None,
        developer_website=r.get("sellerUrl"),
        description=r.get("description"),
        release_notes=r.get("releaseNotes"),
        genre=r.get("primaryGenreName"),
        genre_id=str(r["primaryGenreId"]) if r.get("primaryGenreId") else None,
        icon=r.get("artworkUrl512") or r.get("artworkUrl100"),
        screenshots=r.get("screenshotUrls") or [],
        score=r.get("averageUserRating"),
        ratings=r.get("userRatingCount"),
        price=price,
        currency=r.get("currency"),
        free=(price == 0) if price is not None else None,
        content_rating=r.get("trackContentRating") or r.get("contentAdvisoryRating"),
        track_id=r.get("trackId"),
        bundle_id=r.get("bundleId"),
        seller=r.get("sellerName"),
        seller_url=r.get("sellerUrl"),
        developer_url=r.get("artistViewUrl"),
        file_size_bytes=int(r["fileSizeBytes"]) if str(r.get("fileSizeBytes", "")).isdigit() else None,
        supported_devices=r.get("supportedDevices") or [],
        ipad_screenshots=r.get("ipadScreenshotUrls") or [],
        appletv_screenshots=r.get("appletvScreenshotUrls") or [],
        languages=r.get("languageCodesISO2A") or [],
        genres=r.get("genres") or [],
        genre_ids=r.get("genreIds") or [],
        advisories=r.get("advisories") or [],
        features=r.get("features") or [],
        content_advisory_rating=r.get("contentAdvisoryRating"),
        score_current_version=r.get("averageUserRatingForCurrentVersion"),
        ratings_current_version=r.get("userRatingCountForCurrentVersion"),
        formatted_price=r.get("formattedPrice"),
        is_game_center_enabled=r.get("isGameCenterEnabled"),
        kind=r.get("kind"),
        raw={"lookup": r},
    )


def _walk(o: Any):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_web_html(html: str) -> dict[str, Any]:
    """Extract newest version + its timestamp (and title) from an apps.apple.com page."""
    m = _SSD_RE.search(html)
    if not m:
        raise ParseError("apps.apple.com page has no serialized-server-data block")
    data = json.loads(m.group(1))
    version = date = None
    for node in _walk(data):
        mrv = node.get("mostRecentVersion")
        if mrv is None:
            continue
        for sub in _walk(mrv):
            ps, ss = sub.get("primarySubtitle"), sub.get("secondarySubtitle")
            if isinstance(ps, str) and _VER_RE.match(ps) and isinstance(ss, str):
                version, date = ps, from_js_date(ss)
                break
        if version:
            break
    if not version:
        for sub in _walk(data):
            ps, ss = sub.get("primarySubtitle"), sub.get("secondarySubtitle")
            if isinstance(ps, str) and _VER_RE.match(ps) and isinstance(ss, str) and from_js_date(ss):
                version, date = ps, from_js_date(ss)
                break
    tm = (re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', html)
          or re.search(r'<meta[^>]*content="([^"]+)"[^>]*property="og:title"', html))
    return {"version": version, "updated": date, "title": tm.group(1) if tm else None}


class AppStore:
    """Async App Store scraper.

    ``app_id`` may be the numeric track id (``310633997`` / ``"id310633997"``) or a bundle id
    (``"net.whatsapp.WhatsApp"``). Pace: unlimited by default; set ``concurrency`` and/or
    ``rate_limit`` (requests/second) to slow down.
    """

    def __init__(
        self,
        *,
        verify_with_web: bool = False,
        web_fallback: bool = True,
        concurrency: Optional[int] = None,
        rate_limit: Optional[float] = None,
        http: Optional[HttpClient] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.verify_with_web = verify_with_web
        self.web_fallback = web_fallback
        self._own_http = http is None
        self.http = http or HttpClient(concurrency=concurrency, rate_limit=rate_limit, proxy=proxy)

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def __aenter__(self) -> "AppStore":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ raw calls
    async def lookup(self, ids: Iterable[Union[str, int]], country: str = "US",
                     lang: Optional[str] = None) -> list[dict[str, Any]]:
        """Raw iTunes lookup for numeric ids and/or bundle ids (batched, concurrent)."""
        country = normalize_country(country, APP_STORE_COUNTRIES, "App Store")
        ids = [_clean_id(i) for i in ids]
        numeric = [i for i in ids if i.isdigit()]
        bundles = [i for i in ids if not i.isdigit()]
        params_list = [{"id": ",".join(numeric[i:i + 150])} for i in range(0, len(numeric), 150)]
        params_list += [{"bundleId": ",".join(bundles[i:i + 150])} for i in range(0, len(bundles), 150)]

        async def one(params: dict) -> list[dict[str, Any]]:
            params.update({"country": country.lower(), "entity": "software"})
            if lang:
                params["lang"] = lang
            resp = await self.http.get(LOOKUP_URL, params=params)
            if resp.status_code == 400 and "country" in resp.text:
                raise InvalidCountry(f"{country} is not an App Store storefront")
            if resp.status_code != 200:
                raise StoreScraperError(f"iTunes lookup -> HTTP {resp.status_code}")
            try:
                data = json.loads(resp.text)
            except ValueError as exc:
                raise ParseError(f"iTunes lookup sent non-JSON: {resp.text[:120]!r}") from exc
            return [r for r in data.get("results", []) if r.get("wrapperType") == "software" or "trackId" in r]

        out: list[dict[str, Any]] = []
        for chunk in await asyncio.gather(*(one(p) for p in params_list)):
            out += chunk
        return out

    async def web(self, track_id: Union[str, int], country: str = "US") -> dict[str, Any]:
        country = normalize_country(country, APP_STORE_COUNTRIES, "App Store")
        resp = await self.http.get(WEB_URL.format(cc=country.lower(), id=_clean_id(track_id)))
        if resp.status_code == 404:
            raise AppNotFound(str(track_id), country, "app_store")
        if resp.status_code != 200:
            raise StoreScraperError(f"apps.apple.com {track_id}/{country} -> HTTP {resp.status_code}")
        return parse_web_html(resp.text)

    # ------------------------------------------------------------------ public
    async def app(self, app_id: Union[str, int], country: str = "US", *, lang: Optional[str] = None,
                  verify_with_web: Optional[bool] = None) -> AppStoreApp:
        country = normalize_country(country, APP_STORE_COUNTRIES, "App Store")
        results = await self.lookup([app_id], country, lang)
        verify = self.verify_with_web if verify_with_web is None else verify_with_web
        if results:
            info = parse_lookup_result(results[0], country, lang)
            if verify:
                try:
                    await self._merge_web(info)
                except StoreScraperError as exc:
                    info.raw["web_error"] = str(exc)
            return info
        if self.web_fallback and _is_numeric_id(app_id):
            w = await self.web(app_id, country)  # raises AppNotFound on 404
            if w["version"]:
                return AppStoreApp(
                    app_id=_clean_id(app_id), country=country, language=lang, title=w["title"],
                    version=w["version"], version_source=VersionSource.APP_STORE_WEB.value,
                    last_updated=to_iso(w["updated"]), track_id=int(_clean_id(app_id)),
                    url=WEB_URL.format(cc=country.lower(), id=_clean_id(app_id)), raw={"web": w},
                )
        raise AppNotFound(str(app_id), country, "app_store")

    async def _merge_web(self, info: AppStoreApp) -> None:
        w = await self.web(info.app_id, info.country)
        info.raw["web"] = w
        if w["version"] and _version_key(w["version"]) > _version_key(info.version):
            info.version = w["version"]
            info.version_source = VersionSource.APP_STORE_WEB.value
            if w["updated"]:
                info.last_updated = to_iso(w["updated"])

    async def apps(self, app_ids: Iterable[Union[str, int]], country: str = "US", *,
                   lang: Optional[str] = None) -> dict[str, Union[AppStoreApp, Exception]]:
        """Many apps, one country, batched into as few lookup calls as possible."""
        ids = [_clean_id(i) for i in app_ids]
        country = normalize_country(country, APP_STORE_COUNTRIES, "App Store")
        results = await self.lookup(ids, country, lang)
        by_key: dict[str, dict] = {}
        for r in results:
            by_key[str(r.get("trackId"))] = r
            if r.get("bundleId"):
                by_key[r["bundleId"]] = r
        return {i: (parse_lookup_result(by_key[i], country, lang) if i in by_key
                    else AppNotFound(i, country, "app_store")) for i in ids}

    async def app_all_countries(
        self, app_id: Union[str, int], countries: Optional[Iterable[str]] = None, *,
        lang: Optional[str] = None, verify_with_web: Optional[bool] = None,
    ) -> dict[str, Union[AppStoreApp, Exception]]:
        """``{country: AppStoreApp | AppNotFound | error}`` for every App Store storefront."""
        codes = [normalize_country(c, APP_STORE_COUNTRIES, "App Store") for c in (countries or APP_STORE_COUNTRIES)]
        res = await asyncio.gather(*(self.app(app_id, c, lang=lang, verify_with_web=verify_with_web) for c in codes),
                                   return_exceptions=True)
        return dict(zip(codes, res))
