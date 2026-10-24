"""Android device profiles used to talk to the Play Android API.

Google Play decides which build of a multi-APK / app-bundle app a device receives from the
device configuration uploaded at login (ABIs, SDK level, screen, GL extensions, features).
That is why "Varies with device" apps can only be resolved *for a concrete device*.

The bundled profiles (``data/devices.properties``) come from EFF's rs-google-play, which
collected them from the Calyx Institute and AuroraOSS; that file is GPL-3.0-or-later (see
the SPDX headers inside it). You can load your own file exported from Aurora Store's spoof
manager with ``load_profiles(path)``.
"""
from __future__ import annotations

import configparser
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Optional, Union

from .proto import Field

DEFAULT_DEVICE = "px_9a"
#: A spread of form factors / ABIs / SDK levels used by ``all_devices`` version resolution.
REPRESENTATIVE_DEVICES = ("px_9a", "sm_s25u", "px_tablet", "sm_a13_5g", "google_kiwi_x86_64", "bravia_vu2")


@dataclass
class DeviceProfile:
    name: str
    props: dict[str, str] = field(repr=False)

    def get(self, key: str, default: str = "") -> str:
        return self.props.get(key, default)

    def _int(self, key: str) -> Optional[int]:
        v = self.props.get(key)
        try:
            return int(v) if v not in (None, "") else None
        except ValueError:
            return None

    def _bool(self, key: str) -> bool:
        return self.props.get(key, "false").strip().lower() == "true"

    def _list(self, key: str) -> list[str]:
        return [s for s in self.props.get(key, "").split(",") if s]

    @property
    def sdk(self) -> Optional[int]:
        return self._int("Build.VERSION.SDK_INT")

    @property
    def platforms(self) -> list[str]:
        return self._list("Platforms")

    @property
    def readable_name(self) -> str:
        return self.get("UserReadableName", self.name)

    # ------------------------------------------------------------------ protobuf builders
    def device_configuration(self) -> list[Field]:
        """``DeviceConfigurationProto``."""
        f: list[Field] = [
            (1, self._int("TouchScreen")),
            (2, self._int("Keyboard")),
            (3, self._int("Navigation")),
            (4, self._int("ScreenLayout")),
            (5, self._bool("HasHardKeyboard")),
            (6, self._bool("HasFiveWayNavigation")),
            (7, self._int("Screen.Density")),
            (8, self._int("GL.Version")),
        ]
        f += [(9, s) for s in self._list("SharedLibraries")]
        f += [(10, s) for s in self._list("Features")]
        f += [(11, s) for s in self.platforms]
        f += [(12, self._int("Screen.Width")), (13, self._int("Screen.Height"))]
        f += [(14, s) for s in self._list("Locales")]
        f += [(15, s) for s in self._list("GL.Extensions")]
        f += [(26, [(1, s), (2, 0)]) for s in self._list("Features")]
        return f

    def android_build(self) -> list[Field]:
        """``AndroidBuildProto``."""
        return [
            (1, self.get("Build.FINGERPRINT")),
            (2, self.get("Build.HARDWARE")),
            (3, self.get("Build.BRAND")),
            (4, self.get("Build.RADIO")),
            (5, self.get("Build.BOOTLOADER")),
            (6, self.get("Client", "android-google")),
            (7, int(time.time() / 1000)),  # mirrors gplayapi / rs-google-play
            (8, self._int("GSF.version")),
            (9, self.get("Build.DEVICE")),
            (10, self.sdk),
            (11, self.get("Build.MODEL")),
            (12, self.get("Build.MANUFACTURER")),
            (13, self.get("Build.PRODUCT")),
            (14, False),
        ]

    def checkin_request(self, locale: str, timezone: str) -> list[Field]:
        """``AndroidCheckinRequest``."""
        checkin = [
            (1, self.android_build()),
            (2, 0),
            (6, self.get("CellOperator") or None),
            (7, self.get("SimOperator") or None),
            (8, self.get("Roaming") or None),
            (9, 0),
        ]
        return [
            (2, 0),
            (4, checkin),
            (6, locale),
            (12, timezone),
            (14, 3),
            (18, self.device_configuration()),
            (20, 0),
        ]

    def finsky_user_agent(self) -> str:
        return (
            f"Android-Finsky/{self.get('Vending.versionString')} (api=3,versionCode={self.get('Vending.version')},"
            f"sdk={self.sdk},device={self.get('Build.DEVICE')},hardware={self.get('Build.HARDWARE')},"
            f"product={self.get('Build.PRODUCT')},platformVersionRelease={self.get('Build.VERSION.RELEASE')},"
            f"model={self.get('Build.MODEL')},buildId={self.get('Build.ID')},isWideScreen=0,"
            f"supportedAbis={';'.join(self.platforms)})"
        )

    def auth_user_agent(self) -> str:
        return f"GoogleAuth/1.4 ({self.get('Build.DEVICE')} {self.get('Build.ID')})"


def _parse(text: str) -> dict[str, DeviceProfile]:
    cp = configparser.RawConfigParser(strict=False, interpolation=None, comment_prefixes=("#",), delimiters=("=",))
    cp.optionxform = str  # keep key case
    cp.read_string(text)
    out = {}
    for section in cp.sections():
        props = {k: v.replace("\\:", ":") for k, v in cp.items(section)}
        out[section] = DeviceProfile(section, props)
    return out


_BUNDLED: Optional[dict[str, DeviceProfile]] = None


def load_profiles(path: Union[str, Path, None] = None) -> dict[str, DeviceProfile]:
    """Load profiles from ``path`` (INI, Aurora/rs-google-play format) or the bundled file."""
    global _BUNDLED
    if path is not None:
        return _parse(Path(path).read_text(encoding="utf-8"))
    if _BUNDLED is None:
        text = resources.files("mobile_store_scraper").joinpath("data/devices.properties").read_text(encoding="utf-8")
        _BUNDLED = _parse(text)
    return _BUNDLED


def get_profile(name_or_profile: Union[str, DeviceProfile, None] = None) -> DeviceProfile:
    if isinstance(name_or_profile, DeviceProfile):
        return name_or_profile
    name = name_or_profile or DEFAULT_DEVICE
    profiles = load_profiles()
    if name not in profiles:
        raise KeyError(f"unknown device profile {name!r}; available: {', '.join(sorted(profiles))}")
    return profiles[name]


def available_devices() -> list[str]:
    return sorted(load_profiles())
