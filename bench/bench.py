"""Live benchmark / acceptance test for mobile_store_scraper. Hits the real stores from this machine.

    python bench/bench.py                       # full run (Play + Apple, all countries)
    python bench/bench.py --quick               # a few countries only
    python bench/bench.py --validate-countries  # re-probe which country codes each store accepts
    python bench/bench.py --only play|apple

Writes a human summary to stdout and the full results to bench/results/<timestamp>.json.
Exit code is non-zero when an acceptance check fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mobile_store_scraper as mss  # noqa: E402
from mobile_store_scraper import (APP_STORE_COUNTRIES, PLAY_COUNTRIES, AppNotFound, AppStore,  # noqa: E402
                                  GooglePlay, HttpClient, StoreScraperError, VersionSource)

# Apps whose web listing shows a plain version, and apps that are "Varies with device".
PLAY_FIXED = ["com.whatsapp", "com.mojang.minecraftpe", "com.discord", "org.mozilla.firefox", "com.duolingo"]
PLAY_VARIES = ["com.android.chrome", "com.google.android.youtube", "com.instagram.android", "com.facebook.katana",
               "com.spotify.music", "com.zhiliaoapp.musically", "com.google.android.gm", "com.netflix.mediaclient",
               "com.google.android.apps.maps", "org.telegram.messenger"]
APPLE_APPS = ["310633997", "284882215", "net.whatsapp.WhatsApp", "389801252", "324684580", "835599320"]
SESSION_DIR = ROOT / "bench" / ".sessions"
PACE: dict = {"concurrency": None, "rate_limit": None}   # set from --concurrency / --rate-limit

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def fmt(info) -> str:
    code = getattr(info, "version_code", None)
    return (f"{info.app_id:32} v={str(info.version):24} code={str(code):12} src={str(info.version_source):13} "
            f"updated={info.last_updated} min_os={info.min_os_ver} dl={getattr(info, 'downloads', '-')}")


async def bench_play(quick: bool, out: dict) -> None:
    print("\n=== Spec example: mss.scrape(store='gp', app='com.naturalmotion.customstreetracer2', country='DE') ===")
    t = time.perf_counter()
    app = await mss.scrape_async(store="gp", app="com.naturalmotion.customstreetracer2", country="DE",
                                 session_dir=SESSION_DIR, **PACE)
    print(f"  ({time.perf_counter() - t:.2f}s)  {app!r}")
    for k in ("version", "last_updated", "min_os_ver", "downloads", "released", "publisher", "installs",
              "version_source", "varies_with_device", "score", "ratings", "reviews", "genre", "content_rating"):
        print(f"  app.{k:20} = {getattr(app, k)!r}")
    out["spec_example"] = app.to_dict()
    check(bool(app.version and app.last_updated and app.min_os_ver and app.downloads and app.released
               and app.publisher), "spec example: all requested attributes present")
    check(app.publisher == "Zynga" and app.min_os_ver.startswith("Android"), "spec example: publisher Zynga, Android min OS")

    print("\n=== Google Play ===")
    async with GooglePlay(session_dir=SESSION_DIR, **PACE) as gp:
        # 1) auto mode, US -----------------------------------------------------------
        t = time.perf_counter()
        res = await gp.apps(PLAY_FIXED + PLAY_VARIES, "US")
        dt = time.perf_counter() - t
        print(f"[auto/US] {len(res)} apps in {dt:.1f}s (concurrent, incl. API login when needed)")
        out["play_auto_us"] = {}
        for app_id, info in res.items():
            if isinstance(info, Exception):
                print(f"  {app_id:32} ERROR {type(info).__name__}: {info}")
                out["play_auto_us"][app_id] = {"error": str(info)}
                continue
            print("  " + fmt(info) + ("  [varies]" if info.varies_with_device else "") +
                  (f"  api_error={info.raw.get('api_error')}" if info.raw.get("api_error") else ""))
            out["play_auto_us"][app_id] = info.to_dict()
        ok = [i for i in res.values() if not isinstance(i, Exception)]
        check(len(ok) == len(res), "every Play app fetched")
        check(all(i.version for i in ok), "every Play app has a version (incl. 'Varies with device' apps)")
        check(all(i.last_updated and i.last_updated.endswith("Z") for i in ok), "every Play app has a UTC last_updated")
        check(all(i.min_os_ver and i.min_os_ver.startswith("Android") for i in ok), "every Play app has min_os_ver")
        check(all(i.downloads for i in ok), "every Play app has downloads")
        check(all(i.publisher for i in ok), "every Play app has publisher")
        no_rel = [i.app_id for i in ok if not i.released]
        print(f"  info: released date not published by the store for: {no_rel or 'none'}")
        varies = [i for i in ok if i.varies_with_device]
        check(len(varies) >= 3, f"varies-with-device apps detected ({len(varies)})")
        check(all(i.version_source == VersionSource.PLAY_API.value for i in varies), "varies apps resolved through Play API")

        # 2) web vs API agreement on fixed-version apps ---------------------------------
        print("[cross-check] web version vs Android API version for single-version apps")
        agree = compared = 0
        for app_id in PLAY_FIXED:
            info = res.get(app_id)
            if isinstance(info, Exception) or not info:
                continue
            try:
                api = await gp.api_details(app_id)
            except StoreScraperError as exc:
                print(f"  {app_id:32} API error {exc}")
                continue
            web_v = info.raw["web"]["version"]
            if web_v is None:
                print(f"  {app_id:32} web=<varies with device>  api={api.version} ({api.version_code})  (skipped)")
                continue
            compared += 1
            same = api.version == web_v
            agree += same
            print(f"  {app_id:32} web={web_v:20} api={api.version} ({api.version_code}) {'==' if same else '!='}")
        check(compared >= 3 and agree == compared, f"web and API agree on {agree}/{compared} single-version apps")

        # 2b) bulkDetails: many packages in one API request --------------------------------
        t = time.perf_counter()
        bulk = await gp.api().bulk_details(PLAY_VARIES + ["com.does.not.exist.zzz"])
        got = {k: v for k, v in bulk.items() if v is not None}
        print(f"[bulkDetails] {len(bulk)} packages in one request, {time.perf_counter() - t:.2f}s -> "
              f"{len(got)} resolved; missing={[k for k, v in bulk.items() if v is None]}")
        for k, v in list(got.items())[:4]:
            print(f"  {k:32} {v.version} ({v.version_code})")
        check(len(got) >= len(PLAY_VARIES) - 1 and bulk["com.does.not.exist.zzz"] is None,
              "bulkDetails resolves real packages and maps unknown ones to None")
        out["play_bulk"] = {k: (v.version if v else None) for k, v in bulk.items()}

        # 3) all devices ------------------------------------------------------------------
        print("[all_devices] per-device builds")
        out["play_all_devices"] = {}
        for app_id in PLAY_VARIES[:3]:
            t = time.perf_counter()
            dvs = await gp.versions_by_device(app_id)
            print(f"  ({time.perf_counter() - t:.1f}s)")
            out["play_all_devices"][app_id] = [d.__dict__ for d in dvs]
            print(f"  {app_id}")
            for d in dvs:
                print(f"     {d.device:20} sdk={str(d.sdk):4} {d.version or '-':28} {d.version_code or '':>12} {d.error or ''}")
            check(sum(d.version is not None for d in dvs) >= 3, f"{app_id}: >=3 device profiles resolved")

        # 4) all countries --------------------------------------------------------------
        countries = ["US", "DE", "JP", "BR", "IN", "KR", "GB", "FR"] if quick else list(PLAY_COUNTRIES)
        for app_id in ("com.whatsapp", "com.android.chrome"):
            t = time.perf_counter()
            allc = await gp.app_all_countries(app_id, countries)
            dt = time.perf_counter() - t
            found = {c: i for c, i in allc.items() if not isinstance(i, Exception)}
            missing = [c for c, i in allc.items() if isinstance(i, AppNotFound)]
            errors = {c: f"{type(i).__name__}: {i}" for c, i in allc.items()
                      if isinstance(i, Exception) and not isinstance(i, AppNotFound)}
            versions = Counter(i.version for i in found.values())
            updated = Counter(i.last_updated for i in found.values())
            print(f"[all countries] {app_id}: {len(countries)} countries in {dt:.1f}s -> available={len(found)} "
                  f"not_listed={len(missing)} errors={len(errors)}")
            print(f"  versions={dict(versions)}  updated={dict(updated.most_common(3))}")
            if missing:
                print(f"  not listed in: {' '.join(sorted(missing))}")
            for c, e in list(errors.items())[:10]:
                print(f"  error {c}: {e}")
            out[f"play_all_countries_{app_id}"] = {
                "seconds": dt, "available": len(found), "not_listed": missing, "errors": errors,
                "versions": dict(versions), "updated": dict(updated),
            }
            check(not errors, f"{app_id}: no errors across {len(countries)} Play countries")
            check(all(i.version for i in found.values()), f"{app_id}: version present in every country")
        print(f"[http] web requests={gp.http.stats}  api requests={gp._api_http.stats}")


async def bench_apple(quick: bool, out: dict) -> None:
    print("\n=== Apple App Store ===")
    async with AppStore(**PACE) as ap:
        t = time.perf_counter()
        res = await ap.apps(APPLE_APPS, "US")
        print(f"[batch lookup/US] {len(res)} apps in {time.perf_counter() - t:.2f}s (one request)")
        out["apple_us"] = {}
        for k, info in res.items():
            if isinstance(info, Exception):
                print(f"  {k:32} ERROR {info}")
                continue
            print("  " + fmt(info) + f"  {info.title}")
            out["apple_us"][k] = info.to_dict()
        ok = [i for i in res.values() if not isinstance(i, Exception)]
        check(len(ok) == len(APPLE_APPS), "every Apple app fetched (numeric ids and bundle ids)")
        check(all(i.version and i.last_updated and i.min_os_ver and i.publisher and i.released for i in ok),
              "every Apple app has version, last_updated, min_os_ver, publisher, released")

        print("[verify_with_web] lookup vs apps.apple.com")
        for app_id in APPLE_APPS[:3]:
            info = await ap.app(app_id, "US", verify_with_web=True)
            w = info.raw.get("web") or {}
            print(f"  {app_id:24} lookup={info.raw['lookup'].get('version'):12} web={w.get('version')}  "
                  f"web_date={w.get('updated')}  chosen={info.version} ({info.version_source})")
            check(w.get("version") is not None, f"{app_id}: web fallback parser finds a version")

        countries = ["US", "DE", "JP", "BR", "IN", "CN", "GB", "FR"] if quick else list(APP_STORE_COUNTRIES)
        for app_id in ("310633997", "284882215"):
            t = time.perf_counter()
            allc = await ap.app_all_countries(app_id, countries)
            dt = time.perf_counter() - t
            found = {c: i for c, i in allc.items() if not isinstance(i, Exception)}
            missing = [c for c, i in allc.items() if isinstance(i, AppNotFound)]
            errors = {c: f"{type(i).__name__}: {i}" for c, i in allc.items()
                      if isinstance(i, Exception) and not isinstance(i, AppNotFound)}
            versions = Counter(i.version for i in found.values())
            print(f"[all countries] {app_id}: {len(countries)} storefronts in {dt:.1f}s -> available={len(found)} "
                  f"not_listed={len(missing)} errors={len(errors)} versions={dict(versions)}")
            if missing:
                print(f"  not listed in: {' '.join(sorted(missing))}")
            for c, e in list(errors.items())[:10]:
                print(f"  error {c}: {e}")
            out[f"apple_all_countries_{app_id}"] = {"seconds": dt, "available": len(found), "not_listed": missing,
                                                    "errors": errors, "versions": dict(versions)}
            check(not errors, f"{app_id}: no errors across {len(countries)} App Store storefronts")
        print(f"[http] {ap.http.stats}")


ISO = ("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY "
       "BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM "
       "FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT "
       "JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN "
       "MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT "
       "PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL "
       "TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW").split()


async def validate_countries(out: dict) -> None:
    print("\n=== Country validation ===")
    async with HttpClient(concurrency=8) as http:
        async def probe(c: str):
            r = await http.get("https://itunes.apple.com/lookup", params={"id": "310633997", "country": c.lower()})
            return c, r.status_code
        apple = dict(await asyncio.gather(*(probe(c) for c in ISO)))
        valid = sorted(c for c, s in apple.items() if s == 200)
        print(f"Apple accepts {len(valid)} codes")
        added = sorted(set(valid) - set(APP_STORE_COUNTRIES))
        removed = sorted(set(APP_STORE_COUNTRIES) - set(valid))
        print(f"  new vs package list: {added}  dropped: {removed}")
        check(not added and not removed, "APP_STORE_COUNTRIES matches Apple's live storefronts")

        async def play(c: str):
            r = await http.get("https://play.google.com/store/apps/details",
                               params={"id": "com.google.android.youtube", "hl": "en", "gl": c})
            return c, r.status_code
        p = dict(await asyncio.gather(*(play(c) for c in PLAY_COUNTRIES)))
        bad = {c: s for c, s in p.items() if s != 200}
        print(f"Play: YouTube listing answered 200 in {len(p) - len(bad)}/{len(p)} PLAY_COUNTRIES; non-200: {bad}")
        out["country_validation"] = {"apple_valid": valid, "apple_added": added, "apple_removed": removed, "play_non200": bad}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", choices=["play", "apple"])
    ap.add_argument("--validate-countries", action="store_true")
    ap.add_argument("--concurrency", type=int, default=None, help="max simultaneous requests (default unlimited)")
    ap.add_argument("--rate-limit", type=float, default=None, help="requests/second per host (default unlimited)")
    a = ap.parse_args()
    PACE.update(concurrency=a.concurrency, rate_limit=a.rate_limit)
    print(f"pace: concurrency={a.concurrency or 'unlimited'} rate_limit={a.rate_limit or 'unlimited'}")
    out: dict = {"started": datetime.now(timezone.utc).isoformat(), "python": sys.version}
    t0 = time.perf_counter()
    if a.validate_countries:
        await validate_countries(out)
    else:
        if a.only in (None, "play"):
            await bench_play(a.quick, out)
        if a.only in (None, "apple"):
            await bench_apple(a.quick, out)
    out["total_seconds"] = time.perf_counter() - t0
    out["failures"] = failures
    res_dir = ROOT / "bench" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    path = res_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"\nTotal {out['total_seconds']:.1f}s. {len(failures)} failed checks. Results: {path}")
    for f in failures:
        print("  FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
