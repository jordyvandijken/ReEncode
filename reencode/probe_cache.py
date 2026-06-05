"""Persistent cache for ffprobe metadata.

Each cache entry stores probe data plus a file snapshot so entries can be
invalidated when the source file changes.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def _cache_file_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ReEncode" / "probe-cache.json"
    return Path.home() / ".reencode" / "probe-cache.json"


def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class ProbeCache:
    """Disk-backed cache for probe metadata."""

    def __init__(self, file_path: Path | None = None):
        self._file_path = file_path or _cache_file_path()
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            text = self._file_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._entries = {}
            self._dirty = False
            return

        entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            self._entries = entries
        else:
            self._entries = {}
        self._dirty = False

    def get_valid_probe(self, path: str) -> dict | None:
        key = _normalize_path(path)
        entry = self._entries.get(key)
        if not isinstance(entry, dict):
            return None

        try:
            stat = os.stat(path)
        except OSError:
            return None

        expected_mtime_ns = entry.get("file_mtime_ns")
        expected_size = entry.get("file_size")

        if expected_mtime_ns != stat.st_mtime_ns or expected_size != stat.st_size:
            return None

        probe = entry.get("probe")
        if not isinstance(probe, dict):
            return None
        return probe

    def upsert_probe(self, path: str, probe: dict):
        key = _normalize_path(path)

        try:
            stat = os.stat(path)
        except OSError:
            return

        self._entries[key] = {
            "path": os.path.abspath(path),
            "file_mtime_ns": stat.st_mtime_ns,
            "file_size": stat.st_size,
            "probed_at": time.time(),
            "probe": probe,
        }
        self._dirty = True

    def save(self):
        if not self._dirty:
            return

        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "entries": self._entries,
        }

        fd, tmp_path = tempfile.mkstemp(
            prefix="probe-cache-",
            suffix=".tmp",
            dir=str(self._file_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(payload, tmp_file, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp_path, self._file_path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return

        self._dirty = False
