"""Async client for the Google Play *Android* API (``android.clients.google.com/fdfe``).

This is the API the Play Store app itself uses. Unlike the public web listing it answers
with the exact ``versionCode`` / ``versionString`` that a given device would install, which
is the only reliable source for apps published as multi-APK or app bundles that the web
listing shows as "Varies with device".

Three ways to log in:

* ``PlayApiClient.anonymous()`` - borrows a short-lived token from Aurora Store's public
  token dispenser (default; no Google account needed).
* ``PlayApiClient.with_aas_token(email, aas_token)`` - your own Google account (AAS token).
* ``PlayApiClient.with_auth_token(email, auth_token)`` - an OAuth ``ya29.`` token you already have.

Protocol details follow EFF's rs-google-play and AuroraOSS gplayapi.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from ..dates import from_play_api_date, from_timestamp
from ..errors import AppNotFound, PlayApiError, PlayAuthError
from ..http import HttpClient
from .device import DeviceProfile, get_profile
from .proto import Msg, encode

BASE = "https://android.clients.google.com"
FDFE = BASE + "/fdfe"
DISPENSER_URL = "https://auroraoss.com/api/auth"
DISPENSER_USER_AGENT = "com.aurora.store-4.8.4-76"

CLIENT_SIG = "38918a453d07199354f8b19af05ec6562ced5788"
GMS = "com.google.android.gms"
DFE_TARGETS = (
    "CAESN/qigQYC2AMBFfUbyA7SM5Ij/CvfBoIDgxHqGP8R3xzIBvoQtBKFDZ4HAY4FrwSVMasHBO0O2Q8akgYRAQECAQO7AQEpKZ0CnwECAwRrAQYBr9PPAoK7sQMB"
    "AQMCBAkIDAgBAwEDBAICBAUZEgMEBAMLAQEBBQEBAcYBARYED+cBfS8CHQEKkAEMMxcBIQoUDwYHIjd3DQ4MFk0JWGYZEREYAQOLAYEBFDMIEYMBAgICAgICOxkC"
    "D18LGQKEAcgDBIQBAgGLARkYCy8oBTJlBCUocxQn0QUBDkkGxgNZQq0BZSbeAmIDgAEBOgGtAaMCDAOQAZ4BBIEBKUtQUYYBQscDDxPSARA1oAEHAWmnAsMB2wFy"
    "ywGLAxol+wImlwOOA80CtwN26A0WjwJVbQEJPAH+BRDeAfkHK/ABASEBCSAaHQemAzkaRiu2Ad8BdXeiAwEBGBUBBN4LEIABK4gB2AFLfwECAdoENq0CkQGMBsIB"
    "iQEtiwGgA1zyAUQ4uwS8AwhsvgPyAcEDF27vApsBHaICGhl3GSKxAR8MC6cBAgItmQYG9QIeywLvAeYBDArLAh8HASI4ELICDVmVBgsY/gHWARtcAsMBpALiAdsB"
    "A7QBpAJmIArpByn0AyAKBwHTARIHAX8D+AMBcRIBBbEDmwUBMacCHAciNp0BAQF0OgQLJDuSAh54kwFSP0eeAQQ4M5EBQgMEmwFXywFo0gFyWwMcapQBBugBPUW2"
    "AVgBKmy3AR6PAbMBGQxrUJECvQR+8gFoWDsYgQNwRSczBRXQAgtRswEW0ALMAREYAUEBIG6yATYCRE8OxgER8gMBvQEDRkwLc8MBTwHZAUOnAXiiBakDIbYBNNcC"
    "IUmuArIBSakBrgFHKs0EgwV/G3AD0wE6LgECtQJ4xQFwFbUCjQPkBS6vAQqEAUZF3QIM9wEhCoYCQhXsBCyZArQDugIziALWAdIBlQHwBdUErQE6qQaSA4EEIvYB"
    "Hir9AQVLmgMCApsCKAwHuwgrENsBAjNYswEVmgIt7QJnN4wDEnta+wGfAcUBxgEtEFXQAQWdAUAeBcwBAQM7rAEJATJ0LENrdh73A6UBhAE+qwEeASxLZUMhDREu"
    "H0CGARbd7K0GlQo"
)
DFE_PHENOTYPE = (
    "H4sIAAAAAAAAAB3OO3KjMAAA0KRNuWXukBkBQkAJ2MhgAZb5u2GCwQZbCH_EJ77QHmgvtDtbv-Z9_H63zXXU0NVPB1odlyGy7751Q3CitlPDvFd8lxhz3tpNmz7P92C"
    "Fw73zdHU2Ie0Ad2kmR8lxhiErTFLt3RPGfJQHSDy7Clw10bg8kqf2owLokN4SecJTLoSwBnzQSd652_MOf2d1vKBNVedzg4ciPoLz2mQ8efGAgYeLou-l-PXn_7Sna1"
    "MfhHuySxt-4esulEDp8Sbq54CPPKjpANW-lkU2IZ0F92LBI-ukCKSptqeq1eXU96LD9nZfhKHdtjSWwJqUm_2r6pMHOxk01saVanmNopjX3YxQafC4iC6T55aRbC8nTI"
    "98AF_kItIQAJb5EQxnKTO7TZDWnr01HVPxelb9A2OWX6poidMWl16K54kcu_jhXw-JSBQkVcD_fPsLSZu6joIBAAA"
)


@dataclass
class PlaySession:
    """Everything needed to make authenticated fdfe calls. JSON-serialisable for caching."""
    email: str
    auth_token: str
    device: str
    gsf_id: int
    device_config_token: str
    checkin_consistency_token: Optional[str] = None
    dfe_cookie: Optional[str] = None
    locale: str = "en_US"
    timezone: str = "UTC"
    anonymous: bool = True
    created: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "PlaySession":
        return cls(**json.loads(text))


@dataclass
class PlayApiDetails:
    package: str
    title: Optional[str]
    developer: Optional[str]
    version: Optional[str]
    version_code: Optional[int]
    updated: Optional[datetime]
    updated_raw: Optional[str]
    target_sdk: Optional[int]
    downloads: Optional[str]
    device: str
    recent_changes: Optional[str] = None
    min_os_ver: Optional[str] = None       # "Android 7.0" (from "Android 7.0 and up")
    real_installs: Optional[int] = None    # exact install count
    min_installs: Optional[int] = None
    installs_label: Optional[str] = None   # "50,000,000+ downloads"
    released: Optional[datetime] = None
    download_size: Optional[int] = None
    developer_email: Optional[str] = None
    developer_website: Optional[str] = None
    developer_address: Optional[str] = None
    contains_ads: Optional[bool] = None
    in_app_product_price: Optional[str] = None
    offered_by: Optional[str] = None


def _parse_form(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _min_os_label(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return re.sub(r"\s+and up\s*$", "", text.strip(), flags=re.I) or None


def _item_to_details(item: Msg, device: str, fallback_pkg: str = "") -> Optional[PlayApiDetails]:
    if not item:
        return None
    app = item.msg(13).msg(1)
    pkg = app.str(14) or item.str(1) or fallback_pkg
    updated_raw = app.str(16)
    released_ts = app.msg(64).msg(2).int(1)
    label = app.str(13)
    return PlayApiDetails(
        package=pkg,
        title=item.str(5) or app.str(5),
        developer=item.str(6) or app.str(1),
        version=app.str(4),
        version_code=app.int(3),
        updated=from_play_api_date(updated_raw),
        updated_raw=updated_raw,
        target_sdk=app.int(32),
        downloads=app.str(78) or app.str(61),
        device=device,
        recent_changes=app.str(15),
        min_os_ver=_min_os_label(app.msg(82).msg(1).str(1)),
        real_installs=app.int(70),
        min_installs=app.int(53),
        installs_label=label,
        released=from_timestamp(released_ts) if released_ts else from_play_api_date(app.msg(64).str(1)),
        download_size=app.int(9),
        developer_email=app.str(11) or app.msg(86).str(2),
        developer_website=app.str(12),
        developer_address=re.sub(r"<br\s*/?>", "\n", app.msg(86).str(3)) if app.msg(86).str(3) else None,
        contains_ads=bool(app.str(30)) if app.has(30) else False,
        in_app_product_price=app.str(67),
        offered_by=app.msg(68).str(1),
    )


class PlayApiClient:
    """Authenticated, async Play Android API client bound to one device profile.

    Login happens lazily on first use and is shared by concurrent callers. Pass
    ``session_file`` to persist the session between runs (strongly recommended for
    anonymous logins - the dispenser is a shared community resource).
    """

    def __init__(
        self,
        *,
        device: Union[str, DeviceProfile, None] = None,
        email: Optional[str] = None,
        aas_token: Optional[str] = None,
        auth_token: Optional[str] = None,
        dispenser_url: Optional[str] = DISPENSER_URL,
        locale: str = "en_US",
        timezone: str = "UTC",
        accept_tos: Optional[bool] = None,
        session_file: Union[str, Path, None] = None,
        http: Optional[HttpClient] = None,
        concurrency: Optional[int] = None,
    ) -> None:
        self.profile = get_profile(device)
        self.email = email
        self.aas_token = aas_token
        self.auth_token = auth_token
        self.dispenser_url = dispenser_url
        self.locale = locale
        self.timezone = timezone
        self.anonymous = not (aas_token or auth_token)
        # Dispenser accounts are throwaway: accepting ToS for them is what Aurora does.
        self.accept_tos = self.anonymous if accept_tos is None else accept_tos
        self.session_file = Path(session_file) if session_file else None
        self._own_http = http is None
        self.http = http or HttpClient(concurrency=concurrency)
        self.session: Optional[PlaySession] = None
        self._login_lock = asyncio.Lock()
        if self.anonymous and not dispenser_url:
            raise ValueError("no credentials and no dispenser_url: cannot log in")

    # ----------------------------------------------------------------- constructors
    @classmethod
    def anonymous_client(cls, **kw: Any) -> "PlayApiClient":
        return cls(**kw)

    @classmethod
    def with_aas_token(cls, email: str, aas_token: str, **kw: Any) -> "PlayApiClient":
        return cls(email=email, aas_token=aas_token, **kw)

    @classmethod
    def with_auth_token(cls, email: str, auth_token: str, **kw: Any) -> "PlayApiClient":
        return cls(email=email, auth_token=auth_token, **kw)

    # ----------------------------------------------------------------- context manager
    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def __aenter__(self) -> "PlayApiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ----------------------------------------------------------------- login
    async def login(self, force: bool = False) -> PlaySession:
        async with self._login_lock:
            if self.session and not force:
                return self.session
            if not force and self.session_file and self.session_file.exists():
                try:
                    s = PlaySession.from_json(self.session_file.read_text(encoding="utf-8"))
                    if s.device == self.profile.name and s.locale == self.locale:
                        self.session = s
                        return s
                except (ValueError, TypeError, KeyError):
                    pass
            self.session = await self._fresh_login()
            if self.session_file:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(self.session.to_json(), encoding="utf-8")
            return self.session

    async def _fresh_login(self) -> PlaySession:
        email, token = self.email, self.auth_token
        if self.anonymous:
            email, token = await self._dispenser_token()
        gsf_id, consistency = await self._checkin()
        partial = PlaySession(
            email=email or "", auth_token=token or "", device=self.profile.name, gsf_id=gsf_id,
            device_config_token="", checkin_consistency_token=consistency,
            locale=self.locale, timezone=self.timezone, anonymous=self.anonymous,
        )
        partial.device_config_token = await self._upload_device_config(partial)
        if not partial.auth_token:
            partial.auth_token = await self._exchange_aas_token(gsf_id)
        partial.dfe_cookie = await self._toc(partial)
        return partial

    async def _dispenser_token(self) -> tuple[str, str]:
        body = json.dumps(self.profile.props).encode()
        resp = await self.http.post(
            self.dispenser_url, content=body,
            headers={"User-Agent": DISPENSER_USER_AGENT, "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise PlayAuthError(f"token dispenser answered {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise PlayAuthError(f"token dispenser sent non-JSON: {resp.text[:200]}") from exc
        token = data.get("authToken") or data.get("auth")
        if not token:
            raise PlayAuthError(f"token dispenser response has no token: {list(data)}")
        return data.get("email", ""), token

    async def _checkin(self) -> tuple[int, Optional[str]]:
        body = encode(self.profile.checkin_request(self.locale, self.timezone))
        resp = await self.http.post(
            BASE + "/checkin", content=body,
            headers={"Content-Type": "application/x-protobuf", "User-Agent": self.profile.auth_user_agent(), "app": GMS},
        )
        if resp.status_code != 200:
            raise PlayAuthError(f"checkin failed with HTTP {resp.status_code}")
        msg = Msg.decode(resp.content)
        gsf_id = msg.int(7, signed=False)
        if not gsf_id:
            raise PlayAuthError("checkin response carried no android id")
        return gsf_id, msg.str(12)

    async def _exchange_aas_token(self, gsf_id: int) -> str:
        p = self.profile
        form = {
            "androidId": f"{gsf_id:x}", "sdk_version": str(p.sdk), "Email": self.email or "",
            "google_play_services_version": p.get("GSF.version"), "device_country": "us", "lang": "en",
            "callerSig": CLIENT_SIG, "app": "com.android.vending", "client_sig": CLIENT_SIG,
            "callerPkg": GMS, "Token": self.aas_token or "", "oauth2_foreground": "1",
            "token_request_options": "CAA4AVAB", "check_email": "1", "system_partition": "1",
            "service": "oauth2:https://www.googleapis.com/auth/googleplay",
        }
        resp = await self.http.post(
            BASE + "/auth", data=form,
            headers={"User-Agent": p.auth_user_agent(), "app": GMS, "device": f"{gsf_id:x}"},
        )
        reply = _parse_form(resp.text)
        if resp.status_code != 200 or "auth" not in reply:
            raise PlayAuthError(f"AAS token exchange failed (HTTP {resp.status_code}): {reply.get('error', resp.text[:200])}")
        return reply["auth"]

    def _headers(self, s: Optional[PlaySession]) -> dict[str, str]:
        p = self.profile
        h = {
            "User-Agent": p.finsky_user_agent(),
            "Accept-Language": self.locale.replace("_", "-"),
            "X-DFE-Encoded-Targets": DFE_TARGETS,
            "X-DFE-Phenotype": DFE_PHENOTYPE,
            "X-DFE-Client-Id": "am-android-google",
            "X-DFE-Network-Type": "4",
            "X-DFE-Content-Filters": "",
            "X-Limit-Ad-Tracking-Enabled": "false",
            "X-Ad-Id": "",
            "X-DFE-UserLanguages": self.locale,
            "X-DFE-Request-Params": "timeoutMs=4000",
        }
        if p.get("SimOperator"):
            h["X-DFE-MCCMCN"] = p.get("SimOperator")
        if s is not None:
            if s.auth_token:
                h["Authorization"] = f"Bearer {s.auth_token}"
            h["X-DFE-Device-Id"] = f"{s.gsf_id:x}"
            if s.checkin_consistency_token:
                h["X-DFE-Device-Checkin-Consistency-Token"] = s.checkin_consistency_token
            if s.device_config_token:
                h["X-DFE-Device-Config-Token"] = s.device_config_token
            if s.dfe_cookie:
                h["X-DFE-Cookie"] = s.dfe_cookie
        return h

    async def _fdfe(self, s: PlaySession, method: str, path: str, *, params: Optional[dict] = None,
                    content: Optional[bytes] = None, extra_headers: Optional[dict] = None):
        headers = self._headers(s)
        if extra_headers:
            headers.update(extra_headers)
        resp = await self.http.request(method, f"{FDFE}/{path}", params=params, content=content, headers=headers)
        wrapper = Msg.decode(resp.content) if resp.content else Msg()
        return resp, wrapper

    async def _upload_device_config(self, s: PlaySession) -> str:
        body = encode([(1, self.profile.device_configuration())])
        resp, w = await self._fdfe(s, "POST", "uploadDeviceConfig", content=body,
                                   extra_headers={"Content-Type": "application/x-protobuf"})
        token = w.msg(1).msg(28).str(1)
        if resp.status_code != 200 or not token:
            raise PlayAuthError(f"uploadDeviceConfig failed (HTTP {resp.status_code}): {self._error_text(w)}")
        return token

    async def _toc(self, s: PlaySession) -> Optional[str]:
        resp, w = await self._fdfe(s, "GET", "toc")
        if resp.status_code != 200:
            raise PlayAuthError(f"toc failed (HTTP {resp.status_code}): {self._error_text(w)}")
        toc = w.msg(1).msg(6)
        tos_token = toc.str(7)
        if tos_token:
            if not self.accept_tos:
                raise PlayAuthError("this Google account has not accepted the Play Terms of Service; "
                                    "pass accept_tos=True to accept them")
            await self._fdfe(s, "POST", "acceptTos", content=f"tost={tos_token}&toscme=false".encode(),
                             extra_headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp, w = await self._fdfe(s, "GET", "toc")
            toc = w.msg(1).msg(6)
        return toc.str(22)

    @staticmethod
    def _error_text(w: Msg) -> str:
        return w.msg(2).str(2) or ""

    # ----------------------------------------------------------------- public calls
    async def _authed(self, method: str, path: str, **kw: Any):
        s = await self.login()
        resp, w = await self._fdfe(s, method, path, **kw)
        if resp.status_code == 401:  # token expired or revoked -> log in again once
            s = await self.login(force=True)
            resp, w = await self._fdfe(s, method, path, **kw)
        return resp, w

    async def details(self, package: str) -> PlayApiDetails:
        resp, w = await self._authed("GET", "details", params={"doc": package})
        item = w.msg(1).msg(2).msg(4)
        if resp.status_code == 404 or (not item and "not found" in self._error_text(w).lower()):
            raise AppNotFound(package, store="google_play")
        if resp.status_code != 200 or not item:
            raise PlayApiError(f"details({package}) -> HTTP {resp.status_code}: {self._error_text(w) or 'empty payload'}")
        d = _item_to_details(item, self.profile.name, package)
        assert d is not None
        return d

    async def bulk_details(self, packages: list[str]) -> dict[str, Optional[PlayApiDetails]]:
        """One request for many packages. Missing / unavailable apps map to ``None``."""
        body = encode([(1, p) for p in packages] + [(2, False), (3, True)])
        resp, w = await self._authed("POST", "bulkDetails", content=body,
                                     extra_headers={"Content-Type": "application/x-protobuf"})
        if resp.status_code != 200:
            raise PlayApiError(f"bulkDetails -> HTTP {resp.status_code}: {self._error_text(w)}")
        entries = list(w.msg(1).msg(19).msgs(1))
        out: dict[str, Optional[PlayApiDetails]] = {p: None for p in packages}
        for pkg, entry in zip(packages, entries):  # entries come back in request order
            d = _item_to_details(entry.msg(1), self.profile.name, pkg)
            if d is not None:
                out[d.package or pkg] = d
        return out


    # ----------------------------------------------------------------- downloads
    async def purchase(self, package: str, version_code: int) -> Optional[str]:
        """'Buy' a free app (adds it to the account's library); returns the delivery token if any.

        Raises ``VersionNotAvailable`` when Play refuses this version code for this account/device.
        """
        from .download import VersionNotAvailable
        resp, w = await self._authed("POST", "purchase", params={"ot": "1", "doc": package, "vc": str(version_code)},
                                     content=b"", extra_headers={"Content-Length": "0"})
        if resp.status_code == 404:
            raise AppNotFound(package, store="google_play")
        err = self._error_text(w)
        if resp.status_code != 200:
            raise VersionNotAvailable(f"purchase {package}@{version_code} -> HTTP {resp.status_code}: {err}")
        buy = w.msg(1).msg(4)
        return buy.str(55) or None

    async def delivery_info(self, package: str, version_code: Optional[int] = None):
        """Exact files (base/split APKs, OBBs, dex metadata) Play serves for ``version_code``.

        ``version_code=None`` means the code this session is currently offered (``details``).
        """
        from .download import VersionNotAvailable, parse_delivery
        if version_code is None:
            d = await self.details(package)
            if not d.version_code:
                raise VersionNotAvailable(f"{package} is not offered to device {self.profile.name}")
            version_code = d.version_code
        dtok = await self.purchase(package, version_code)
        params = {"ot": "1", "doc": package, "vc": str(version_code)}
        if dtok:
            params["dtok"] = dtok
        resp, w = await self._authed("GET", "delivery", params=params)
        data = w.msg(1).msg(21).msg(2)
        if resp.status_code != 200 or not data:
            status = w.msg(1).msg(21).int(1)
            raise VersionNotAvailable(f"delivery {package}@{version_code} -> HTTP {resp.status_code}, "
                                      f"status={status}: {self._error_text(w) or 'no delivery data'}")
        return parse_delivery(data, package, version_code, self.profile.name)

    async def download(self, package: str, dest, version_code: Optional[int] = None, **kw):
        """Resolve + download + verify. Returns ``(DeliveryInfo, [paths])``."""
        from pathlib import Path as _P
        from .download import download_files
        info = await self.delivery_info(package, version_code)
        paths = await download_files(info, _P(dest), self.http, **kw)
        return info, paths

    @property
    def identity(self) -> dict:
        """Who Play thinks we are (for rollout sampling logs). Email is hashed."""
        import hashlib
        s = self.session
        if not s:
            return {}
        return {"account": hashlib.sha256(s.email.encode()).hexdigest()[:12] if s.email else None,
                "gsf_id": f"{s.gsf_id:x}", "device": s.device}


def parse_details_response(data: bytes, device: str = "") -> Optional[PlayApiDetails]:
    """Parse a raw ``fdfe/details`` response body (useful for fixtures/tests)."""
    return _item_to_details(Msg.decode(data).msg(1).msg(2).msg(4), device)

