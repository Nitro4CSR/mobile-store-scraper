"""Download a rollout build through the saved sessions that were offered it.

Each saved session (watch/catch_sessions/<pkg>_<code>_<gsf>.json) gets ONE quick attempt
(no long 429 back-off); on a rate limit we move straight on to the next session.
Usage: python tools/download_new_build.py [version_code] [--info]
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_store_scraper import HttpClient, StoreScraperError  # noqa: E402
from mobile_store_scraper.play.download import download_files  # noqa: E402
from mobile_store_scraper.play.rollout import client_from_session_file  # noqa: E402

PKG = "com.naturalmotion.customstreetracer2"


def p(*a):
    print(*a, flush=True)


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    info_only = "--info" in sys.argv
    code = int(args[0]) if args else 6905206
    sessions = sorted((ROOT / "watch" / "catch_sessions").glob(f"{PKG}_{code}_*.json"))
    p(f"{len(sessions)} saved sessions were offered {code}")
    quick = HttpClient(concurrency=4, retries=1, backoff=2.0, block_retries=0)  # fail fast on 429
    big = HttpClient(concurrency=2, retries=4, backoff=2.0)                    # for the file transfers
    for i, sf in enumerate(sessions, 1):
        c = client_from_session_file(sf, quick)
        try:
            d = await c.details(PKG)
            p(f"[{i}] {sf.stem[-16:]} offered {d.version} ({d.version_code})")
            info = await c.delivery_info(PKG, code)
        except StoreScraperError as exc:
            p(f"[{i}] {sf.stem[-16:]} failed: {type(exc).__name__}: {str(exc)[:160]}")
            await asyncio.sleep(3)
            continue
        p(f"[{i}] delivery OK: files={len(info.files)} total={info.total_size / 1e6:.1f} MB")
        for f in info.files:
            p(f"    {f.kind:12} {f.filename:62} {(f.size or 0) / 1e6:8.1f} MB")
        if info_only:
            break
        dest = ROOT / "downloads" / PKG / str(code)
        last = {}

        def prog(name, done, total):
            pct = int(done * 100 / total) if total else 0
            if last.get(name) != pct // 10:
                last[name] = pct // 10
                p(f"      {name}: {pct}%")

        paths = await download_files(info, dest, big, progress=prog)
        p(f"SAVED + VERIFIED ({len(paths)} files) -> {dest}")
        break
    else:
        p("No session could get a delivery right now (rate limited). Try again later.")
    await quick.aclose()
    await big.aclose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
