# -*- coding: utf-8 -*-
"""Jobs dock: job list, actions, progress, log, order polling."""

import glob
import os
import threading

from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QWidget,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsApplication, QgsProject, QgsSettings

from .backend_auth import create_and_auth_backend, ensure_footprint_dependencies
from .dock_common import STATUS_COLORS, STATUS_LABELS, dock_title, job_footprints_sources

class PWTTJobsDock(QDockWidget):
    """Dockable jobs panel: job table, action buttons, progress bar, and log."""

    # Thread-safe bridge for status messages from background tasks
    _status_signal = pyqtSignal(str, str)   # (job_id, message)
    # Signal to auto-resume a job on the main thread
    _auto_resume_signal = pyqtSignal(str)   # job_id
    # Thread-safe: append a line to a job log (e.g. CDSE poll from background thread)
    _order_poll_log = pyqtSignal(str, str)  # (job_id, message)
    # Emitted after the job table is refreshed (e.g. GRD staging dock sync)
    jobs_changed = pyqtSignal()

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(dock_title("PWTT \u2014 Jobs", plugin_dir), parent)
        self.setObjectName("PWTTJobsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        self._active_tasks = {}    # job_id -> PWTTRunTask
        self._job_logs = {}        # job_id -> [str]
        self._job_progress = {}    # job_id -> int (0-100)
        self._poll_running = False
        self.controls_dock = None  # set after construction by plugin

        self._build_ui()

        self._status_signal.connect(self._handle_status_message)
        self._auto_resume_signal.connect(self._auto_resume_job)
        self._order_poll_log.connect(self._append_order_poll_log)

        # Order-polling timer (checks every 2 min)
        self._order_timer = QTimer(self)
        self._order_timer.timeout.connect(self._poll_orders)
        self._order_timer.start(120_000)

        # Recover stale jobs and populate table
        from ..core import job_store
        job_store.recover_stale_jobs()
        self._refresh_job_list()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Job table
        self.job_table = QTableWidget(0, 7)
        self.job_table.setHorizontalHeaderLabels(
            ["Status", "Backend", "Remote Job", "Local ID", "Output folder", "Dates", "Created"]
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
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.job_table.setMaximumHeight(180)
        self.job_table.itemSelectionChanged.connect(self._on_job_selected)
        layout.addWidget(self.job_table)

        # Action buttons
        btn_row = QHBoxLayout()
        self.load_btn = QPushButton("Load parameters")
        self.load_local_btn = QPushButton("Load Local")
        self.apply_style_btn = QPushButton("Apply styling")
        self.footprints_btn = QPushButton("Per-building (OSM)")
        self.resume_btn = QPushButton("Resume")
        self.stop_btn = QPushButton("Stop")
        self.cancel_btn = QPushButton("Cancel")
        self.rerun_btn = QPushButton("Rerun")
        self.delete_btn = QPushButton("Delete")
        for btn in (self.load_btn, self.load_local_btn, self.apply_style_btn, self.footprints_btn, self.resume_btn, self.stop_btn,
                     self.cancel_btn, self.rerun_btn, self.delete_btn):
            btn.setEnabled(False)
            btn_row.addWidget(btn)
        self.load_btn.setToolTip("Load job AOI to map and parameters to controls panel")
        self.load_local_btn.setToolTip(
            "If the result GeoTIFF (and footprints) exist on disk, add them to the map"
        )
        self.apply_style_btn.setToolTip(
            "Re-apply PWTT band-1 pseudocolor (3\u20135) and layer opacity to this job\u2019s "
            "result raster already in the project (matches layer name or GeoTIFF path)"
        )
        self.footprints_btn.setToolTip(
            "Fetch OSM buildings and mean damage (T-stat) per polygon using the job\u2019s result GeoTIFF"
        )
        self.load_btn.clicked.connect(self._load_selected)
        self.load_local_btn.clicked.connect(self._load_local_selected)
        self.apply_style_btn.clicked.connect(self._apply_styling_to_result_selected)
        self.footprints_btn.clicked.connect(self._footprints_for_local_selected)
        self.resume_btn.clicked.connect(self._resume_selected)
        self.stop_btn.clicked.connect(self._stop_selected)
        self.cancel_btn.clicked.connect(self._cancel_selected)
        self.rerun_btn.clicked.connect(self._rerun_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        layout.addLayout(btn_row)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Log
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.WidgetWidth)
        layout.addWidget(self.log_text)

        self.setWidget(w)

    # ── Table helpers ─────────────────────────────────────────────────────────

    def _refresh_job_list(self):
        from ..core import job_store
        jobs = job_store.load_jobs()

        selected_id = self._get_selected_job_id()
        self.job_table.setRowCount(len(jobs))

        for row, job in enumerate(jobs):
            st = job["status"]

            # Status cell (carries job id)
            item = QTableWidgetItem(STATUS_LABELS.get(st, st))
            item.setForeground(QColor(STATUS_COLORS.get(st, "#000")))
            item.setData(Qt.UserRole, job["id"])
            self.job_table.setItem(row, 0, item)

            # Backend
            bname = {"openeo": "openEO", "gee": "GEE", "local": "Local"}.get(
                job["backend_id"], job["backend_id"]
            )
            self.job_table.setItem(row, 1, QTableWidgetItem(bname))

            # Remote Job ID (e.g. openEO batch job id)
            remote_id = job.get("remote_job_id") or ""
            # Show truncated id for readability
            display_id = remote_id[-12:] if len(remote_id) > 12 else remote_id
            rid_item = QTableWidgetItem(display_id)
            rid_item.setToolTip(remote_id)  # full id on hover
            self.job_table.setItem(row, 2, rid_item)

            # Local job id (plugin UUID)
            lid = job["id"]
            lid_disp = lid[:8] + "\u2026" if len(lid) > 10 else lid
            lid_item = QTableWidgetItem(lid_disp)
            lid_item.setToolTip(lid)
            self.job_table.setItem(row, 3, lid_item)

            # Output directory
            out_dir = job.get("output_dir") or ""
            odisp = out_dir
            if len(out_dir) > 48:
                odisp = "\u2026" + out_dir[-47:]
            od_item = QTableWidgetItem(odisp)
            od_item.setToolTip(out_dir)
            self.job_table.setItem(row, 4, od_item)

            # Dates
            dates = f"{job['war_start'][:7]} \u2192 {job['inference_start'][:7]}"
            self.job_table.setItem(row, 5, QTableWidgetItem(dates))

            # Created
            created = job.get("created_at", "")[:16].replace("T", " ")
            self.job_table.setItem(row, 6, QTableWidgetItem(created))

        # Restore selection
        if selected_id:
            for row in range(self.job_table.rowCount()):
                item = self.job_table.item(row, 0)
                if item and item.data(Qt.UserRole) == selected_id:
                    self.job_table.setCurrentCell(row, 0)
                    return
        # Auto-select first row if nothing selected
        if self.job_table.rowCount() > 0 and self.job_table.currentRow() < 0:
            self.job_table.setCurrentCell(0, 0)

        self.jobs_changed.emit()

    def _get_selected_job_id(self):
        row = self.job_table.currentRow()
        if row < 0:
            return None
        item = self.job_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _get_selected_job(self):
        jid = self._get_selected_job_id()
        if not jid:
            return None
        from ..core import job_store
        return job_store.get_job(jid)

    def _local_result_tif_path(self, job):
        """Path to the job\u2019s result GeoTIFF if it exists on disk, else None."""
        if not job:
            return None
        jid = job["id"]
        out_dir = (job.get("output_dir") or "").strip()
        for cand in (
            job.get("output_tif"),
            os.path.join(out_dir, f"pwtt_{jid}.tif") if out_dir else None,
        ):
            if cand and os.path.isfile(cand) and os.path.getsize(cand) > 0:
                return cand
        return None

    def _select_job(self, job_id):
        for row in range(self.job_table.rowCount()):
            item = self.job_table.item(row, 0)
            if item and item.data(Qt.UserRole) == job_id:
                self.job_table.setCurrentCell(row, 0)
                return

    def focus_job(self, job_id):
        """Show the jobs dock and select *job_id* in the table."""
        self.show()
        self.raise_()
        self._select_job(job_id)
        self._on_job_selected()

    def resume_job_by_id(self, job_id):
        """Resume a job by id (used from GRD staging dock)."""
        from ..core import job_store
        job = job_store.get_job(job_id)
        if not job:
            QMessageBox.warning(self, "PWTT", "Job not found.")
            return
        if job_id in self._active_tasks:
            QMessageBox.information(self, "PWTT", "This job is already running.")
            return
        if job["status"] not in job_store.RESUMABLE_STATUSES:
            QMessageBox.information(
                self, "PWTT", "This job is not in a resumable state."
            )
            return
        self._select_job(job_id)
        self._resume_selected()

    def _on_job_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            for btn in (self.load_btn, self.load_local_btn, self.apply_style_btn, self.footprints_btn, self.resume_btn, self.stop_btn,
                         self.cancel_btn, self.rerun_btn, self.delete_btn):
                btn.setEnabled(False)
            self.log_text.clear()
            self.progress_bar.setValue(0)
            return

        st = job["status"]
        self.load_btn.setEnabled(True)
        self.load_local_btn.setEnabled(True)
        self.apply_style_btn.setEnabled(True)
        self.footprints_btn.setEnabled(self._local_result_tif_path(job) is not None)
        self.resume_btn.setEnabled(st in job_store.RESUMABLE_STATUSES)
        self.stop_btn.setEnabled(st == job_store.STATUS_RUNNING)
        self.cancel_btn.setEnabled(
            st in (job_store.STATUS_RUNNING, job_store.STATUS_WAITING_ORDERS,
                   job_store.STATUS_STOPPED)
        )
        self.rerun_btn.setEnabled(True)
        self.delete_btn.setEnabled(st != job_store.STATUS_RUNNING)

        self.resume_btn.setText(
            "Check && Resume" if st == job_store.STATUS_WAITING_ORDERS else "Resume"
        )

        # Log
        self.log_text.clear()
        for msg in self._job_logs.get(job["id"], []):
            self.log_text.append(msg)

        # Progress
        if st == job_store.STATUS_COMPLETED:
            self.progress_bar.setValue(100)
        elif job["id"] in self._job_progress:
            self.progress_bar.setValue(self._job_progress[job["id"]])
        else:
            self.progress_bar.setValue(0)

    # ── Launch / lifecycle ────────────────────────────────────────────────────

    def launch_job(self, job, backend):
        """Start a task for the given job. Called from controls dock or resume."""
        if job["id"] in self._active_tasks:
            return
        from ..core.pwtt_task import PWTTRunTask
        from ..core import job_store

        job["status"] = job_store.STATUS_RUNNING
        job["error"] = None
        job_store.save_job(job)

        fp_sources = job_footprints_sources(job)
        task = PWTTRunTask(
            backend=backend,
            aoi_wkt=job["aoi_wkt"],
            war_start=job["war_start"],
            inference_start=job["inference_start"],
            pre_interval=job["pre_interval"],
            post_interval=job["post_interval"],
            output_dir=job["output_dir"],
            include_footprints=bool(fp_sources),
            footprints_sources=fp_sources,
            job_id=job["id"],
            remote_job_id=job.get("remote_job_id"),
            damage_threshold=job.get("damage_threshold", 3.3),
            gee_viz=job.get("gee_viz", False),
        )

        job_id = job["id"]
        self._active_tasks[job_id] = task
        bname = {"openeo": "openEO", "gee": "GEE", "local": "Local"}.get(
            job["backend_id"], job["backend_id"]
        )
        remote_id = job.get("remote_job_id")
        log_parts = [f"Task started — backend: {bname}"]
        if remote_id:
            log_parts.append(f"remote job: {remote_id}")
        log_parts.append(f"dates: {job['war_start']} → {job['inference_start']}")
        log_parts.append(f"pre: {job['pre_interval']}mo, post: {job['post_interval']}mo")
        log_parts.append(f"output: {job['output_dir']}")
        self._job_logs.setdefault(job_id, []).append("<br>".join(log_parts))
        self._job_progress[job_id] = 0

        task.taskCompleted.connect(lambda _jid=job_id: self._on_task_completed(_jid))
        task.taskTerminated.connect(lambda _jid=job_id: self._on_task_terminated(_jid))
        if hasattr(task, "progressChanged"):
            task.progressChanged.connect(
                lambda v, _jid=job_id: self._on_task_progress(_jid, v)
            )
        task.on_status_message(
            lambda msg, _jid=job_id: self._status_signal.emit(_jid, msg)
        )

        QgsApplication.taskManager().addTask(task)

        self._refresh_job_list()
        self._select_job(job_id)
        self.show()
        self.raise_()

    # ── Task callbacks (main thread) ──────────────────────────────────────────

    def _append_order_poll_log(self, job_id, msg):
        self._job_logs.setdefault(job_id, []).append(msg)
        if self._get_selected_job_id() == job_id:
            self.log_text.append(msg)

    def _handle_status_message(self, job_id, msg):
        self._job_logs.setdefault(job_id, []).append(msg)
        if self._get_selected_job_id() == job_id:
            self.log_text.append(msg)

        # Persist remote job id as soon as the backend sets it
        task = self._active_tasks.get(job_id)
        if task:
            remote_id = getattr(task, "remote_job_id", None)
            if remote_id:
                from ..core import job_store
                existing = job_store.get_job(job_id)
                if existing and existing.get("remote_job_id") != remote_id:
                    job_store.update_job(job_id, remote_job_id=remote_id)
                    note = f"Remote job ID saved: {remote_id}"
                    self._job_logs.setdefault(job_id, []).append(note)
                    if self._get_selected_job_id() == job_id:
                        self.log_text.append(note)
                    self._refresh_job_list()

    def _on_task_progress(self, job_id, value):
        self._job_progress[job_id] = int(value)
        if self._get_selected_job_id() == job_id:
            self.progress_bar.setValue(int(value))

    def _on_task_completed(self, job_id):
        from ..core import job_store
        task = self._active_tasks.pop(job_id, None)
        if not task:
            return

        # GRD offline: run() returned True so QgsTask does not show a failure notification.
        if task.products_offline:
            remote_id = getattr(task, "remote_job_id", None)
            if remote_id:
                job_store.update_job(job_id, remote_job_id=remote_id)
            ids = list(task.offline_product_ids)
            prows = list(getattr(task, "offline_products", []) or [])
            by_id = {
                p["id"]: p
                for p in prows
                if isinstance(p, dict) and p.get("id")
            }
            offline_products = [
                {
                    "id": pid,
                    "name": str(by_id.get(pid, {}).get("name", "")),
                    "date": str(by_id.get(pid, {}).get("date", "")),
                }
                for pid in ids
            ]
            job_store.update_job(
                job_id,
                status=job_store.STATUS_WAITING_ORDERS,
                offline_product_ids=ids,
                offline_products=offline_products,
            )
            ids_str = ", ".join(ids[:5])
            if len(ids) > 5:
                ids_str += f" (+{len(ids) - 5} more)"
            self._job_logs.setdefault(job_id, []).append(
                f"<b>Products offline</b> — staging from cold storage.<br>"
                f"Product IDs: {ids_str}<br>"
                f"Will auto-check every 2 min and resume when available."
            )
            self._job_progress.pop(job_id, None)
            self._refresh_job_list()
            if self._get_selected_job_id() == job_id:
                self._on_job_selected()
            return

        output_tif = getattr(task, "output_tif", None)
        footprints = getattr(task, "footprints_gpkg", None)  # backwards compat (first)
        footprints_gpkgs = getattr(task, "footprints_gpkgs", {}) or {}
        update_fields = dict(
            status=job_store.STATUS_COMPLETED,
            output_tif=output_tif,
            footprints_gpkg=footprints,
            footprints_gpkgs=footprints_gpkgs,
            offline_product_ids=[],
            offline_products=[],
        )
        remote_id = getattr(task, "remote_job_id", None)
        if remote_id:
            update_fields["remote_job_id"] = remote_id
        job_store.update_job(job_id, **update_fields)
        self._job_progress[job_id] = 100
        # Rich completion log
        done_parts = ["<b>Task completed successfully.</b>"]
        if output_tif:
            try:
                size_mb = os.path.getsize(output_tif) / (1024 * 1024)
                done_parts.append(f"Output: {output_tif} ({size_mb:.1f} MB)")
            except OSError:
                done_parts.append(f"Output: {output_tif}")
        for src, fp_path in footprints_gpkgs.items():
            done_parts.append(f"Footprints ({src}): {fp_path}")
        if remote_id:
            done_parts.append(f"Remote job: {remote_id}")
        self._job_logs.setdefault(job_id, []).append("<br>".join(done_parts))
        self._refresh_job_list()
        if self._get_selected_job_id() == job_id:
            self.progress_bar.setValue(100)
            self.log_text.append("<br>".join(done_parts))

    def _on_task_terminated(self, job_id):
        from ..core import job_store
        task = self._active_tasks.pop(job_id, None)
        if not task:
            return

        # Persist remote job id (e.g. openEO) even on failure so we can resume
        remote_id = getattr(task, "remote_job_id", None)
        if remote_id:
            job_store.update_job(job_id, remote_job_id=remote_id)

        if task.isCanceled():
            current = job_store.get_job(job_id)
            if current and current["status"] not in (
                job_store.STATUS_STOPPED, job_store.STATUS_CANCELLED
            ):
                job_store.update_job(job_id, status=job_store.STATUS_CANCELLED)
            msg = "Task was cancelled."
            if remote_id:
                msg += f" Remote job: {remote_id}"
            self._job_logs.setdefault(job_id, []).append(msg)
        elif task.exception:
            job_store.update_job(
                job_id, status=job_store.STATUS_FAILED, error=str(task.exception)
            )
            err_parts = [f"<b>Task failed:</b> {task.exception}"]
            if remote_id:
                err_parts.append(f"Remote job: {remote_id}")
            self._job_logs.setdefault(job_id, []).append("<br>".join(err_parts))
            if task.error_detail:
                self._job_logs[job_id].append(f"<pre>{task.error_detail}</pre>")
        else:
            job_store.update_job(
                job_id, status=job_store.STATUS_FAILED, error="Unknown error"
            )
            self._job_logs.setdefault(job_id, []).append(
                "Task terminated unexpectedly."
            )

        self._refresh_job_list()
        if self._get_selected_job_id() == job_id:
            self._on_job_selected()

    # ── Action handlers ───────────────────────────────────────────────────────

    def _load_selected(self):
        """Load job AOI to map, zoom to it, and fill parameters in controls panel."""
        job = self._get_selected_job()
        if not job:
            return
        if not self.controls_dock:
            return
        self.controls_dock.load_job_params(job)

    def _load_local_selected(self):
        """Add on-disk result raster and footprint layers for the selected job, if they exist."""
        job = self._get_selected_job()
        if not job:
            return
        jid = job["id"]
        out_dir = (job.get("output_dir") or "").strip()

        tif_path = self._local_result_tif_path(job)

        if not tif_path:
            hint = os.path.join(out_dir, f"pwtt_{jid}.tif") if out_dir else "(set output folder)"
            QMessageBox.information(
                self,
                "PWTT",
                "No local result GeoTIFF found for this job.\n\n"
                "Checked the path stored on the job (if any) and:\n"
                f"  {hint}",
            )
            return

        from qgis.core import QgsRasterLayer, QgsVectorLayer

        from ..core.qgis_layer_tree import (
            add_map_layer_to_pwtt_job_group,
            pwtt_damage_layer_name,
            pwtt_footprints_layer_name,
        )
        from ..core.qgis_output_style import (
            damage_threshold_from_job_meta,
            style_pwtt_footprints_layer,
            style_pwtt_raster_layer,
        )

        backend_id = job.get("backend_id")
        project = QgsProject.instance()
        thr = damage_threshold_from_job_meta(
            tif_path, default=float(job.get("damage_threshold", 3.3))
        )

        label = pwtt_damage_layer_name(jid, backend_id)
        rlayer = QgsRasterLayer(tif_path, label, "gdal")
        if not rlayer.isValid():
            QMessageBox.warning(
                self, "PWTT", f"Could not open raster as a QGIS layer:\n{tif_path}"
            )
            return
        style_pwtt_raster_layer(rlayer, damage_threshold=thr)
        add_map_layer_to_pwtt_job_group(project, rlayer, jid, backend_id)
        log_parts = [f"Load Local: added {label}"]

        fp_items = []
        gpkgs = job.get("footprints_gpkgs") or {}
        if isinstance(gpkgs, dict):
            for src, pth in gpkgs.items():
                if pth and os.path.isfile(pth) and os.path.getsize(pth) > 0:
                    fp_items.append((src, pth))
        if not fp_items:
            legacy = job.get("footprints_gpkg")
            if legacy and os.path.isfile(legacy) and os.path.getsize(legacy) > 0:
                fp_items.append((None, legacy))
        if not fp_items and out_dir:
            prefix = f"pwtt_{jid}_footprints_"
            suffix_to_source = {"war": "historical_war_start", "infer": "historical_inference_start"}
            for pth in sorted(glob.glob(os.path.join(glob.escape(out_dir), prefix + "*.gpkg"))):
                base = os.path.basename(pth)
                if not base.startswith(prefix) or not base.endswith(".gpkg"):
                    continue
                suf = base[len(prefix) : -len(".gpkg")]
                src = suffix_to_source.get(suf, suf)
                fp_items.append((src, pth))

        for src, pth in fp_items:
            fp_label = pwtt_footprints_layer_name(
                jid,
                backend_id,
                src,
                war_start=job.get("war_start"),
                inference_start=job.get("inference_start"),
            )
            vl = QgsVectorLayer(pth, fp_label, "ogr")
            if vl.isValid():
                style_pwtt_footprints_layer(vl)
                add_map_layer_to_pwtt_job_group(project, vl, jid, backend_id)
                log_parts.append(f"Load Local: added {fp_label}")
            else:
                log_parts.append(f"Load Local: skipped invalid footprints \u2014 {pth}")

        msg = "<br>".join(log_parts)
        self._job_logs.setdefault(jid, []).append(msg)
        if self._get_selected_job_id() == jid:
            self.log_text.append(msg)

    @staticmethod
    def _raster_source_paths_resolved(layer):
        """Paths to compare to a job GeoTIFF (handles simple GDAL vs URI-ish sources)."""
        src = (layer.source() or "").strip()
        if not src:
            return []
        paths = [src]
        if "|" in src:
            paths.append(src.split("|", 1)[0].strip())
        if src.lower().startswith("gdal:") and '"' in src:
            # e.g. GDAL:"path":band
            for part in src.split('"'):
                p = part.strip()
                if p.endswith(".tif") or p.endswith(".tiff") or os.path.sep in p:
                    paths.append(p)
        out = []
        for p in paths:
            if p and os.path.isfile(p):
                try:
                    out.append(os.path.realpath(p))
                except OSError:
                    out.append(os.path.abspath(p))
        return list(dict.fromkeys(out))

    def _apply_styling_to_result_selected(self):
        """Re-run PWTT symbology on the selected job\u2019s result raster if it is in the map."""
        job = self._get_selected_job()
        if not job:
            return
        jid = job["id"]
        backend_id = job.get("backend_id")
        thr_default = float(job.get("damage_threshold", 3.3))
        tif_path = self._local_result_tif_path(job)
        try:
            tif_resolved = os.path.realpath(tif_path) if tif_path else None
        except OSError:
            tif_resolved = os.path.abspath(tif_path) if tif_path else None

        from qgis.core import QgsRasterLayer

        from ..core.qgis_layer_tree import pwtt_damage_layer_name
        from ..core.qgis_output_style import (
            damage_threshold_from_job_meta,
            style_pwtt_raster_layer,
        )

        expected_name = pwtt_damage_layer_name(jid, backend_id)
        project = QgsProject.instance()
        matched = []
        for _lid, layer in project.mapLayers().items():
            if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
                continue
            layer_paths = self._raster_source_paths_resolved(layer)
            name_ok = layer.name() == expected_name
            path_ok = bool(
                tif_resolved and layer_paths and any(p == tif_resolved for p in layer_paths)
            )
            if not name_ok and not path_ok:
                continue
            meta_tif = tif_path or (layer_paths[0] if layer_paths else (layer.source() or ""))
            thr = damage_threshold_from_job_meta(meta_tif, default=thr_default)
            style_pwtt_raster_layer(layer, damage_threshold=thr)
            matched.append(layer.name())

        if not matched:
            QMessageBox.information(
                self,
                "PWTT",
                "No matching result raster in the project.\n\n"
                f"Expected layer name:\n  {expected_name}\n"
                + (
                    f"\nor GeoTIFF path:\n  {tif_path}"
                    if tif_path
                    else "\n(Set job output folder / run \u201cLoad Local\u201d so the plugin "
                    "knows the GeoTIFF path for matching.)"
                ),
            )
            return

        msg = "Apply styling: updated " + ", ".join(matched)
        self._job_logs.setdefault(jid, []).append(msg)
        if self._get_selected_job_id() == jid:
            self.log_text.append(msg)
        try:
            from qgis.utils import iface as qgis_iface

            if qgis_iface is not None:
                qgis_iface.mapCanvas().refresh()
        except ImportError:
            pass

    def _footprints_for_local_selected(self):
        """OSM buildings + zonal mean T-stat for this job\u2019s on-disk result raster."""
        job = self._get_selected_job()
        if not job:
            return
        jid = job["id"]
        tif_path = self._local_result_tif_path(job)
        if not tif_path:
            hint = os.path.join(
                (job.get("output_dir") or "").strip(), f"pwtt_{jid}.tif"
            ) if (job.get("output_dir") or "").strip() else "(set output folder)"
            QMessageBox.information(
                self,
                "PWTT",
                "No local result GeoTIFF found for this job.\n\n"
                "Run the job or point output to an existing raster, then try again.\n"
                f"Expected e.g.:\n  {hint}",
            )
            return
        out_dir = (job.get("output_dir") or "").strip()
        if not out_dir:
            QMessageBox.warning(self, "PWTT", "Job has no output folder set.")
            return
        gpkg_path = os.path.join(out_dir, f"pwtt_{jid}_footprints_current.gpkg")
        if not ensure_footprint_dependencies(self):
            return

        aoi_wkt = (job.get("aoi_wkt") or "").strip()
        if not aoi_wkt:
            from ..core.utils import raster_bounds_to_aoi_wkt

            aoi_wkt = raster_bounds_to_aoi_wkt(tif_path)
        if not aoi_wkt:
            QMessageBox.warning(
                self, "PWTT",
                "Could not determine AOI (job has no AOI WKT and raster extent could not be read).",
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
                self._add_local_footprints_layer(gpkg_path, jid, job.get("backend_id"))
                return

        os.makedirs(out_dir, exist_ok=True)
        self.footprints_btn.setEnabled(False)
        self._status_signal.emit(jid, f"Computing OSM building footprints for job {jid}\u2026")
        last_err = []
        done = []

        def _worker():
            try:
                from ..core.footprints import compute_footprints

                def _prog(pct, msg):
                    self._status_signal.emit(jid, f"[{pct}%] {msg}")

                compute_footprints(
                    tif_path,
                    aoi_wkt,
                    gpkg_path,
                    progress_callback=_prog,
                )
                done.append(True)
            except Exception as e:
                last_err.append(str(e))
                self._status_signal.emit(jid, f"Footprints error: {e}")

        def _check_done():
            if t.is_alive():
                return
            timer.stop()
            self.footprints_btn.setEnabled(self._local_result_tif_path(self._get_selected_job()) is not None)
            if last_err:
                return
            if done and os.path.isfile(gpkg_path) and os.path.getsize(gpkg_path) > 0:
                from ..core import job_store

                existing = job_store.get_job(jid) or job
                gpkgs = dict(existing.get("footprints_gpkgs") or {})
                gpkgs["current_osm"] = gpkg_path
                legacy = existing.get("footprints_gpkg") or gpkg_path
                job_store.update_job(
                    jid,
                    footprints_gpkgs=gpkgs,
                    footprints_gpkg=legacy,
                )
                self._refresh_job_list()
                note = f"Footprints saved: {gpkg_path}"
                self._job_logs.setdefault(jid, []).append(note)
                if self._get_selected_job_id() == jid:
                    self.log_text.append(note)
                self._add_local_footprints_layer(gpkg_path, jid, job.get("backend_id"))
            else:
                self._status_signal.emit(jid, "Footprints step finished but output file is missing.")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        timer = QTimer(self)
        timer.timeout.connect(_check_done)
        timer.start(400)

    def _add_local_footprints_layer(self, path, job_id, backend_id):
        from qgis.core import QgsProject, QgsVectorLayer

        from ..core.qgis_layer_tree import (
            add_map_layer_to_pwtt_job_group,
            pwtt_footprints_layer_name,
        )
        from ..core.qgis_output_style import style_pwtt_footprints_layer

        job = self._get_selected_job()
        label = pwtt_footprints_layer_name(
            job_id,
            backend_id,
            "current_osm",
            war_start=(job or {}).get("war_start"),
            inference_start=(job or {}).get("inference_start"),
        )
        layer = QgsVectorLayer(path, label, "ogr")
        if layer.isValid():
            style_pwtt_footprints_layer(layer)
            add_map_layer_to_pwtt_job_group(QgsProject.instance(), layer, job_id, backend_id)
            msg = f"Layer added: {label}"
            self._job_logs.setdefault(job_id, []).append(msg)
            if self._get_selected_job_id() == job_id:
                self.log_text.append(msg)
        else:
            msg = "Failed to load footprints GeoPackage."
            self._job_logs.setdefault(job_id, []).append(msg)
            if self._get_selected_job_id() == job_id:
                self.log_text.append(msg)

    def _resume_selected(self):
        job = self._get_selected_job()
        if not job:
            return
        try:
            backend = create_and_auth_backend(
                job["backend_id"],
                parent=self,
                controls_dock=self.controls_dock,
                local_data_source=job.get("data_source")
                if job["backend_id"] == "local"
                else None,
            )
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        remote_id = job.get("remote_job_id")
        prev_status = job.get("status", "?")
        if remote_id:
            self._job_logs.setdefault(job["id"], []).append(
                f"Resuming (was {prev_status}) — remote job: {remote_id}"
            )
        else:
            self._job_logs.setdefault(job["id"], []).append(
                f"Resuming (was {prev_status}) — will create new remote job"
            )
        self.launch_job(job, backend)

    def _stop_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            return
        task = self._active_tasks.get(job["id"])
        if task:
            remote_id = getattr(task, "remote_job_id", None) or job.get("remote_job_id")
            job_store.update_job(job["id"], status=job_store.STATUS_STOPPED)
            task.cancel()
            msg = "Stopping task…"
            if remote_id:
                msg += f" (remote job {remote_id} will continue on server)"
            self._job_logs.setdefault(job["id"], []).append(msg)
        self._refresh_job_list()

    def _cancel_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            return
        remote_id = job.get("remote_job_id")
        job_store.update_job(job["id"], status=job_store.STATUS_CANCELLED)
        task = self._active_tasks.pop(job["id"], None)
        if task:
            task.cancel()
        msg = "Cancelled."
        if remote_id:
            msg += f" Note: remote job {remote_id} may still be running on the server."
        self._job_logs.setdefault(job["id"], []).append(msg)
        self._refresh_job_list()
        if self._get_selected_job_id() == job["id"]:
            self._on_job_selected()

    def _rerun_selected(self):
        """Create a new job with the same parameters and launch it."""
        old = self._get_selected_job()
        if not old:
            return
        try:
            backend = create_and_auth_backend(
                old["backend_id"],
                parent=self,
                controls_dock=self.controls_dock,
                local_data_source=old.get("data_source")
                if old["backend_id"] == "local"
                else None,
            )
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        old_fp_sources = job_footprints_sources(old)
        from ..core import job_store
        new_job = job_store.create_job(
            backend_id=old["backend_id"],
            aoi_wkt=old["aoi_wkt"],
            war_start=old["war_start"],
            inference_start=old["inference_start"],
            pre_interval=old["pre_interval"],
            post_interval=old["post_interval"],
            output_dir="",  # set below
            include_footprints=bool(old_fp_sources),
            footprints_sources=old_fp_sources,
            damage_threshold=old.get("damage_threshold", 3.3),
            gee_viz=old.get("gee_viz", False),
            data_source=old.get("data_source", "cdse"),
        )
        # Derive base dir from old output_dir (old is base/old_id/)
        base_dir = os.path.dirname(old["output_dir"].rstrip("/"))
        if not base_dir:
            proj_path = QgsProject.instance().absolutePath()
            base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
        new_job["output_dir"] = os.path.join(base_dir, new_job["id"])
        os.makedirs(new_job["output_dir"], exist_ok=True)
        job_store.save_job(new_job)
        self.launch_job(new_job, backend)

    def _delete_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job or job["status"] == job_store.STATUS_RUNNING:
            return
        job_store.delete_job(job["id"])
        self._job_logs.pop(job["id"], None)
        self._job_progress.pop(job["id"], None)
        self._refresh_job_list()

    # ── Order polling ─────────────────────────────────────────────────────────

    def _poll_orders(self):
        """Kick off a background thread that checks offline product status."""
        if self._poll_running:
            return
        from ..core import job_store
        waiting = [
            j
            for j in job_store.load_jobs()
            if j["status"] == job_store.STATUS_WAITING_ORDERS
            and j["backend_id"] == "local"
            and j.get("offline_product_ids")
            and (j.get("data_source") or "cdse") == "cdse"
        ]
        if not waiting:
            return
        s = QgsSettings()
        s.beginGroup("PWTT")
        username = s.value("cdse_username", "")
        password = s.value("cdse_password", "")
        s.endGroup()
        if not username or not password:
            return
        self._poll_running = True
        threading.Thread(
            target=self._poll_orders_worker,
            args=(username, password, waiting),
            daemon=True,
        ).start()

    def _poll_orders_worker(self, username, password, waiting_jobs):
        from datetime import datetime

        try:
            from ..core.downloader import get_token, _is_product_online

            token = get_token(username, password)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for job in waiting_jobs:
                jid = job["id"]
                pids = job["offline_product_ids"]
                online_n = sum(
                    1 for pid in pids if _is_product_online(token, pid)
                )
                n = len(pids)
                if online_n >= n:
                    self._order_poll_log.emit(
                        jid,
                        f"[{ts}] Auto-check CDSE API: all {n} product(s) online — resuming.",
                    )
                    self._auto_resume_signal.emit(jid)
                else:
                    self._order_poll_log.emit(
                        jid,
                        f"[{ts}] Auto-check CDSE API: {online_n}/{n} product(s) online; still waiting.",
                    )
        except Exception as e:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for job in waiting_jobs:
                self._order_poll_log.emit(
                    job["id"],
                    f"[{ts}] Auto-check CDSE API failed (will retry on next timer): {e}",
                )
        finally:
            self._poll_running = False

    def _auto_resume_job(self, job_id):
        """Main thread: auto-resume a job whose products are now online."""
        from ..core import job_store
        job = job_store.get_job(job_id)
        if not job or job["status"] != job_store.STATUS_WAITING_ORDERS:
            return
        if job_id in self._active_tasks:
            return
        try:
            backend = create_and_auth_backend(
                job["backend_id"],
                controls_dock=self.controls_dock,
                local_data_source=job.get("data_source")
                if job["backend_id"] == "local"
                else None,
            )
        except Exception:
            return
        self._job_logs.setdefault(job_id, []).append(
            "Products now available \u2014 auto-resuming\u2026"
        )
        self.launch_job(job, backend)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self):
        self._order_timer.stop()

