"""Catch Google Play staged rollouts early.

During a staged rollout Play decides per account/device who gets the new build, so a single
session usually still sees the old one. Three tools:

* :func:`sample_versions` - ask Play from many independent sessions and tally what each sees.
  ``strategy="accounts"``: a fresh anonymous dispenser account per sample.
  ``strategy="devices"``: one account, a fresh device check-in (new GSF id) per sample.
* :func:`probe_version_code` - ask Play to deliver a *specific* version code to one session.
  If Play serves rollout builds to anyone who asks by code, this needs no luck at all.
* :class:`RolloutWatcher` - stateful hourly check: notices "Updated on" moving, samples when a
  rollout looks active, probes the newest code it knows, optionally downloads new builds, and
  appends everything to a JSONL log so we learn which technique works for a given app.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from ..errors import StoreScraperError
from ..http import HttpClient
from .api import PlayApiClient
from .device import DEFAULT_DEVICE


@dataclass
class Sample:
    strategy: str
    device: str
    account: Optional[str]
    gsf_id: Optional[str]
    version: Optional[str] = None
    version_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class SampleReport:
    package: str
    strategy: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def codes(self) -> Counter:
        return Counter(s.version_code for s in self.samples if s.version_code)

    @property
    def newest(self) -> Optional[Sample]:
        ok = [s for s in self.samples if s.version_code]
        return max(ok, key=lambda s: s.version_code) if ok else None

    def summary(self) -> dict[str, Any]:
        versions: dict[str, str] = {}
        for s in self.samples:
            if s.version_code:
                versions[str(s.version_code)] = s.version or ""
        return {"strategy": self.strategy, "samples": len(self.samples),
                "errors": sum(1 for s in self.samples if s.error),
                "codes": {str(k): v for k, v in sorted(self.codes.items())}, "versions": versions}


async def sample_versions(
    package: str,
    n: int = 10,
    *,
    strategy: str = "devices",
    device: str = DEFAULT_DEVICE,
    concurrency: int = 3,
    http: Optional[HttpClient] = None,
    dispenser_url: Optional[str] = None,
    keep_sessions: Optional[Path] = None,
    base_client: Optional[PlayApiClient] = None,
) -> SampleReport:
    """Ask ``n`` independent Play sessions which build of ``package`` they are offered.

    ``keep_sessions``: directory where every sampled session is saved, so a session that saw a
    new build can be reused later to download it (see :func:`client_from_session_file`).
    """
    own = http is None
    http = http or HttpClient(concurrency=concurrency * 2)
    report = SampleReport(package, strategy)
    sem = asyncio.Semaphore(concurrency)
    extra = {"dispenser_url": dispenser_url} if dispenser_url else {}
    base: Optional[PlayApiClient] = None
    if strategy == "devices":
        # one account (reuse the caller's logged-in session if given: no extra dispenser call),
        # a brand-new device check-in per sample
        base = base_client or PlayApiClient(device=device, http=http, **extra)
        await base.login()
    elif strategy != "accounts":
        raise ValueError("strategy must be 'devices' or 'accounts'")

    async def one(i: int) -> None:
        async with sem:
            if base is not None:
                c = PlayApiClient(device=device, http=http, email=base.session.email,
                                  auth_token=base.session.auth_token, accept_tos=True)
            else:
                c = PlayApiClient(device=device, http=http, **extra)
            smp = Sample(strategy, device, None, None)
            try:
                await c.login()
                ident = c.identity
                smp.account, smp.gsf_id = ident.get("account"), ident.get("gsf_id")
                d = await c.details(package)
                smp.version, smp.version_code = d.version, d.version_code
                if keep_sessions and c.session:
                    keep_sessions.mkdir(parents=True, exist_ok=True)
                    (keep_sessions / f"{package}_{d.version_code}_{smp.gsf_id}.json").write_text(
                        c.session.to_json(), encoding="utf-8")
            except StoreScraperError as exc:
                smp.error = f"{type(exc).__name__}: {exc}"
            report.samples.append(smp)

    try:
        await asyncio.gather(*(one(i) for i in range(n)))
    finally:
        if own:
            await http.aclose()
    return report


def client_from_session_file(path: Union[str, Path], http: Optional[HttpClient] = None) -> PlayApiClient:
    """Re-open a sampled session (same account + device id) - e.g. one that was offered a new build."""
    from .api import PlaySession
    s = PlaySession.from_json(Path(path).read_text(encoding="utf-8"))
    c = PlayApiClient(device=s.device, email=s.email, auth_token=s.auth_token, http=http, accept_tos=True)
    c.session = s
    return c


async def probe_version_code(client: PlayApiClient, package: str, version_code: int) -> dict[str, Any]:
    """Can this session get ``version_code`` by asking for it explicitly? (no download)"""
    try:
        info = await client.delivery_info(package, version_code)
        return {"version_code": version_code, "available": True, "files": len(info.files),
                "total_size": info.total_size}
    except StoreScraperError as exc:
        return {"version_code": version_code, "available": False, "error": f"{type(exc).__name__}: {exc}"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class RolloutWatcher:
    """Run :meth:`check` on a schedule (e.g. hourly). State + log live in ``state_dir``.

    A rollout is considered *active* when the listing's "Updated on" moved but the build our
    main session sees did not change, or when sampling finds a code above the one we see. While
    active (max ``active_days``), every check samples ``samples`` sessions and probes the newest
    known code. New builds are downloaded to ``download_dir`` when given.
    """

    def __init__(self, package: str, state_dir: Union[str, Path], *, country: str = "US",
                 device: str = DEFAULT_DEVICE, samples: int = 8, strategy: str = "devices",
                 download_dir: Union[str, Path, None] = None, active_days: float = 10.0,
                 always_sample: bool = False, probe: bool = True) -> None:
        self.package = package
        self.state_dir = Path(state_dir)
        self.country = country
        self.device = device
        self.samples = samples
        self.strategy = strategy
        self.download_dir = Path(download_dir) if download_dir else None
        self.active_days = active_days
        self.always_sample = always_sample
        self.probe = probe
        self.state_path = self.state_dir / f"{package}.state.json"
        self.log_path = self.state_dir / f"{package}.log.jsonl"
        self.obs_path = self.state_dir / f"{package}.observations.csv"

    def _observe(self, time_: str, source: str, version: Optional[str], code: Optional[int],
                 account: Optional[str] = None, gsf_id: Optional[str] = None, extra: str = "") -> None:
        """One CSV line per thing we saw - easy to grep/open in Excel later."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        new = not self.obs_path.exists()
        with open(self.obs_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write("time_utc,source,version,version_code,account,gsf_id,extra\n")
            fields = [time_, source, version or "", str(code or ""), account or "", gsf_id or "", extra]
            fh.write(",".join(f.replace(",", ";") for f in fields) + "\n")

    def report(self) -> list[dict[str, Any]]:
        """Every build ever seen, oldest first: version, code, first/last seen, how it was found."""
        st = self._load()
        rows = [{"version_code": int(k), **v} for k, v in st["known"].items()]
        return sorted(rows, key=lambda r: r["version_code"])

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"known": {}, "last_updated": None, "active_since": None, "downloaded": []}

    def _save(self, st: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st, indent=2), encoding="utf-8")

    def _log(self, entry: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    @staticmethod
    def _remember(st: dict, code: Optional[int], version: Optional[str], source: str) -> bool:
        if not code:
            return False
        k = str(code)
        new = k not in st["known"]
        rec = st["known"].setdefault(k, {"version": version, "first_seen": _now(), "sources": []})
        rec["last_seen"] = _now()
        rec.setdefault("first_source", source)
        if source not in rec["sources"]:
            rec["sources"].append(source)
        return new

    async def check(self) -> dict[str, Any]:
        from .scraper import GooglePlay  # local import: avoid cycle
        st = self._load()
        known_before = set(st["known"])
        entry: dict[str, Any] = {"time": _now(), "package": self.package}
        t0 = time.monotonic()
        sessions = self.state_dir / "sessions"
        async with GooglePlay(session_dir=self.state_dir, device=self.device) as gp:
            # 1) public listing: "Updated on" + displayed version
            try:
                web = await gp.app(self.package, self.country, version_mode="web")
                entry["web"] = {"version": web.version, "last_updated": web.last_updated}
                self._observe(entry["time"], f"web:{self.country}", web.version, None,
                              extra=f"listing_updated={web.last_updated}")
            except StoreScraperError as exc:
                web = None
                entry["web"] = {"error": f"{type(exc).__name__}: {exc}"}
            # 2) our main (cached) API session
            main = gp.api()
            try:
                d = await main.details(self.package)
                entry["main"] = {"version": d.version, "version_code": d.version_code, **main.identity}
                self._remember(st, d.version_code, d.version, "main")
                self._observe(entry["time"], "main", d.version, d.version_code, main.identity.get("account"),
                              main.identity.get("gsf_id"), extra=f"build_date={d.updated_raw}")
            except StoreScraperError as exc:
                d = None
                entry["main"] = {"error": f"{type(exc).__name__}: {exc}"}
            main_code = d.version_code if d else None

            # 3) rollout heuristics
            if web is not None and web.last_updated and web.last_updated != st.get("last_updated"):
                if st.get("last_updated") is not None:
                    entry["signal"] = "listing 'Updated on' changed"
                    st["active_since"] = st.get("active_since") or _now()
                st["last_updated"] = web.last_updated
            if web is not None and web.version and d and d.version and web.version != d.version:
                entry["signal"] = f"listing shows {web.version}, main session gets {d.version}"
                st["active_since"] = st.get("active_since") or _now()
            if st.get("active_since"):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["active_since"].replace("Z", "+00:00")))
                if age.total_seconds() > self.active_days * 86400:
                    st["active_since"] = None
                    entry["note"] = "rollout window closed"

            # 4) sample while a rollout looks active (or on the very first run, as a baseline)
            first_run = not st.get("sampled_once")
            if self.samples > 0 and (self.always_sample or st.get("active_since") or first_run):
                rep = await sample_versions(self.package, self.samples, strategy=self.strategy, device=self.device,
                                            keep_sessions=sessions if self.download_dir else None,
                                            base_client=main if (self.strategy == "devices" and d) else None)
                entry["sampling"] = rep.summary()
                st["sampled_once"] = True
                for s in rep.samples:
                    self._remember(st, s.version_code, s.version, f"sample:{self.strategy}")
                    self._observe(entry["time"], f"sample:{self.strategy}", s.version, s.version_code,
                                  s.account, s.gsf_id, extra=s.error or "")
                if rep.newest and main_code and rep.newest.version_code > main_code:
                    st["active_since"] = st.get("active_since") or _now()

            # 5) probe the newest known code from the main session (does asking by code work?)
            newest = max((int(k) for k in st["known"]), default=None)
            if self.probe and newest and main_code and newest > main_code:
                entry["probe"] = await probe_version_code(main, self.package, newest)
                if entry["probe"]["available"]:
                    self._remember(st, newest, st["known"][str(newest)]["version"], "probe_by_code")
                self._observe(entry["time"], "probe_by_code", st["known"][str(newest)]["version"], newest,
                              extra="available" if entry["probe"]["available"] else entry["probe"].get("error", ""))

            # 6) download builds we haven't stored yet
            if self.download_dir and newest and str(newest) not in st["downloaded"]:
                entry["download"] = await self._download(main, newest, sessions)
                if entry["download"].get("ok"):
                    st["downloaded"].append(str(newest))

        entry["newest_known"] = newest
        entry["new_builds"] = sorted(set(st["known"]) - known_before, key=int)
        entry["seconds"] = round(time.monotonic() - t0, 1)
        self._save(st)
        self._log(entry)
        return entry

    async def _download(self, main: PlayApiClient, code: int, sessions: Path) -> dict[str, Any]:
        dest = self.download_dir / self.package / str(code)
        tried = []
        candidates = [main] + [client_from_session_file(p, main.http)
                               for p in sorted(sessions.glob(f"{self.package}_{code}_*.json"))]
        for c in candidates[:4]:
            try:
                info, paths = await c.download(self.package, dest, code)
                return {"ok": True, "dest": str(dest), "files": [p.name for p in paths], "bytes": info.total_size,
                        "via": "main" if c is main else "sampled_session"}
            except StoreScraperError as exc:
                tried.append(f"{type(exc).__name__}: {exc}")
        return {"ok": False, "errors": tried}


__all__ = ["Sample", "SampleReport", "sample_versions", "probe_version_code", "client_from_session_file",
           "RolloutWatcher"]
