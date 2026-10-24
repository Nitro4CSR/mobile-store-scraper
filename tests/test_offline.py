"""Offline tests: parsers run against real responses captured by bench/capture_fixtures.py."""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import httpx
import pytest

from mobile_store_scraper import APP_STORE_COUNTRIES, PLAY_COUNTRIES, AppNotFound, AppStore, GooglePlay, HttpClient, InvalidCountry
from mobile_store_scraper.appstore import parse_lookup_result, parse_web_html
from mobile_store_scraper.countries import normalize_country
from mobile_store_scraper.dates import from_js_date, from_play_api_date, from_timestamp, to_iso
from mobile_store_scraper.models import Store, VersionSource
from mobile_store_scraper.play.api import parse_details_response
from mobile_store_scraper.play.device import available_devices, get_profile
from mobile_store_scraper.play.proto import Fixed64, Msg, encode
from mobile_store_scraper.play.web import parse_details_html

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    p = FIX / name
    if not p.exists() or p.stat().st_size == 0:
        pytest.skip(f"fixture {name} missing - run bench/capture_fixtures.py")
    return p.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- dates
def test_dates_unify_to_utc():
    assert to_iso(from_timestamp(1792803153)) == "2026-10-24T00:52:33Z"
    assert to_iso(from_timestamp(1792803153000)) == "2026-10-24T00:52:33Z"  # ms tolerated
    assert to_iso(from_play_api_date("Oct 23, 2026")) == "2026-10-23T00:00:00Z"
    assert to_iso(from_play_api_date("23 Oct 2026")) == "2026-10-23T00:00:00Z"
    assert to_iso(from_js_date("Thu Oct 22 2026 15:42:36 GMT+0000 (Coordinated Universal Time)")) == "2026-10-22T15:42:36Z"
    assert to_iso(from_js_date("Thu Oct 22 2026 17:42:36 GMT+0200 (CEST)")) == "2026-10-22T15:42:36Z"
    assert from_timestamp(None) is None and from_play_api_date("Varies") is None


# ----------------------------------------------------------------------------- countries
def test_country_lists():
    assert len(PLAY_COUNTRIES) == 225 and len(set(PLAY_COUNTRIES)) == 225
    assert len(APP_STORE_COUNTRIES) == 184 and len(set(APP_STORE_COUNTRIES)) == 184
    assert normalize_country("uk") == "GB"
    assert normalize_country("de", PLAY_COUNTRIES) == "DE"
    with pytest.raises(InvalidCountry):
        normalize_country("ZZ", APP_STORE_COUNTRIES, "App Store")
    with pytest.raises(InvalidCountry):
        normalize_country("GL", APP_STORE_COUNTRIES, "App Store")  # Greenland: no storefront


# ----------------------------------------------------------------------------- protobuf
def test_proto_roundtrip():
    data = encode([(1, "a"), (1, "b"), (2, 300), (3, [(4, True)]), (7, Fixed64(2**63 + 5)), (9, -1)])
    m = Msg.decode(data)
    assert m.strs(1) == ["a", "b"] and m.int(2) == 300 and m.msg(3).bool(4) is True
    assert m.int(7, signed=False) == 2**63 + 5
    assert m.int(9) == -1
    assert not m.msg(99) and m.msg(99).msg(1).str(2) is None  # safe chains


def test_device_profiles_build_checkin():
    assert {"px_9a", "sm_s25u", "px_tablet", "bravia_vu2"} <= set(available_devices())
    p = get_profile("px_9a")
    m = Msg.decode(encode(p.checkin_request("en_US", "UTC")))
    assert m.msg(4).msg(1).str(1).startswith("google/tegu/tegu:15")
    assert m.msg(18).strs(11) == ["arm64-v8a"]
    assert "Android-Finsky/" in p.finsky_user_agent() and "sdk=35" in p.finsky_user_agent()


# ----------------------------------------------------------------------------- Play web
@pytest.mark.parametrize("name", ["play_com.whatsapp_US_en.html", "play_com.whatsapp_DE_de.html",
                                  "play_com.mojang.minecraftpe_US_en.html"])
