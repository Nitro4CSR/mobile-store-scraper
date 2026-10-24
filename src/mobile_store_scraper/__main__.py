"""Command line.

    python -m mobile_store_scraper gp com.naturalmotion.customstreetracer2 -c DE
    python -m mobile_store_scraper as 310633997 -c all --concurrency 20 --rate-limit 10
    python -m mobile_store_scraper gp com.google.android.youtube -m all_devices --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .errors import StoreScraperError
from .scrape import Scraper

SHOW = ("version", "last_updated", "min_os_ver", "downloads", "released", "publisher")


def _row(app) -> str:
    return (f"{app.country:3} {str(app.version):22} {str(app.last_updated):21} {str(app.min_os_ver):13} "
            f"{str(getattr(app, 'downloads', '-')):>13} {str(app.released):21} {app.publisher or ''}")


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mobile-store-scraper")
    ap.add_argument("store", help="gp / google_play / play  or  as / apple / app_store / ios")
    ap.add_argument("app", help="package name (Play) or track id / bundle id (App Store)")
    ap.add_argument("-c", "--country", default="US", help="ISO code, comma list, or 'all'")
    ap.add_argument("-l", "--lang", default=None)
    ap.add_argument("-m", "--mode", default="auto", choices=["auto", "web", "api", "all_devices"],
                    help="Google Play version mode")
    ap.add_argument("--concurrency", type=int, default=None, help="max simultaneous requests (default: unlimited)")
    ap.add_argument("--rate-limit", type=float, default=None, help="requests/second per host (default: unlimited)")
    ap.add_argument("--json", action="store_true", help="print every attribute as JSON")
    a = ap.parse_args(argv)

    async with Scraper(concurrency=a.concurrency, rate_limit=a.rate_limit, version_mode=a.mode, lang=a.lang) as s:
        if a.country.lower() != "all" and "," not in a.country:
            try:
                res = {a.country.upper(): await s.scrape(a.store, a.app, a.country)}
            except StoreScraperError as exc:
                res = {a.country.upper(): exc}
        else:
            res = await s.scrape_countries(a.store, a.app, a.country)
    if a.json:
        print(json.dumps({k: (v.to_dict() if not isinstance(v, Exception) else {"error": f"{type(v).__name__}: {v}"})
                          for k, v in res.items()}, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"{'cc':3} {'version':22} {'last_updated':21} {'min_os_ver':13} {'downloads':>13} {'released':21} publisher")
        for k, v in res.items():
            print(_row(v) if not isinstance(v, Exception) else f"{k:3} {type(v).__name__}: {v}")
    return 0


async def _tools(argv: list[str]) -> int:
    """download / sample / watch (Google Play Android API)."""
    from pathlib import Path

    from .play.api import PlayApiClient
    from .play.rollout import RolloutWatcher, sample_versions
    from .play.scraper import default_cache_dir

    ap = argparse.ArgumentParser(prog="mobile-store-scraper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="download the exact APK build(s) Play serves")
    d.add_argument("package")
    d.add_argument("--vc", type=int, default=None, help="explicit version code (default: what this device is offered)")
    d.add_argument("--device", default="px_9a")
    d.add_argument("--dest", default="downloads")
    d.add_argument("--info", action="store_true", help="only list files/sizes, don't download")
    s = sub.add_parser("sample", help="ask N independent sessions which build they are offered")
    s.add_argument("package")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--strategy", choices=["devices", "accounts"], default="devices")
    s.add_argument("--device", default="px_9a")
    w = sub.add_parser("watch", help="one rollout-watch check (run it from a scheduler)")
    w.add_argument("package")
    w.add_argument("--state-dir", default=str(default_cache_dir() / "watch"))
    w.add_argument("--download-dir", default=None)
    w.add_argument("--country", default="US")
    w.add_argument("--samples", type=int, default=8)
    w.add_argument("--strategy", choices=["devices", "accounts"], default="devices")
    a = ap.parse_args(argv)

    if a.cmd == "download":
        sf = default_cache_dir() / f"play_session_anon_{a.device}.json"
        async with PlayApiClient(device=a.device, session_file=sf) as c:
            info = await c.delivery_info(a.package, a.vc)
            print(f"{info.package} versionCode={info.version_code} device={info.device} "
                  f"files={len(info.files)} total={info.total_size / 1e6:.1f} MB")
            for f in info.files:
                print(f"  {f.kind:13} {f.filename:60} {(f.size or 0) / 1e6:9.1f} MB")
            if not a.info:
                from .play.download import download_files
                last = {}

                def prog(name, done, total):
                    pct = int(done * 100 / total) if total else 0
                    if last.get(name) != pct // 10:
                        last[name] = pct // 10
                        print(f"    {name}: {pct}%", flush=True)

                paths = await download_files(info, Path(a.dest) / a.package / str(info.version_code), c.http,
                                             progress=prog)
                print("saved + verified:", *[str(p) for p in paths], sep="\n  ")
    elif a.cmd == "sample":
        rep = await sample_versions(a.package, a.n, strategy=a.strategy, device=a.device)
        print(json.dumps(rep.summary(), indent=2))
        for smp in rep.samples:
            print(f"  account={smp.account} gsf={smp.gsf_id} -> {smp.version} ({smp.version_code}) {smp.error or ''}")
    else:
        entry = await RolloutWatcher(a.package, a.state_dir, country=a.country, samples=a.samples,
                                     strategy=a.strategy, download_dir=a.download_dir).check()
        print(json.dumps(entry, indent=2, default=str))
    return 0


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    argv = sys.argv[1:]
    if argv and argv[0] in ("download", "sample", "watch"):
        sys.exit(asyncio.run(_tools(argv)))
    sys.exit(asyncio.run(_main(argv)))


if __name__ == "__main__":
    main()
