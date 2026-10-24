"""Download exact APK builds from Google Play (purchase -> delivery -> verified files).

Only free apps (or apps owned by the logged-in account). Play serves the version code a device
is currently eligible for - plus, possibly, a code requested explicitly while it is still
being served (e.g. during a staged rollout). Old, withdrawn version codes are not available.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..errors import PlayApiError
from ..http import HttpClient
from .proto import Msg

ProgressCb = Callable[[str, int, Optional[int]], None]  # (filename, bytes_done, bytes_total)


class VersionNotAvailable(PlayApiError):
    """Play refused to deliver the requested version code to this account/device."""


@dataclass
class DeliveryFile:
    kind: str                 # "base" | "split" | "obb_main" | "obb_patch" | "dex_metadata"
    name: str                 # split name ("config.arm64_v8a") or "base"
    filename: str
    url: str
    size: Optional[int] = None
    sha1: Optional[str] = None
    sha256: Optional[str] = None


@dataclass
class DeliveryInfo:
    package: str
    version_code: int
    device: str
    files: list[DeliveryFile] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def total_size(self) -> int:
        return sum(f.size or 0 for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("cookies", None)
        for f in d["files"]:
            f.pop("url", None)  # signed, short-lived and account-bound - don't persist
        d["total_size"] = self.total_size
        return d


def parse_delivery(data: Msg, package: str, version_code: int, device: str) -> DeliveryInfo:
    """``AndroidAppDeliveryData`` -> ``DeliveryInfo``."""
    info = DeliveryInfo(package=package, version_code=version_code, device=device)
    if data.str(3):
        info.files.append(DeliveryFile("base", "base", f"{package}.apk", data.str(3), data.int(1),
                                       data.str(2), data.str(19)))
    for sp in data.msgs(15):
        name = sp.str(1) or "split"
        if sp.str(5):
            info.files.append(DeliveryFile("split", name, f"{package}.{name}.apk", sp.str(5), sp.int(2),
                                           sp.str(4), sp.str(9)))
    for af in data.msgs(4):
        kind = "obb_main" if (af.int(1) or 0) == 0 else "obb_patch"
        vc = af.int(2) or version_code
        if af.str(4):
            info.files.append(DeliveryFile(kind, kind, f"{'main' if kind == 'obb_main' else 'patch'}.{vc}.{package}.obb",
                                           af.str(4), af.int(3), af.str(8)))
    dm = data.msg(21)
    if dm.str(3):
        info.files.append(DeliveryFile("dex_metadata", "dex_metadata", f"{package}.dm", dm.str(3), dm.int(1),
                                       None, dm.str(2)))
    for c in data.msgs(5):
        if c.str(1):
            info.cookies[c.str(1)] = c.str(2) or ""
    return info


def _digest_matches(expected: Optional[str], digest: bytes) -> Optional[bool]:
    """Play encodes hashes as unpadded URL-safe base64 (sometimes hex). ``None`` = nothing to check."""
    if not expected:
        return None
    e = expected.strip()
    if e.lower() == digest.hex():
        return True
    b64 = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    b64std = base64.b64encode(digest).decode().rstrip("=")
    return e.rstrip("=") in (b64, b64std)


async def download_files(
    info: DeliveryInfo,
    dest: Path,
    http: HttpClient,
    *,
    verify: bool = True,
    kinds: Optional[set[str]] = None,
    progress: Optional[ProgressCb] = None,
    chunk: int = 1 << 20,
) -> list[Path]:
    """Stream every delivery file to ``dest`` (resumable-safe: writes ``*.part`` then renames)."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for f in info.files:
        if kinds and f.kind not in kinds:
            continue
        target = dest / f.filename
        part = target.with_suffix(target.suffix + ".part")
        h1, h256 = hashlib.sha1(), hashlib.sha256()
        done = 0
        async with http.semaphore:
            async with http.client.stream("GET", f.url, cookies=info.cookies or None, timeout=None) as resp:
                if resp.status_code != 200:
                    raise PlayApiError(f"download {f.filename}: HTTP {resp.status_code}")
                with open(part, "wb") as fh:
                    async for block in resp.aiter_bytes(chunk):
                        fh.write(block)
                        h1.update(block)
                        h256.update(block)
                        done += len(block)
                        if progress:
                            progress(f.filename, done, f.size)
        if verify:
            if f.size is not None and done != f.size:
                part.unlink(missing_ok=True)
                raise PlayApiError(f"{f.filename}: size {done} != expected {f.size}")
            for expected, digest, label in ((f.sha256, h256.digest(), "sha256"), (f.sha1, h1.digest(), "sha1")):
                if _digest_matches(expected, digest) is False:
                    part.unlink(missing_ok=True)
                    raise PlayApiError(f"{f.filename}: {label} mismatch")
        part.replace(target)
        out.append(target)
    manifest = info.to_dict()
    manifest["verified"] = verify
    (dest / "delivery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


__all__ = ["DeliveryFile", "DeliveryInfo", "VersionNotAvailable", "parse_delivery", "download_files"]