def test_play_web_fixed_version(name):
    d = parse_details_html(fixture(name))
    assert d["title"] and d["publisher"]
    assert d["version"] and d["version"][0].isdigit()
    assert d["varies_with_device"] is False
    assert d["last_updated"].endswith("Z")
    assert d["min_os_ver"] is None or d["min_os_ver"].startswith("Android ")


def test_play_web_localised_date_same_instant():
    en = parse_details_html(fixture("play_com.whatsapp_US_en.html"))
    de = parse_details_html(fixture("play_com.whatsapp_DE_de.html"))
    assert en["last_updated"] == de["last_updated"]          # unified
    assert en["last_updated_on"] != de["last_updated_on"]    # while the display strings differ


@pytest.mark.parametrize("name", ["play_com.android.chrome_US_en.html", "play_com.instagram.android_JP_ja.html",
                                  "play_com.spotify.music_BR_pt.html", "play_org.telegram.messenger_US_en.html"])
def test_play_web_varies_with_device(name):
    d = parse_details_html(fixture(name))
    assert d["version"] is None and d["varies_with_device"] is True
    assert d["last_updated"] is not None and d["title"]


def test_play_web_paid_app_price():
    d = parse_details_html(fixture("play_com.mojang.minecraftpe_US_en.html"))
    assert d["price"] and d["price"] > 0 and d["currency"] == "USD"


# ----------------------------------------------------------------------------- Play API
@pytest.mark.parametrize("pkg", ["com.whatsapp", "com.android.chrome", "com.instagram.android"])
def test_play_api_details(pkg):
    p = FIX / f"play_api_details_{pkg}.bin"
    if not p.exists() or p.stat().st_size == 0:
        pytest.skip("api fixture missing")
    d = parse_details_response(p.read_bytes(), "px_9a")
    assert d.package == pkg
    assert d.version and d.version[0].isdigit()
    assert d.version_code and d.version_code > 0
    assert d.updated is not None


# ----------------------------------------------------------------------------- Apple
def test_apple_lookup_parse():
    data = json.loads(fixture("apple_lookup_de.json"))
    infos = [parse_lookup_result(r, "DE") for r in data["results"]]
    assert {i.bundle_id for i in infos} >= {"net.whatsapp.WhatsApp", "com.facebook.Facebook"}
    for i in infos:
        assert i.store == Store.APP_STORE.value and i.version and i.last_updated.endswith("Z")
        assert i.last_updated_dt.tzinfo == timezone.utc and i.min_os_ver.startswith("iOS ")
        assert i.to_dict()["last_updated"] == i.last_updated and i.publisher and i.released


@pytest.mark.parametrize("name", ["apple_web_310633997_us.html", "apple_web_284882215_de.html"])
def test_apple_web_parse(name):
    w = parse_web_html(fixture(name))
    assert w["version"] and w["version"][0].isdigit()
    assert w["updated"] is not None and w["updated"].tzinfo is not None


# ----------------------------------------------------------------------------- scrapers w/ mocked transport
def _mock_http(handler) -> HttpClient:
    return HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), retries=1, backoff=0)


async def test_appstore_with_mock_transport():
    lookup = fixture("apple_lookup_de.json")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("country") == "zz":
            return httpx.Response(400, text='{"errorMessage":"Invalid value(s) for key(s): [country]"}')
        if req.url.params.get("id") == "1":
            return httpx.Response(200, text='{"resultCount":0,"results":[]}')
        return httpx.Response(200, text=lookup)

    async with AppStore(http=_mock_http(handler), web_fallback=False) as ap:
        info = await ap.app("id310633997", "de")
        assert info.version_source == VersionSource.ITUNES_LOOKUP.value and info.country == "DE"
        many = await ap.apps(["310633997", "com.facebook.Facebook", "1"], "DE")
        assert not isinstance(many["com.facebook.Facebook"], Exception)
        assert isinstance(many["1"], AppNotFound)
        with pytest.raises(AppNotFound):
            await ap.app("1", "DE")


