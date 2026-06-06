from __future__ import annotations

from enum import StrEnum


class ScanState(StrEnum):
    IDLE = "idle"
    QUICKSCAN = "quickscan"
    METADATA = "metadata"


class ScanPhase(StrEnum):
    DISCOVERY = "discovery"
    METADATA = "metadata"
    PROBE = "probe"
    STORAGE = "storage"
