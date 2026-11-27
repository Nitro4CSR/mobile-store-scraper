"""One-off: try to catch a staged-rollout build of CSR2 and download it.

Writes a summary to stdout (-> tools/logs/task.log) and details to watch/catch_<time>.json.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_store_scraper import HttpClient, StoreScraperError  # noqa: E402
from mobile_store_scraper.play.api import PlayApiClient  # noqa: E402
from mobile_store_scraper.play.download import download_files  # noqa: E402
from mobile_store_scraper.play.rollout import client_from_session_file, probe_version_code, sample_versions  # noqa: E402
from mobile_store_scraper.play.scraper import default_cache_dir  # noqa: E402

PKG = "com.naturalmotion.customstreetracer2"
KNOWN = 6805204  # 6.8.0
OUT = ROOT / "watch"
SESS = OUT / "catch_sessions"
DL = ROOT / "downloads"


def p(*a):
    print(*a, flush=True)


async def main():
    OUT.mkdir(exist_ok=True)
    t0 = time.monotonic()
    res = {"time": datetime.now(timezone.utc).isoformat(), "samples": []}
    http = HttpClient(concurrency=8)
    main_c = PlayApiClient(device="px_9a", http=http,
                           session_file=default_cache_dir() / "play_session_anon_px_9a.json")
    d = await main_c.details(PKG)
    p(f"[main] {d.version} ({d.version_code}) build_date={d.updated_raw} {main_c.identity}")
    res["main"] = {"version": d.version, "code": d.version_code, **main_c.identity}

    found = {}  # code -> (version, session file)

    def take(rep, label):
        s = rep.summary()
        p(f"[{label}] samples={s['samples']} errors={s['errors']} codes={s['codes']} versions={s['versions']}")
        for smp in rep.samples:
            res["samples"].append({"label": label, **smp.__dict__})
            if smp.error:
                p(f"    error: {smp.error[:160]}")
            if smp.version_code and smp.version_code > KNOWN and smp.version_code not in found:
                f = SESS / f"{PKG}_{smp.version_code}_{smp.gsf_id}.json"
                found[smp.version_code] = (smp.version, f)
                p(f"    >>> NEW BUILD {smp.version} ({smp.version_code}) offered to gsf={smp.gsf_id} account={smp.account}")

    # 1) fresh device ids, one account (the main session's)
    take(await sample_versions(PKG, 25, strategy="devices", device="px_9a", http=http,
                               keep_sessions=SESS, base_client=main_c, concurrency=5), "devices x25 px_9a")
    # 2) fresh anonymous accounts
    if not found:
        take(await sample_versions(PKG, 10, strategy="accounts", device="px_9a", http=http,
                                   keep_sessions=SESS, concurrency=3), "accounts x10 px_9a")
    # 3) other device types (rollouts can be per device class)
    if not found:
        for dev in ("sm_s25u", "px_tablet", "sm_a13_5g", "google_kiwi_x86_64", "bravia_vu2"):
            take(await sample_versions(PKG, 4, strategy="accounts", device=dev, http=http,
                                       keep_sessions=SESS, concurrency=2), f"accounts x4 {dev}")
            if found:
                break

    res["found"] = {str(k): v[0] for k, v in found.items()}
    if not found:
        p("\nNo session was offered a build newer than 6.8.0 this time.")
    for code, (version, sess_file) in sorted(found.items()):
        p(f"\n=== {version} ({code})")
        # does asking by number work from a session that was NOT offered it?
        pr = await probe_version_code(main_c, PKG, code)
        p(f"[probe from main session] {pr}")
        res.setdefault("probe", {})[str(code)] = pr
        # download through whichever session works
        for label, c in (("lucky_session", client_from_session_file(sess_file, http)), ("main_by_code", main_c)):
            try:
                info = await c.delivery_info(PKG, code)
                p(f"[{label}] files={len(info.files)} total={info.total_size / 1e6:.1f} MB")
                for f in info.files:
                    p(f"    {f.kind:12} {f.filename:62} {(f.size or 0) / 1e6:8.1f} MB")
                dest = DL / PKG / str(code)
                last = {}

                def prog(name, done, total):
                    pct = int(done * 100 / total) if total else 0
                    if last.get(name) != pct // 25:
                        last[name] = pct // 25
                        p(f"      {name}: {pct}%")

                paths = await download_files(info, dest, c.http, progress=prog)
                p(f"[{label}] SAVED + VERIFIED -> {dest}")
                res.setdefault("download", {})[str(code)] = {"via": label, "dest": str(dest),
                                                              "files": [x.name for x in paths]}
                break
            except StoreScraperError as exc:
                p(f"[{label}] failed: {type(exc).__name__}: {exc}")

    res["seconds"] = round(time.monotonic() - t0, 1)
    path = OUT / f"catch_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    p(f"\nDone in {res['seconds']}s -> {path}")
    await http.aclose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