async def test_googleplay_web_only_and_404():
    html = fixture("play_com.whatsapp_US_en.html")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("id") == "com.whatsapp":
            return httpx.Response(200, text=html)
        return httpx.Response(404)

    async with GooglePlay(http=_mock_http(handler), version_mode="web", session_dir=False) as gp:
        info = await gp.app("com.whatsapp", "us")
        assert info.version and info.version_source == VersionSource.PLAY_WEB.value
        res = await gp.app_all_countries("com.whatsapp", ["US", "DE"])
        assert all(not isinstance(v, Exception) for v in res.values())
        with pytest.raises(AppNotFound):
            await gp.app("nope.nope", "US")
        with pytest.raises(InvalidCountry):
            await gp.app("com.whatsapp", "XX")


async def test_retry_on_429():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, text="ok")

    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), retries=4, backoff=0)
    r = await http.get("https://example.test/")
    assert r.status_code == 200 and calls["n"] == 3 and http.stats["retries"] == 2


async def test_play_retries_degraded_page():
    html = fixture("play_com.whatsapp_US_en.html")
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html>degraded</html>" if calls["n"] == 1 else html)

    async with GooglePlay(http=_mock_http(handler), version_mode="web", session_dir=False) as gp:
        info = await gp.app("com.whatsapp", "MY")
        assert info.version and calls["n"] == 2


async def test_host_cooldown_is_shared_and_rate_limit_spaces_requests():
    import time as _t
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(_t.monotonic())
        return httpx.Response(429) if len(seen) == 1 else httpx.Response(200)

    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), backoff=0.2, rate_limit=50)
    import asyncio as _a
    rs = await _a.gather(*(http.get(f"https://h.test/{i}") for i in range(5)))
    assert all(r.status_code == 200 for r in rs) and http.stats["throttled"] == 1
    # after the 429 every later request waited for the host cooldown (>= backoff)
    assert max(seen[1:]) - seen[0] >= 0.2


async def test_google_sorry_page_fails_fast():
    from mobile_store_scraper import IpBlocked
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if req.url.host == "play.google.com":
            return httpx.Response(302, headers={"Location": "https://www.google.com/sorry/index?continue=x"})
        return httpx.Response(429, text="unusual traffic")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    http = HttpClient(client, backoff=0, block_retries=0)
    with pytest.raises(IpBlocked):
        await http.get("https://play.google.com/store/apps/details?id=x")
    assert calls["n"] == 2  # one request + the redirect, no retry storm

    calls["n"] = 0
    http = HttpClient(client, backoff=0, block_retries=2, block_cooldown=0.05)
    with pytest.raises(IpBlocked):
        await http.get("https://play.google.com/store/apps/details?id=x")
    assert calls["n"] == 6 and http.stats["blocked"] == 3  # paused + retried twice, then gave up
    with pytest.raises(IpBlocked, match="circuit open"):
        await http.get("https://play.google.com/store/apps/details?id=y")
    assert calls["n"] == 6  # circuit breaker: nothing was sent


async def test_block_lifts_during_cooldown():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "play.google.com":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(302, headers={"Location": "https://www.google.com/sorry/index"})
            return httpx.Response(200, text="ok")
        return httpx.Response(429)

    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
                      backoff=0, block_cooldown=0.05)
    r = await http.get("https://play.google.com/x")
    assert r.status_code == 200 and http.stats["blocked"] == 1


async def test_api_fallback_when_web_blocked():
    from datetime import datetime, timezone as tz
    from mobile_store_scraper.play.api import PlayApiDetails

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "play.google.com":
            return httpx.Response(302, headers={"Location": "https://www.google.com/sorry/index"})
        return httpx.Response(429)

    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
                      backoff=0, block_retries=0)
    gp = GooglePlay(http=http, api_fallback_on_block=True, session_dir=False)

    async def fake_api_details(app_id, device=None):
        return PlayApiDetails(app_id, "Chrome", "Google LLC", "155.0.8059.84", 805908404,
                              datetime(2026, 10, 15, tzinfo=tz.utc), "Oct 15, 2026", 37, "10 billion", "px_9a")

    gp.api_details = fake_api_details  # type: ignore[assignment]
    info = await gp.app("com.android.chrome", "DE")
    assert info.version == "155.0.8059.84" and info.version_code == 805908404
    assert info.version_source == VersionSource.PLAY_API.value and info.raw["country_verified"] is False
    assert info.last_updated == "2026-10-15T00:00:00Z" and info.publisher == "Google LLC"
    await gp.aclose()



