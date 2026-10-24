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


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
