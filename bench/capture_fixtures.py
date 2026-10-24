"""Capture real store responses into tests/fixtures so the parsers can be tested offline.

    python bench/capture_fixtures.py

Saves only the parts the parsers read (Play ds:5 block, Apple serialized-server-data),
plus raw Play Android API ``details`` protobuf bodies.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_store_scraper import HttpClient  # noqa: E402
from mobile_store_scraper.play.api import FDFE, PlayApiClient  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
PLAY = [("com.whatsapp", "US", "en"), ("com.whatsapp", "DE", "de"), ("com.android.chrome", "US", "en"),
        ("com.instagram.android", "JP", "ja"), ("com.spotify.music", "BR", "pt"), ("org.telegram.messenger", "US", "en"),
        ("com.mojang.minecraftpe", "US", "en"), ("com.naturalmotion.customstreetracer2", "DE", "en")]
API = ["com.whatsapp", "com.android.chrome", "com.instagram.android", "com.naturalmotion.customstreetracer2"]
APPLE_WEB = [("310633997", "us"), ("284882215", "de")]


async def main() -> None:
    FIX.mkdir(parents=True, exist_ok=True)
    async with HttpClient(concurrency=4) as http:
        for app, gl, hl in PLAY:
            r = await http.get("https://play.google.com/store/apps/details", params={"id": app, "hl": hl, "gl": gl})
            blocks = re.findall(r"<script[^>]*>AF_initDataCallback\(.*?</script>", r.text, re.S)
            keep = blocks
            name = f"play_{app}_{gl}_{hl}.html"
            (FIX / name).write_text("\n".join(keep), encoding="utf-8")
            print("saved", name, r.status_code, sum(map(len, keep)))
        for tid, cc in APPLE_WEB:
            r = await http.get(f"https://apps.apple.com/{cc}/app/id{tid}")
            m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>.*?</script>', r.text, re.S)
            name = f"apple_web_{tid}_{cc}.html"
            (FIX / name).write_text(m.group(0) if m else "", encoding="utf-8")
            print("saved", name, r.status_code)
        r = await http.get("https://itunes.apple.com/lookup", params={"id": "310633997,284882215", "country": "de"})
        (FIX / "apple_lookup_de.json").write_text(r.text, encoding="utf-8")
        print("saved apple_lookup_de.json", r.status_code)

    async with PlayApiClient(session_file=ROOT / "bench" / ".sessions" / "play_session_anon_px_9a.json") as api:
        s = await api.login()
        for pkg in API:
            resp, _ = await api._fdfe(s, "GET", "details", params={"doc": pkg})
            (FIX / f"play_api_details_{pkg}.bin").write_bytes(resp.content)
            print("saved api", pkg, resp.status_code, len(resp.content))
    print("done ->", FIX, FDFE)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