# ----------------------------------------------------------------------------- new attribute API
def _api_fixture(pkg):
    p = FIX / f"play_api_details_{pkg}.bin"
    if not p.exists():
        pytest.skip("api fixture missing")
    return parse_details_response(p.read_bytes(), "px_9a")


async def test_scrape_csr2_attributes_match_user_example():
    """The example from the spec: com.naturalmotion.customstreetracer2 in DE."""
    import mobile_store_scraper as mss
    html = fixture("play_com.naturalmotion.customstreetracer2_DE_en.html")

    def handler(req):
        return httpx.Response(200, text=html)

    async with mss.Scraper() as s:
        s._gp = GooglePlay(http=_mock_http(handler), session_dir=False)
        app = await s.scrape(store="gp", app="com.naturalmotion.customstreetracer2", country="DE")
    assert isinstance(app, mss.GooglePlayApp)
    assert app.version == "6.8.0"
    assert app.last_updated.startswith("2026-10-14T") and app.last_updated.endswith("Z")
    assert app.min_os_ver == "Android 7.0" and app.min_sdk == 24
    assert app.downloads == 82008679 and app.installs == "50,000,000+" and app.min_installs == 50000000
    assert app.released == "2016-06-29T15:01:05Z"
    assert app.publisher == "Zynga" == app.developer
    # google-play-scraper keys, snake_case
    assert app.genre_id == "GAME_RACING" and app.content_rating == "USK: Ages 12+"
    assert len(app.histogram) == 5 and app.reviews and app.ratings and app.score
    assert app.offers_iap and app.in_app_product_price.startswith("€")
    assert app.developer_email == "android.support@zynga.com" and app.privacy_policy
    assert len(app.screenshots) > 3 and app.icon and app.header_image and app.video
    assert app["version"] == "6.8.0"                       # dict-style access works too
    assert app.to_dict()["publisher"] == "Zynga"
    assert app.last_updated_dt.tzinfo is not None


async def test_varies_with_device_filled_from_api():
    """Chrome's listing says 'Varies with device': version, min OS, size come from the API."""
    html = fixture("play_com.android.chrome_US_en.html")
    api = _api_fixture("com.android.chrome")

    def handler(req):
        return httpx.Response(200, text=html)

    gp = GooglePlay(http=_mock_http(handler), session_dir=False)

    async def fake(app_id, device=None):
        return api

    gp.api_details = fake  # type: ignore[assignment]
    app = await gp.app("com.android.chrome", "US")
    assert app.varies_with_device is True
    assert app.version == "155.0.8059.84" and app.version_code == 805908404
    assert app.version_source == VersionSource.PLAY_API.value
    assert app.min_os_ver == "Android 10"
    assert isinstance(app.downloads, int) and app.downloads > 10_000_000_000
    assert app.target_sdk == 37 and app.download_size and app.version_last_updated == "2026-10-15T00:00:00Z"
    assert app.last_updated == "2026-10-22T22:01:41Z"   # store's "Updated on" stays the listing value
    await gp.aclose()


def test_api_details_parse_csr2():
    d = _api_fixture("com.naturalmotion.customstreetracer2")
    assert (d.version, d.version_code, d.min_os_ver, d.real_installs) == ("6.8.0", 6805204, "Android 7.0", 82008679)
    assert to_iso(d.released) == "2016-06-29T15:01:05Z" and d.developer == "Zynga"
    assert "<br" not in (d.developer_address or "")


def test_store_aliases_and_store_specific_fields():
    import mobile_store_scraper as mss
    for a in ("gp", "GP", "google_play", "play", "android", "google-play"):
        assert mss.resolve_store(a) is Store.GOOGLE_PLAY
    for a in ("as", "apple", "app_store", "ios", "itunes", "App Store"):
        assert mss.resolve_store(a) is Store.APP_STORE
    with pytest.raises(ValueError):
        mss.resolve_store("amazon")
    a = mss.AppStoreApp(app_id="1", country="US")
    assert not hasattr(a, "downloads") and hasattr(mss.GooglePlayApp(app_id="x", country="US"), "downloads")


