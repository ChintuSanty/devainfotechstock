"""Audio device discovery.

Two logical channels are captured:
  * 'others' - the WASAPI loopback of the default render device, which is every
    sound the machine plays: Zoom, Teams, Meet in the browser, Webex, anything.
  * 'me'     - the microphone.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, asdict
from typing import Any, Literal

Kind = Literal["loopback", "input"]


@dataclass
class DeviceInfo:
    id: str
    name: str
    kind: Kind
    channels: int
    sample_rate: int
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_devices() -> dict[str, list[dict[str, Any]]]:
    """Return {'loopback': [...], 'input': [...]} for the settings screen."""
    if platform.system() == "Windows":
        from .backends.windows_wasapi import list_wasapi_devices

        return list_wasapi_devices()
    from .backends.portaudio import list_portaudio_devices

    return list_portaudio_devices()
