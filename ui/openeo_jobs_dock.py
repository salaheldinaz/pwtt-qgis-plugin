# -*- coding: utf-8 -*-
"""openEO jobs dock: list remote batch jobs and download results."""

import os
import threading

from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QWidget,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsProject

from .backend_auth import create_and_auth_backend, ensure_footprint_dependencies

class PWTTOpenEOJobsDock(QDockWidget):
    """List openEO batch jobs from the server and download/add results."""

    _jobs_loaded = pyqtSignal(list)  # emitted from worker thread
    _log_signal = pyqtSignal(str)   # thread-safe log append

    def __init__(self, parent=None, plugin_dir=None):
        # Short dock title (no PWTT/version suffix); server job title is in the Job ID tooltip.
        super().__init__("PWTT \u2014 openEO Jobs", parent)
        self.setObjectName("PWTTOpenEOJobsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self._conn = None  # openEO connection (set after auth)
        self._remote_jobs = []  # list of job metadata dicts
        self._build_ui()
        self._jobs_loaded.connect(self._populate_table)
        self._log_signal.connect(self._append_log)

    def _build_ui(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Top bar: refresh
        top_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Connect && Refresh")
        self.refresh_btn.setToolTip("Authenticate to openEO and list all batch jobs")
        self.refresh_btn.clicked.connect(self._refresh_jobs)
        top_row.addWidget(self.refresh_btn)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray; font-size: 0.9em;")
        top_row.addWidget(self.status_label, 1)
        layout.addLayout(top_row)

        # Job table
        self.job_table = QTableWidget(0, 5)
        self.job_table.setHorizontalHeaderLabels(
            ["Job ID", "Status", "Progress", "Created", "Updated"]
        )
        self.job_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.job_table.setSelectionMode(QTableWidget.SingleSelection)
        self.job_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.job_table.verticalHeader().hide()
        hdr = self.job_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.job_table)

        # Action buttons
        btn_row = QHBoxLayout()
        self.download_btn = QPushButton("Download && Add to Map")
        self.download_btn.setEnabled(False)
        self.download_btn.setToolTip("Download result GeoTIFF and add as layer")
        self.download_btn.clicked.connect(self._download_selected)
        btn_row.addWidget(self.download_btn)
        self.footprints_btn = QPushButton("Per-building (OSM)")
        self.footprints_btn.setEnabled(False)
        self.footprints_btn.setToolTip(
            "After the GeoTIFF is downloaded: fetch OSM buildings and mean damage (T-stat) per polygon"
        )
        self.footprints_btn.clicked.connect(self._footprints_for_selected)
        btn_row.addWidget(self.footprints_btn)
        self.logs_btn = QPushButton("Show Logs")
        self.logs_btn.setEnabled(False)
        self.logs_btn.setToolTip("Fetch and show server-side logs for selected job")
        self.logs_btn.clicked.connect(self._show_selected_logs)
        btn_row.addWidget(self.logs_btn)
        self.delete_remote_btn = QPushButton("Delete Remote Job")
        self.delete_remote_btn.setEnabled(False)
        self.delete_remote_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.delete_remote_btn)
        layout.addLayout(btn_row)

        # Log
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(140)
        layout.addWidget(self.log_text)

        self.job_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.setWidget(w)

    def _append_log(self, msg):
        self.log_text.append(msg)

    # ── Auth & refresh ───────────────────────────────────────────────────────

    def _refresh_jobs(self):
        """Authenticate (if needed) and list all openEO batch jobs."""
        if not self._conn:
            try:
                backend = create_and_auth_backend(
                    "openeo",
                    parent=self,
                    controls_dock=getattr(self, "controls_dock", None),
                )
                self._conn = backend._conn
                self.log_text.append("Connected to openEO CDSE.")
            except RuntimeError as e:
                if str(e) != "Authentication cancelled.":
                    QMessageBox.warning(self, "PWTT", str(e))
                return
            except Exception as e:
                QMessageBox.warning(self, "PWTT", f"openEO auth failed: {e}")
                return

        self.status_label.setText("Loading jobs\u2026")
        self.refresh_btn.setEnabled(False)
        self.log_text.append("Fetching job list from server…")

        def _worker():
            try:
                jobs = self._conn.list_jobs()
                result = []
                for j in jobs:
                    result.append({
                        "id": j.get("id", ""),
                        "status": j.get("status", ""),
                        "created": j.get("created", ""),
                        "updated": j.get("updated", ""),
                        "title": j.get("title", ""),
                        "progress": j.get("progress"),
                        "costs": j.get("costs"),
                        "usage": j.get("usage"),
                    })
                self._log_signal.emit(f"Received {len(result)} job(s) from server.")
                self._jobs_loaded.emit(result)
            except Exception as e:
                self._log_signal.emit(f"Failed to list jobs: {e}")
                self._jobs_loaded.emit([])

        threading.Thread(target=_worker, daemon=True).start()

    def _populate_table(self, jobs):
        self._remote_jobs = jobs
        self.job_table.setRowCount(len(jobs))

        _color_map = {
            "finished": "#4CAF50",
            "running": "#2196F3",
            "queued": "#FF9800",
            "created": "#888",
            "error": "#F44336",
            "canceled": "#888",
            "cancelled": "#888",
        }

        for row, j in enumerate(jobs):
            # Job ID
            jid = j["id"]
            display_id = jid[-16:] if len(jid) > 16 else jid
            id_item = QTableWidgetItem(display_id)
            # Build rich tooltip
            tip_parts = [f"ID: {jid}"]
            if j.get("title"):
                tip_parts.append(f"Title: {j['title']}")
            if j.get("costs") is not None:
                tip_parts.append(f"Costs: {j['costs']}")
            usage = j.get("usage")
            if usage and isinstance(usage, dict):
                for k, v in usage.items():
                    tip_parts.append(f"{k}: {v}")
            id_item.setToolTip("\n".join(tip_parts))
            id_item.setData(Qt.UserRole, jid)
            self.job_table.setItem(row, 0, id_item)

            # Status
            st_item = QTableWidgetItem(j["status"])
            color = _color_map.get(j["status"], "#000")
            st_item.setForeground(QColor(color))
            self.job_table.setItem(row, 1, st_item)

            # Progress
            progress = j.get("progress")
            prog_text = f"{progress}%" if progress is not None else ""
            self.job_table.setItem(row, 2, QTableWidgetItem(prog_text))

            # Created
            created = (j.get("created") or "")[:19].replace("T", " ")
            self.job_table.setItem(row, 3, QTableWidgetItem(created))

            # Updated
            updated = (j.get("updated") or "")[:19].replace("T", " ")
            self.job_table.setItem(row, 4, QTableWidgetItem(updated))

        # Summary in status bar
        by_status = {}
        for j in jobs:
            st = j.get("status", "?")
            by_status[st] = by_status.get(st, 0) + 1
        summary = ", ".join(f"{cnt} {st}" for st, cnt in sorted(by_status.items()))
        self.status_label.setText(f"{len(jobs)} job(s)" + (f" ({summary})" if summary else ""))
        self.refresh_btn.setEnabled(True)

    # ── Selection ────────────────────────────────────────────────────────────

    def _on_selection_changed(self):
        row = self.job_table.currentRow()
        if row < 0 or row >= len(self._remote_jobs):
            self.download_btn.setEnabled(False)
            self.footprints_btn.setEnabled(False)
            self.logs_btn.setEnabled(False)
            self.delete_remote_btn.setEnabled(False)
            return
        j = self._remote_jobs[row]
        self.download_btn.setEnabled(j["status"] == "finished")
        self.footprints_btn.setEnabled(j["status"] == "finished")
        self.logs_btn.setEnabled(True)
        self.delete_remote_btn.setEnabled(j["status"] not in ("running", "queued"))

    def _get_selected_remote_id(self):
        row = self.job_table.currentRow()
        if row < 0:
            return None
        item = self.job_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _remote_job_local_paths(self, job_id):
        """Same layout as download: ``<project_or_home>/PWTT/<job_id>/pwtt_<job_id>.tif``."""
        proj_path = QgsProject.instance().absolutePath()
        base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
        out_dir = os.path.join(base_dir, job_id)
        tif_path = os.path.join(out_dir, f"pwtt_{job_id}.tif")
        gpkg_path = os.path.join(out_dir, f"pwtt_{job_id}_footprints.gpkg")
        return out_dir, tif_path, gpkg_path

    # ── Show Logs ─────────────────────────────────────────────────────────────

    def _show_selected_logs(self):
        """Fetch and display server-side logs for the selected openEO job."""
        job_id = self._get_selected_remote_id()
        if not job_id or not self._conn:
            return
        self.logs_btn.setEnabled(False)
        self.log_text.append(f"Fetching logs for {job_id}…")

        def _worker():
            try:
                job = self._conn.job(job_id)
                # Also fetch job metadata
                try:
                    info = job.describe()
                    parts = []
                    for key in ("id", "status", "created", "updated", "progress"):
                        val = info.get(key)
                        if val is not None and val != "":
                            parts.append(f"{key}={val}")
                    usage = info.get("usage")
                    if usage and isinstance(usage, dict):
                        for k, v in usage.items():
                            parts.append(f"{k}={v}")
                    costs = info.get("costs")
                    if costs is not None:
                        parts.append(f"costs={costs}")
                    if parts:
                        self._log_signal.emit(f"<b>Job info:</b> {', '.join(parts)}")
                except Exception:
                    pass

                logs = job.logs()
                entries = list(logs) if logs else []
                if not entries:
                    self._log_signal.emit(f"No log entries for {job_id}.")
                else:
                    self._log_signal.emit(f"<b>{len(entries)} log entries for {job_id}:</b>")
                    for entry in entries[-30:]:  # last 30
                        if isinstance(entry, dict):
                            lvl = entry.get("level", "info")
                            msg = entry.get("message", "")
                            eid = entry.get("id", "")
                        else:
                            lvl, msg, eid = "info", str(entry), ""
                        if not msg:
                            continue
                        color = {"error": "#F44336", "warning": "#FF9800"}.get(lvl, "#666")
                        prefix = f"[{eid}] " if eid else ""
                        self._log_signal.emit(
                            f'<span style="color:{color}">[{lvl}]</span> {prefix}{msg}'
                        )
            except Exception as e:
                self._log_signal.emit(f"Failed to fetch logs: {e}")

        def _done():
            self.logs_btn.setEnabled(True)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        # Re-enable button after thread finishes
        _timer = QTimer(self)
        _timer.timeout.connect(lambda: (not t.is_alive()) and (_timer.stop(), _done()))
        _timer.start(500)

    # ── Download ─────────────────────────────────────────────────────────────

    def _download_selected(self):
        """Download result of the selected openEO job and add to QGIS layers."""
        job_id = self._get_selected_remote_id()
        if not job_id or not self._conn:
            return

        out_dir, out_path, _gpkg_unused = self._remote_job_local_paths(job_id)
        os.makedirs(out_dir, exist_ok=True)

        if os.path.isfile(out_path):
            reply = QMessageBox.question(
                self, "PWTT",
                f"File already exists:\n{out_path}\n\nAdd existing file to map instead of re-downloading?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                self._add_tif_to_map(out_path, job_id)
                return

        self.download_btn.setEnabled(False)
        self.log_text.append(f"Downloading {job_id}…")
        last_err = []

        def _worker():
            from ..core.openeo_backend import download_job_geotiff

            try:
                job = self._conn.job(job_id)
                results = job.get_results()
                # Log result metadata
                try:
                    meta = results.get_metadata()
                    bbox = meta.get("bbox")
                    assets = meta.get("assets", {})
                    parts = [f"{len(assets)} asset(s)"]
                    if bbox:
                        parts.append(f"bbox={bbox}")
                    for name, info in assets.items():
                        ftype = info.get("type", "")
                        parts.append(f"{name} ({ftype})")
                    self._log_signal.emit(f"Result metadata: {', '.join(parts)}")
                except Exception:
                    pass
                download_job_geotiff(results, out_path, out_dir)
                size_mb = os.path.getsize(out_path) / (1024 * 1024) if os.path.isfile(out_path) else 0
                self._log_signal.emit(f"Downloaded {size_mb:.1f} MB to {out_path}")
            except Exception as e:
                last_err.append(str(e))
                self._log_signal.emit(f"Download error: {e}")

        def _check_done():
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                timer.stop()
                self._add_tif_to_map(out_path, job_id)
                self.download_btn.setEnabled(True)
            elif not t.is_alive():
                timer.stop()
                detail = f" {last_err[-1]}" if last_err else ""
                self.log_text.append(f"Download failed.{detail}")
                self.download_btn.setEnabled(True)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        timer = QTimer(self)
        timer.timeout.connect(_check_done)
        timer.start(1000)

    def _add_tif_to_map(self, path, job_id):
        """Add a GeoTIFF to QGIS layers."""
        from qgis.core import QgsProject, QgsRasterLayer

        from ..core.qgis_layer_tree import (
            add_map_layer_to_pwtt_job_group,
            pwtt_damage_layer_name,
        )
        from ..core.qgis_output_style import damage_threshold_from_job_meta, style_pwtt_raster_layer

        backend_id = "openeo"
        label = pwtt_damage_layer_name(job_id, backend_id)
        layer = QgsRasterLayer(path, label, "gdal")
        if layer.isValid():
            thr = damage_threshold_from_job_meta(path)
            style_pwtt_raster_layer(layer, damage_threshold=thr)
            add_map_layer_to_pwtt_job_group(QgsProject.instance(), layer, job_id, backend_id)
            self.log_text.append(f"Layer added: {label}")
        else:
            self.log_text.append("Failed to load layer \u2014 file may be invalid.")

    def _footprints_for_selected(self):
        """OSM buildings + zonal mean T-stat for the downloaded openEO result raster."""
        job_id = self._get_selected_remote_id()
        if not job_id:
            return
        out_dir, tif_path, gpkg_path = self._remote_job_local_paths(job_id)
        if not os.path.isfile(tif_path) or os.path.getsize(tif_path) == 0:
            QMessageBox.information(
                self, "PWTT",
                "Download the result GeoTIFF first using \u201cDownload && Add to Map\u201d.",
            )
            return
        if not ensure_footprint_dependencies(self):
            return
        from ..core.utils import raster_bounds_to_aoi_wkt

        aoi_wkt = raster_bounds_to_aoi_wkt(tif_path)
        if not aoi_wkt:
            QMessageBox.warning(
                self, "PWTT",
                "Could not read raster extent (missing CRS or rasterio unavailable).",
            )
            return
        if os.path.isfile(gpkg_path) and os.path.getsize(gpkg_path) > 0:
            reply = QMessageBox.question(
                self, "PWTT",
                f"Footprints file already exists:\n{gpkg_path}\n\nOverwrite and recompute?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.No:
                self._add_footprints_layer(gpkg_path, job_id)
                return

        os.makedirs(out_dir, exist_ok=True)
        self.footprints_btn.setEnabled(False)
        self.log_text.append(f"Computing OSM building footprints for {job_id}\u2026")
        last_err = []
        done = []

        def _worker():
            try:
                from ..core.footprints import compute_footprints

                def _prog(pct, msg):
                    self._log_signal.emit(f"[{pct}%] {msg}")

                compute_footprints(
                    tif_path,
                    aoi_wkt,
                    gpkg_path,
                    progress_callback=_prog,
                )
                done.append(True)
            except Exception as e:
                last_err.append(str(e))
                self._log_signal.emit(f"Footprints error: {e}")

        def _check_done():
            if t.is_alive():
                return
            timer.stop()
            self.footprints_btn.setEnabled(True)
            if last_err:
                return
            if done and os.path.isfile(gpkg_path) and os.path.getsize(gpkg_path) > 0:
                self.log_text.append(f"Footprints saved: {gpkg_path}")
                self._add_footprints_layer(gpkg_path, job_id)
            else:
                self.log_text.append("Footprints step finished but output file is missing.")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        timer = QTimer(self)
        timer.timeout.connect(_check_done)
        timer.start(400)

    def _add_footprints_layer(self, path, job_id):
        from qgis.core import QgsProject, QgsVectorLayer

        from ..core.qgis_layer_tree import (
            add_map_layer_to_pwtt_job_group,
            pwtt_footprints_layer_name,
        )
        from ..core.qgis_output_style import style_pwtt_footprints_layer

        backend_id = "openeo"
        label = pwtt_footprints_layer_name(job_id, backend_id, "current_osm")
        layer = QgsVectorLayer(path, label, "ogr")
        if layer.isValid():
            style_pwtt_footprints_layer(layer)
            add_map_layer_to_pwtt_job_group(QgsProject.instance(), layer, job_id, backend_id)
            self.log_text.append(f"Layer added: {label}")
        else:
            self.log_text.append("Failed to load footprints GeoPackage.")

    # ── Delete remote ────────────────────────────────────────────────────────

    def _delete_selected(self):
        job_id = self._get_selected_remote_id()
        if not job_id or not self._conn:
            return
        reply = QMessageBox.question(
            self, "PWTT",
            f"Delete remote openEO job {job_id}?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            job = self._conn.job(job_id)
            status = job.status()
            job.delete()
            self.log_text.append(f"Deleted remote job {job_id} (was {status})")
            self._refresh_jobs()
        except Exception as e:
            self.log_text.append(f"Delete failed: {e}")
            QMessageBox.warning(self, "PWTT", f"Failed to delete: {e}")