async def test_sync_scrape_refuses_inside_event_loop():
    import mobile_store_scraper as mss
    with pytest.raises(RuntimeError, match="scrape_async"):
        mss.scrape("gp", "com.whatsapp", "US")


def test_default_pace_is_unlimited_and_user_pace_applies():
    h = HttpClient()
    assert h.concurrency is None and h.rate_limit is None
    gp = GooglePlay(session_dir=False)
    assert gp.http.concurrency is None and gp.http.rate_limit is None
    gp2 = GooglePlay(concurrency=5, rate_limit=2.5, session_dir=False)
    assert gp2.http.concurrency == 5 and gp2.http.rate_limit == 2.5
    ap = AppStore(concurrency=3, rate_limit=1)
    assert ap.http.concurrency == 3 and ap.http.rate_limit == 1



async def test_fixed_version_but_varies_min_os_uses_api():
    """WhatsApp: listing has a version but 'Requires Android: Varies with device'."""
    html = fixture("play_com.whatsapp_US_en.html")
    api = _api_fixture("com.whatsapp")

    def handler(req):
        return httpx.Response(200, text=html)

    gp = GooglePlay(http=_mock_http(handler), session_dir=False)

    async def fake(app_id, device=None):
        return api

    gp.api_details = fake  # type: ignore[assignment]
    app = await gp.app("com.whatsapp", "US")
    assert app.version_source == VersionSource.PLAY_WEB.value  # listing version kept
    assert app.version_code == api.version_code                 # matched build -> code added
    assert app.min_os_ver == "Android 6.0"
    await gp.aclose()



def test_comments_found_by_shape():
    from mobile_store_scraper.play.web import _find_comments
    review = ["id-1", ["Alice", None], 4, None, "Great game, love the cars", [1791961838, 0]]
    ds = {"ds:5": [], "ds:10": [[review, list(review)]], "ds:8": [[["not", "a", "review"]]]}
    assert _find_comments(ds) == ["Great game, love the cars"] * 2


# ----------------------------------------------------------------------------- downloads / rollout
def test_parse_delivery_and_digest():
    import base64, hashlib
    from mobile_store_scraper.play.download import _digest_matches, parse_delivery
    data = encode([
        (1, 1000), (2, "c2hhMQ"), (3, "https://dl/base"), (19, "c2hhMjU2"),
        (15, [(1, "config.arm64_v8a"), (2, 200), (4, "x"), (5, "https://dl/split1")]),
        (15, [(1, "config.xxhdpi"), (2, 50), (5, "https://dl/split2")]),
        (4, [(1, 0), (2, 6805204), (3, 999), (4, "https://dl/obb")]),
        (21, [(1, 10), (2, "dm"), (3, "https://dl/dm")]),
        (5, [(1, "MarketDA"), (2, "abc")]),
    ])
    info = parse_delivery(Msg.decode(data), "com.x", 6805204, "px_9a")
    kinds = [f.kind for f in info.files]
    assert kinds == ["base", "split", "split", "obb_main", "dex_metadata"]
    assert info.files[1].filename == "com.x.config.arm64_v8a.apk"
    assert info.files[3].filename == "main.6805204.com.x.obb"
    assert info.total_size == 1000 + 200 + 50 + 999 + 10 and info.cookies == {"MarketDA": "abc"}
    assert "url" not in info.to_dict()["files"][0]          # signed URLs are never persisted
    d = hashlib.sha1(b"hello").digest()
    assert _digest_matches(base64.urlsafe_b64encode(d).decode().rstrip("="), d) is True
    assert _digest_matches(d.hex(), d) is True
    assert _digest_matches("nope", d) is False and _digest_matches(None, d) is None


