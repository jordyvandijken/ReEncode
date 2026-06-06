from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def _default_db_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ReEncode" / "scan-records.db"
    return Path.home() / ".reencode" / "scan-records.db"


def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class ScanStore:
    """SQLite-backed storage for scan records and probe reuse decisions.

    `last_scanned` is persisted as a Unix timestamp in seconds.
    """

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_schema(self):
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_records (
                absolute_path TEXT PRIMARY KEY,
                source_root TEXT NOT NULL,
                media_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                last_modified INTEGER NOT NULL,
                encoding TEXT,
                probe_json TEXT,
                last_scanned INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scan_records_source_last_scanned
            ON scan_records (source_root, last_scanned)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scan_records_media_type
            ON scan_records (media_type)
            """
        )
        # Migrate legacy scan-token values (for example 1, 2, 3) to comparable timestamps.
        self._conn.execute(
            """
            UPDATE scan_records
            SET last_scanned = CASE
                WHEN last_modified > 0 THEN last_modified
                ELSE CAST(strftime('%s','now') AS INTEGER)
            END
            WHERE last_scanned < 946684800
            """
        )
        self._conn.commit()

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    def get_record(self, absolute_path: str) -> dict[str, Any] | None:
        normalized = _normalize_path(absolute_path)
        row = self._conn.execute(
            """
            SELECT absolute_path, source_root, media_type, file_size, last_modified,
                   encoding, probe_json, last_scanned
            FROM scan_records
            WHERE absolute_path = ?
            """,
            (normalized,),
        ).fetchone()
        if row is None:
            return None

        probe_json = row["probe_json"]
        probe = None
        if isinstance(probe_json, str) and probe_json:
            try:
                probe = json.loads(probe_json)
            except json.JSONDecodeError:
                probe = None

        return {
            "absolute_path": row["absolute_path"],
            "source_root": row["source_root"],
            "media_type": row["media_type"],
            "file_size": int(row["file_size"]),
            "last_modified": int(row["last_modified"]),
            "encoding": row["encoding"],
            "probe": probe,
            "last_scanned": int(row["last_scanned"]),
        }

    def find_reusable_probe(self, absolute_path: str, file_size: int, last_modified: int) -> dict | None:
        record = self.get_record(absolute_path)
        if record is None:
            return None

        if record["file_size"] != file_size or record["last_modified"] != last_modified:
            return None

        probe = record.get("probe")
        if isinstance(probe, dict):
            return probe
        return None

    def upsert_record(
        self,
        absolute_path: str,
        source_root: str,
        media_type: str,
        file_size: int,
        last_modified: int,
        scanned_at: int | float | None = None,
        encoding: str | None = None,
        probe: dict | None = None,
        commit: bool = True,
    ):
        normalized_path = _normalize_path(absolute_path)
        normalized_root = _normalize_path(source_root)
        probe_json = json.dumps(probe, ensure_ascii=True, separators=(",", ":")) if probe else None
        scanned_at_value = int(scanned_at if scanned_at is not None else time.time())

        self._conn.execute(
            """
            INSERT INTO scan_records (
                absolute_path,
                source_root,
                media_type,
                file_size,
                last_modified,
                encoding,
                probe_json,
                last_scanned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(absolute_path) DO UPDATE SET
                source_root = excluded.source_root,
                media_type = excluded.media_type,
                file_size = excluded.file_size,
                last_modified = excluded.last_modified,
                encoding = excluded.encoding,
                probe_json = excluded.probe_json,
                last_scanned = excluded.last_scanned
            """,
            (
                normalized_path,
                normalized_root,
                media_type,
                int(file_size),
                int(last_modified),
                encoding,
                probe_json,
                scanned_at_value,
            ),
        )
        if commit:
            self._conn.commit()

    def prune_scan_scope(self, source_roots: list[str], scan_started_at: int | float) -> int:
        roots = [_normalize_path(root) for root in source_roots]
        if not roots:
            return 0

        placeholders = ",".join("?" for _ in roots)
        params = [*roots, int(scan_started_at)]
        cursor = self._conn.execute(
            f"""
            DELETE FROM scan_records
            WHERE source_root IN ({placeholders})
            AND last_scanned < ?
            """,
            params,
        )
        self._conn.commit()
        return int(cursor.rowcount or 0)
