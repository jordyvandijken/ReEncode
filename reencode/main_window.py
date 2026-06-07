import os
from queue import Queue
from time import perf_counter
import math
import time
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtWidgets import QMainWindow, QSplitter, QStatusBar, QTabWidget

from reencode import codec_probe
from reencode.constants import MEDIA_TYPES
from reencode.media_panel import FailedPanel, MediaPanel
from reencode.scan_contracts import ScanPhase, ScanState
from reencode.scan_store import ScanStore
from reencode.scanner import ScannerThread
from reencode import size_estimator
from reencode.sources_panel import SourcesPanel


class _LazyScanStore:
    def __init__(self):
        self._store: ScanStore | None = None

    @property
    def db_path(self) -> Path:
        return self._ensure().db_path

    def _ensure(self) -> ScanStore:
        if self._store is None:
            self._store = ScanStore()
        return self._store

    def upsert_record(self, *args, **kwargs):
        return self._ensure().upsert_record(*args, **kwargs)

    def prune_scan_scope(self, *args, **kwargs) -> int:
        return self._ensure().prune_scan_scope(*args, **kwargs)

    def close(self):
        if self._store is None:
            return
        self._store.close()
        self._store = None


class _MetadataProbeWorker(QThread):
    row_ready = Signal(int, str, str, object, str, object, object, object, object)
    failed_item = Signal(int, str, str, str, str)
    progress = Signal(int, str, int, int)
    completed = Signal(int, str, bool, int, int)
    fatal_error = Signal(int, str, str)

    def __init__(self, scan_id: int, store_path: str, source_roots: list[str], parent=None):
        super().__init__(parent)
        self._scan_id = scan_id
        self._store_path = store_path
        self._source_roots = [os.path.normcase(os.path.normpath(os.path.abspath(root))) for root in source_roots]
        self._jobs: Queue[tuple[str, str] | None] = Queue()
        self._cancelled = False
        self._input_closed = False
        self._expected_total = 0
        self._processed = 0
        self._probed = 0

    def submit(self, media_type: str, path: str):
        self._jobs.put((media_type, path))

    def finish(self, expected_total: int):
        self._expected_total = int(expected_total)
        self._input_closed = True
        self._jobs.put(None)

    def cancel(self):
        self._cancelled = True
        self._jobs.put(None)

    def _emit_progress(self):
        total = max(self._expected_total, self._processed)
        self.progress.emit(self._scan_id, ScanPhase.METADATA.value, self._processed, total)

    def _encoding_from_probe(self, media_type: str, probe_info: dict | None) -> str | None:
        if not isinstance(probe_info, dict):
            return None
        if media_type == "Videos":
            value = probe_info.get("video_codec")
        elif media_type == "Audio":
            value = probe_info.get("audio_codec")
        else:
            value = None
        return str(value) if value else None

    def _source_root_for_path(self, path: str) -> str:
        normalized = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        for root in self._source_roots:
            if normalized == root or normalized.startswith(root + os.sep):
                return root
        return self._source_roots[0] if self._source_roots else normalized

    def run(self):
        store = ScanStore(db_path=Path(self._store_path))
        try:
            while True:
                if self._cancelled:
                    break

                job = self._jobs.get()
                if job is None:
                    if self._cancelled:
                        break
                    if self._input_closed and self._jobs.empty():
                        break
                    continue

                if self._cancelled:
                    break

                media_type, path = job
                try:
                    stat = os.stat(path)
                except OSError:
                    self.failed_item.emit(
                        self._scan_id,
                        media_type,
                        path,
                        "Metadata stat failed",
                        ScanPhase.METADATA.value,
                    )
                    self._processed += 1
                    self._emit_progress()
                    continue

                size_bytes = stat.st_size
                modified = str(int(stat.st_mtime))
                modified_int = int(stat.st_mtime)
                probe_info = None
                encoding = None
                estimate = None
                recommend = None

                if media_type in {"Videos", "Audio"}:
                    probe_info = store.find_reusable_probe(path, size_bytes, modified_int)
                    if probe_info is None:
                        probe_info = codec_probe.probe_media_info(path)
                        if probe_info is None:
                            self.failed_item.emit(
                                self._scan_id,
                                media_type,
                                path,
                                "Probe failed",
                                ScanPhase.PROBE.value,
                            )

                    if isinstance(probe_info, dict):
                        self._probed += 1
                        encoding = self._encoding_from_probe(media_type, probe_info)
                        estimate, _ = size_estimator.estimate_output(size_bytes, media_type, path, probe_info)
                        if media_type == "Videos":
                            video_codec = (probe_info.get("video_codec") or "").lower()
                            if video_codec:
                                _status, recommend, _reason = codec_probe.recommendation(video_codec)

                self.row_ready.emit(
                    self._scan_id,
                    media_type,
                    path,
                    size_bytes,
                    modified,
                    probe_info,
                    encoding,
                    estimate,
                    recommend,
                )
                source_root = self._source_root_for_path(path)
                try:
                    store.upsert_record(
                        absolute_path=path,
                        source_root=source_root,
                        media_type=media_type,
                        file_size=size_bytes,
                        last_modified=modified_int,
                        scanned_at=int(time.time()),
                        encoding=encoding,
                        probe=probe_info,
                        commit=False,
                    )
                except Exception as exc:
                    self.failed_item.emit(
                        self._scan_id,
                        media_type,
                        path,
                        f"Storage upsert failed: {exc}",
                        ScanPhase.STORAGE.value,
                    )
                self._processed += 1
                self._emit_progress()

            store.commit()
        except Exception as exc:  # pragma: no cover
            self.fatal_error.emit(self._scan_id, ScanPhase.METADATA.value, str(exc))
            self._cancelled = True
        finally:
            store.close()

        self.completed.emit(
            self._scan_id,
            ScanPhase.METADATA.value,
            self._cancelled,
            self._processed,
            self._probed,
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ReEncode — Media Scanner")
        self.resize(1100, 650)

        self._scanner: ScannerThread | None = None
        self._metadata_probe_worker: _MetadataProbeWorker | None = None
        self._scan_token = 0
        self._scan_state = ScanState.IDLE
        self._total_found = 0
        self._discovery_count = 0
        self._active_source_roots: list[str] = []
        self._discovery_finished = False
        self._worker_finished = False
        self._worker_cancelled = False
        self._worker_processed_count = 0
        self._worker_probed_count = 0
        self._cancel_requested = False

        self._discovery_progress_count = 0
        self._metadata_total = 0

        self._discovered_files: list[tuple[str, str]] = []
        self._pending_metadata_rows: dict[str, list[tuple[str, int, str]]] = {}
        self._pending_metadata_updates: dict[str, list[tuple[str, int, str, int | None]]] = {}
        self._pending_probe_updates: dict[str, tuple[str, dict | None]] = {}
        self._pending_failed_rows: list[tuple[str, str, str]] = []

        self._metadata_processed = 0
        self._scan_store: ScanStore | _LazyScanStore = _LazyScanStore()

        self._metadata_flush_timer = QTimer(self)
        self._metadata_flush_timer.setInterval(100)
        self._metadata_flush_timer.timeout.connect(self._flush_metadata_rows)

        self._probe_flush_timer = QTimer(self)
        self._probe_flush_timer.setInterval(100)
        self._probe_flush_timer.timeout.connect(self._flush_probe_updates)

        self._failed_flush_timer = QTimer(self)
        self._failed_flush_timer.setInterval(100)
        self._failed_flush_timer.timeout.connect(self._flush_failed_rows)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(self._refresh_scan_status)

        self._scan_started_at: float | None = None
        self._scan_started_at_epoch: int | None = None

        self._setup_ui()

    def _get_scan_store(self) -> ScanStore:
        if isinstance(self._scan_store, _LazyScanStore):
            return self._scan_store._ensure()
        return self._scan_store

    def _setup_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        self._sources_panel = SourcesPanel()
        self._sources_panel.setMinimumWidth(220)
        self._sources_panel.setMaximumWidth(360)
        self._sources_panel.scan_requested.connect(self._start_scan)
        self._sources_panel.cancel_requested.connect(self._cancel_scan)
        splitter.addWidget(self._sources_panel)

        self._tab_widget = QTabWidget()
        self._panels: dict[str, MediaPanel] = {}
        for media_type in MEDIA_TYPES:
            panel = MediaPanel(media_type)
            panel.conversion_status_changed.connect(self._on_conversion_status_changed)
            self._panels[media_type] = panel
            self._tab_widget.addTab(panel, media_type)

        self._failed_panel = FailedPanel()
        self._tab_widget.addTab(self._failed_panel, "Failed")
        splitter.addWidget(self._tab_widget)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready. Add folders and click Scan.")

    def _start_scan(self, folders: list[str]):
        self._scan_token += 1
        self._scan_state = ScanState.QUICKSCAN
        self._active_source_roots = [os.path.normcase(os.path.normpath(os.path.abspath(folder))) for folder in folders]

        if self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()
        if self._probe_flush_timer.isActive():
            self._probe_flush_timer.stop()
        if self._failed_flush_timer.isActive():
            self._failed_flush_timer.stop()

        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()

        if self._metadata_probe_worker and self._metadata_probe_worker.isRunning():
            self._metadata_probe_worker.cancel()
            self._metadata_probe_worker.wait()

        for panel in self._panels.values():
            panel.clear()
            panel.set_scan_locked(True)
            if panel in (self._panels.get("Videos"), self._panels.get("Audio")):
                panel.begin_probe_updates()
        self._failed_panel.clear()
        self._failed_panel.set_scan_locked(True)

        self._total_found = 0
        self._discovery_count = 0
        self._discovery_finished = False
        self._worker_finished = False
        self._worker_cancelled = False
        self._worker_processed_count = 0
        self._worker_probed_count = 0
        self._cancel_requested = False
        self._discovery_progress_count = 0
        self._metadata_total = 0
        self._pending_metadata_rows = {}
        self._pending_metadata_updates = {}
        self._pending_probe_updates = {}
        self._pending_failed_rows = []
        self._discovered_files = []
        self._metadata_processed = 0
        self._scan_started_at = perf_counter()
        self._scan_started_at_epoch = int(time.time())

        self._sources_panel.set_scanning(True)
        self._status_bar.showMessage("Scanning…")
        if not self._status_timer.isActive():
            self._status_timer.start()

        self._metadata_probe_worker = None

        self._scanner = ScannerThread(self._scan_token, folders, MEDIA_TYPES, parent=self)
        self._scanner.file_found.connect(self._on_file_found)
        self._scanner.progress.connect(self._on_discovery_progress)
        self._scanner.discovery_finished.connect(self._on_discovery_finished)
        self._scanner.fatal_error.connect(self._on_discovery_fatal_error)
        self._scanner.start()

    def _cancel_scan(self):
        if self._scan_state == ScanState.IDLE:
            return

        self._cancel_requested = True
        self._worker_cancelled = True
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
        if self._metadata_probe_worker and self._metadata_probe_worker.isRunning():
            self._metadata_probe_worker.cancel()

        self._status_bar.showMessage(f"Cancelling scan… {self._scan_timing_text()}")

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            return "--:--"

        total_seconds = int(seconds)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:02}:{minutes:02}:{secs:02}"
        return f"{minutes:02}:{secs:02}"

    def _scan_elapsed_seconds(self) -> float | None:
        if self._scan_started_at is None:
            return None
        return max(0.0, perf_counter() - self._scan_started_at)

    def _scan_timing_text(self, completed: int | None = None, total: int | None = None) -> str:
        elapsed = self._scan_elapsed_seconds()
        elapsed_text = self._format_duration(elapsed)

        eta_seconds: float | None = None
        if (
            elapsed is not None
            and elapsed > 0
            and completed is not None
            and total is not None
            and total > 0
            and completed > 0
        ):
            remaining = max(0, total - completed)
            if remaining == 0:
                eta_seconds = 0.0
            else:
                rate = completed / elapsed
                if rate > 0:
                    eta_seconds = remaining / rate

        eta_text = self._format_duration(eta_seconds)
        return f"{elapsed_text} - {eta_text}"

    def _on_file_found(self, scan_token: int, media_type: str, path: str):
        if scan_token != self._scan_token:
            return

        source_root = self._source_root_for_path(path)
        try:
            self._get_scan_store().upsert_record(
                absolute_path=path,
                source_root=source_root,
                media_type=media_type,
                file_size=0,
                last_modified=0,
                scanned_at=int(time.time()),
                commit=False,
            )
        except Exception as exc:
            self._pending_failed_rows.append(
                (os.path.basename(path) or media_type, f"Storage upsert failed: {exc} ({ScanPhase.STORAGE.value})", path)
            )
            if not self._failed_flush_timer.isActive():
                self._failed_flush_timer.start()

        self._discovered_files.append((media_type, path))
        self._pending_metadata_rows.setdefault(media_type, []).append((path, 0, ""))
        if not self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.start()
        self._total_found += 1

    def _on_discovery_progress(self, scan_token: int, _phase: str, discovered_count: int, _total: int):
        if scan_token != self._scan_token:
            return
        self._discovery_progress_count = discovered_count

    def _on_discovery_finished(self, scan_token: int, _phase: str, count: int, cancelled: bool):
        if scan_token != self._scan_token:
            return

        if self._scanner is not None:
            self._scanner.deleteLater()
            self._scanner = None

        try:
            self._get_scan_store().commit()
        except Exception as exc:
            self._pending_failed_rows.append(("Discovery commit", f"Storage commit failed: {exc} ({ScanPhase.STORAGE.value})", ""))
            if not self._failed_flush_timer.isActive():
                self._failed_flush_timer.start()

        self._discovery_finished = True
        self._discovery_count = count

        if cancelled:
            self._worker_cancelled = True

        if cancelled or not self._discovered_files:
            self._worker_finished = True
            self._worker_processed_count = 0
            self._worker_probed_count = 0
            self._try_finalize_scan()
            return

        self._scan_state = ScanState.METADATA
        store = self._get_scan_store()
        self._metadata_probe_worker = _MetadataProbeWorker(
            self._scan_token,
            str(store.db_path),
            self._active_source_roots,
            parent=self,
        )
        self._metadata_probe_worker.row_ready.connect(self._on_row_ready)
        self._metadata_probe_worker.failed_item.connect(self._on_failed_item)
        self._metadata_probe_worker.progress.connect(self._on_worker_progress)
        self._metadata_probe_worker.completed.connect(self._on_worker_completed)
        self._metadata_probe_worker.fatal_error.connect(self._on_worker_fatal_error)

        for media_type, path in self._discovered_files:
            self._metadata_probe_worker.submit(media_type, path)
        self._metadata_probe_worker.finish(len(self._discovered_files))
        self._metadata_probe_worker.start()

        self._try_finalize_scan()

    def _on_discovery_fatal_error(self, scan_token: int, phase: str, message: str):
        if scan_token != self._scan_token:
            return
        self._pending_failed_rows.append(("Discovery fatal", f"{message} ({phase})", ""))
        self._worker_cancelled = True
        if not self._failed_flush_timer.isActive():
            self._failed_flush_timer.start()

    def _on_row_ready(
        self,
        scan_token: int,
        media_type: str,
        path: str,
        size_bytes: int,
        modified_timestamp: str,
        probe_info: dict | None,
        encoding: str | None,
        estimate: int | None,
        _recommend: str | None,
    ):
        if scan_token != self._scan_token:
            return

        self._pending_metadata_updates.setdefault(media_type, []).append((path, int(size_bytes), modified_timestamp, estimate))
        if media_type in {"Videos", "Audio"}:
            self._pending_probe_updates[path] = (media_type, probe_info)

        if not self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.start()
        if self._pending_probe_updates and not self._probe_flush_timer.isActive():
            self._probe_flush_timer.start()

    def _on_failed_item(self, scan_token: int, media_type: str, path: str, reason: str, phase: str):
        if scan_token != self._scan_token:
            return

        name = os.path.basename(path) if path else media_type or "Unknown"
        self._pending_failed_rows.append((name, f"{reason} ({phase})", path))
        if not self._failed_flush_timer.isActive():
            self._failed_flush_timer.start()

    def _on_worker_progress(self, scan_token: int, phase: str, completed: int, total: int):
        if scan_token != self._scan_token:
            return

        self._metadata_processed = completed
        self._metadata_total = max(total, completed)

    def _on_worker_completed(self, scan_token: int, _phase: str, cancelled: bool, processed: int, probed: int):
        sender = self.sender()
        if isinstance(sender, _MetadataProbeWorker):
            sender.deleteLater()

        if scan_token != self._scan_token:
            return

        self._metadata_probe_worker = None
        self._worker_finished = True
        self._worker_cancelled = cancelled
        self._worker_processed_count = processed
        self._worker_probed_count = probed
        self._try_finalize_scan()

    def _on_worker_fatal_error(self, scan_token: int, phase: str, message: str):
        if scan_token != self._scan_token:
            return
        self._pending_failed_rows.append(("Worker fatal", f"{message} ({phase})", ""))
        self._worker_cancelled = True
        if not self._failed_flush_timer.isActive():
            self._failed_flush_timer.start()

    def _flush_metadata_rows(self, limit: int = 250):
        total_added = 0
        total_updated = 0

        for media_type, rows in self._pending_metadata_rows.items():
            if not rows:
                continue

            panel = self._panels.get(media_type)
            if panel is None:
                rows.clear()
                continue

            if limit == 0:
                take = len(rows)
            else:
                remaining = limit - total_added
                if remaining <= 0:
                    break
                take = min(remaining, len(rows))

            batch = rows[:take]
            del rows[:take]
            panel.add_files(batch)
            total_added += take

        for media_type, rows in self._pending_metadata_updates.items():
            if not rows:
                continue

            panel = self._panels.get(media_type)
            if panel is None:
                rows.clear()
                continue

            rows[:] = panel.prioritize_stat_updates(rows)

            if limit == 0:
                take = len(rows)
            else:
                remaining = limit - total_added
                if remaining <= 0:
                    break
                take = min(remaining, len(rows))

            batch = rows[:take]
            del rows[:take]
            panel.update_file_stats(batch)
            total_updated += take
            total_added += take

        if total_updated and self._scan_state == ScanState.METADATA:
            self._update_metadata_status()

        has_pending = any(rows for rows in self._pending_metadata_rows.values()) or any(
            rows for rows in self._pending_metadata_updates.values()
        )
        if has_pending and not self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.start()
        elif not has_pending and self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()

    def _flush_probe_updates(self, limit: int = 100):
        if not self._pending_probe_updates:
            if self._probe_flush_timer.isActive():
                self._probe_flush_timer.stop()
            return

        per_media: dict[str, list[tuple[str, dict | None]]] = {}
        flush_start = perf_counter()
        target_limit = limit if limit > 0 else 150
        count = 0

        for path, (media_type, probe_info) in list(self._pending_probe_updates.items()):
            per_media.setdefault(media_type, []).append((path, probe_info))
            del self._pending_probe_updates[path]

            count += 1
            if count >= target_limit:
                break
            elapsed_ms = (perf_counter() - flush_start) * 1000
            if elapsed_ms >= 16:
                break

        for media_type, updates in per_media.items():
            panel = self._panels.get(media_type)
            if panel:
                panel.update_probes(updates)

        if not self._pending_probe_updates and self._probe_flush_timer.isActive():
            self._probe_flush_timer.stop()

    def _flush_failed_rows(self, limit: int = 250):
        if not self._pending_failed_rows:
            if self._failed_flush_timer.isActive():
                self._failed_flush_timer.stop()
            return

        take = len(self._pending_failed_rows) if limit == 0 else min(limit, len(self._pending_failed_rows))
        batch = self._pending_failed_rows[:take]
        del self._pending_failed_rows[:take]
        self._failed_panel.add_failures(batch)

        if not self._pending_failed_rows and self._failed_flush_timer.isActive():
            self._failed_flush_timer.stop()

    def _update_metadata_status(self):
        if self._scan_state != ScanState.METADATA or self._worker_finished:
            return
        total = max(1, self._metadata_total or self._discovery_count or self._metadata_processed)
        processed = min(self._metadata_processed, total)
        percent = int((processed / total) * 100)
        self._status_bar.showMessage(
            f"Metadata… {processed}/{total} ({percent}%) | {self._scan_timing_text(processed, total)}"
        )

    def _refresh_scan_status(self):
        if self._scan_state == ScanState.IDLE:
            return
        if self._cancel_requested:
            self._status_bar.showMessage(f"Cancelling scan… {self._scan_timing_text()}")
            return
        if self._scan_state == ScanState.QUICKSCAN:
            discovered = max(self._discovery_progress_count, self._total_found)
            self._status_bar.showMessage(
                f"Scanning… {discovered} files found so far | {self._scan_timing_text()}"
            )
            return
        self._update_metadata_status()

    def _try_finalize_scan(self):
        if not (self._discovery_finished and self._worker_finished):
            return

        if self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()
        if self._probe_flush_timer.isActive():
            self._probe_flush_timer.stop()
        if self._failed_flush_timer.isActive():
            self._failed_flush_timer.stop()

        self._flush_metadata_rows(limit=0)
        self._flush_probe_updates(limit=0)
        self._flush_failed_rows(limit=0)

        for media_type in {"Videos", "Audio"}:
            panel = self._panels.get(media_type)
            if panel:
                panel.end_probe_updates()

        self._finalize_scan(self._discovery_count, self._worker_probed_count)

    def _source_root_for_path(self, path: str) -> str:
        normalized = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        for root in self._active_source_roots:
            if normalized == root or normalized.startswith(root + os.sep):
                return root
        return self._active_source_roots[0] if self._active_source_roots else normalized

    def _finalize_scan(self, discovered_count: int, probed_count: int):
        scan_started_at = self._scan_started_at_epoch if self._scan_started_at_epoch is not None else int(time.time())
        pruned = self._scan_store.prune_scan_scope(self._active_source_roots, scan_started_at)
        elapsed_text = self._format_duration(self._scan_elapsed_seconds())

        self._scan_state = ScanState.IDLE
        self._cancel_requested = False
        if self._status_timer.isActive():
            self._status_timer.stop()
        self._sources_panel.set_scanning(False)
        for panel in self._panels.values():
            panel.set_scan_locked(False)
        self._failed_panel.set_scan_locked(False)

        noun = "file" if discovered_count == 1 else "files"
        failed_count = self._failed_panel.file_count()
        cancelled_text = " (cancelled)" if self._worker_cancelled else ""
        self._status_bar.showMessage(
            f"Scan complete{cancelled_text} — {discovered_count} {noun} found, "
            f"{probed_count} probed, {failed_count} failed, {pruned} stale removed. "
            f"Time: {elapsed_text}."
        )
        self._scan_started_at = None
        self._scan_started_at_epoch = None

    def _on_conversion_status_changed(self, message: str, active: bool):
        if active:
            self._status_bar.showMessage(message)
            return

        self._status_bar.showMessage(message or "Ready. Add folders and click Scan.")

    def closeEvent(self, event):
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()
        if self._metadata_probe_worker and self._metadata_probe_worker.isRunning():
            self._metadata_probe_worker.cancel()
            self._metadata_probe_worker.wait()
        if self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()
        if self._probe_flush_timer.isActive():
            self._probe_flush_timer.stop()
        if self._failed_flush_timer.isActive():
            self._failed_flush_timer.stop()
        if self._status_timer.isActive():
            self._status_timer.stop()
        self._scan_store.close()
        super().closeEvent(event)