async def test_download_files_verifies(tmp_path):
    import base64, hashlib
    from mobile_store_scraper.play.download import DeliveryFile, DeliveryInfo, download_files
    body = b"apk-bytes" * 100
    good = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")

    def handler(req):
        return httpx.Response(200, content=body)

    http = _mock_http(handler)
    info = DeliveryInfo("com.x", 1, "px_9a", [DeliveryFile("base", "base", "com.x.apk", "https://dl/a", len(body), None, good)])
    paths = await download_files(info, tmp_path, http)
    assert paths[0].read_bytes() == body and (tmp_path / "delivery.json").exists()
    bad = DeliveryInfo("com.x", 1, "px_9a", [DeliveryFile("base", "base", "b.apk", "https://dl/b", len(body), None, "zzz")])
    with pytest.raises(Exception, match="sha256 mismatch"):
        await download_files(bad, tmp_path, http)
    assert not (tmp_path / "b.apk").exists() and not (tmp_path / "b.apk.part").exists()


def test_watcher_remember_and_state(tmp_path):
    from mobile_store_scraper.play.rollout import RolloutWatcher
    w = RolloutWatcher("com.x", tmp_path)
    st = w._load()
    assert w._remember(st, 100, "1.0", "main") is True
    assert w._remember(st, 100, "1.0", "sample:devices") is False
    assert st["known"]["100"]["sources"] == ["main", "sample:devices"]
    w._save(st)
    w._log({"a": 1})
    assert w._load()["known"]["100"]["version"] == "1.0" and w.log_path.read_text().strip() == '{"a": 1}'


async def test_watcher_check_logs_new_build(tmp_path, monkeypatch):
    """Full check() with the network mocked: main sees old build, a sampled device sees the new one."""
    from mobile_store_scraper.play import rollout
    from mobile_store_scraper.play.api import PlayApiClient, PlayApiDetails
    from mobile_store_scraper.play.scraper import GooglePlay

    def det(v, code):
        return PlayApiDetails("com.x", "X", "Dev", v, code, None, "Oct 14, 2026", 36, "1M", "px_9a")

    async def fake_app(self, app_id, country="US", **kw):
        raise mss_err("web blocked")

    from mobile_store_scraper.errors import IpBlocked as mss_err
    monkeypatch.setattr(GooglePlay, "app", fake_app)

    async def fake_details(self, package):
        return det("6.8.0", 6805204)

    monkeypatch.setattr(PlayApiClient, "details", fake_details)

    async def fake_login(self, force=False):
        return None

    monkeypatch.setattr(PlayApiClient, "login", fake_login)

    async def fake_sample(package, n, **kw):
        rep = rollout.SampleReport(package, "devices")
        rep.samples = [rollout.Sample("devices", "px_9a", "acc", "g1", "6.8.0", 6805204),
                       rollout.Sample("devices", "px_9a", "acc", "g2", "6.9.0", 6905101)]
        return rep

    monkeypatch.setattr(rollout, "sample_versions", fake_sample)

    async def fake_probe(client, package, code):
        return {"version_code": code, "available": True, "files": 3, "total_size": 1}

    monkeypatch.setattr(rollout, "probe_version_code", fake_probe)
    w = rollout.RolloutWatcher("com.x", tmp_path, always_sample=True, samples=2)
    e = await w.check()
    assert e["new_builds"] == ["6805204", "6905101"] and e["newest_known"] == 6905101
    assert e["probe"]["available"] is True and "error" in e["web"]
    rows = w.report()
    assert [r["version_code"] for r in rows] == [6805204, 6905101]
    assert rows[1]["first_source"] == "sample:devices" and "probe_by_code" in rows[1]["sources"]
    csv = w.obs_path.read_text().splitlines()
    assert csv[0].startswith("time_utc,source") and any("6905101" in line for line in csv)
    e2 = await w.check()
    assert e2["new_builds"] == []



async def test_network_failure_becomes_networkerror():
    from mobile_store_scraper import NetworkError

    def handler(req):
        raise httpx.ConnectError("no route")

    http = HttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), retries=1, backoff=0)
    with pytest.raises(NetworkError, match="ConnectError"):
        await http.get("https://play.google.com/x")
