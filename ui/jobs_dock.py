# -*- coding: utf-8 -*-
"""Jobs dock: job list, actions, progress, log, order polling."""

import glob
import html
import json
import os
import re
from typing import Tuple
import shutil
import threading
from datetime import datetime

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
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QInputDialog,
    QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal, QTimer, QSize, QLocale, QDateTime
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPalette
from qgis.core import QgsApplication, QgsProject, QgsSettings

from .backend_auth import (
    confirm_local_processing_storage,
    create_and_auth_backend,
    ensure_footprint_dependencies,
)
from .dock_common import STATUS_COLORS, STATUS_LABELS, dock_title, job_footprints_sources

from ..core.qgis_layer_tree import job_backend_log_label

# Leading "[YYYY-mm-dd HH:MM:SS] " on persisted activity lines (for color rules after strip).
_PWTT_LOG_TS_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")

_JOBS_IO_FILES_HINT = (
    "Copy the job output folders and any related project files if you want to restore local "
    "result files or check logs on another machine. If you do not copy those files, you will "
    "only have the job parameters."
)


def pwtt_activity_ts_prefix() -> str:
    """Local-time bracket prefix for Jobs / activity log lines."""
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")


def _format_local_datetime(iso_str: str) -> str:
    """Format an ISO datetime string using the device's locale (short format)."""
    if not iso_str:
        return ""
    dt = QDateTime.fromString(iso_str, Qt.ISODate)
    if not dt.isValid():
        return iso_str[:16].replace("T", " ")
    return QLocale.system().toString(dt, QLocale.ShortFormat)


def _jobs_dock_btn_icon(*theme_paths: str, resource_fallback: str = None) -> QIcon:
    """QGIS theme icon if available, else optional ``:/pwtt/...`` resource."""
    for p in theme_paths:
        ic = QgsApplication.getThemeIcon(p)
        if not ic.isNull():
            return ic
    if resource_fallback:
        ic = QIcon(resource_fallback)
        if not ic.isNull():
            return ic
    return QIcon()


_JOBS_BTN_ICON_SIZE = QSize(18, 18)


def _format_bytes_short(n: int) -> str:
    if n <= 0:
        return "\u2014"
    for suffix, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {suffix}"
    return f"{n} B"


def _job_output_size_bytes(job: dict) -> int:
    """Total size of on-disk job output (folder tree or loose artifact files)."""
    out_dir = (job.get("output_dir") or "").strip()
    total = 0
    if out_dir and os.path.isdir(out_dir):
        try:
            for root, _dirs, files in os.walk(out_dir):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    paths = set()
    jid = job.get("id") or ""
    for cand in (job.get("output_tif"), job.get("footprints_gpkg")):
        if cand:
            paths.add(cand)
    for p in (job.get("footprints_gpkgs") or {}).values():
        if p:
            paths.add(p)
    if out_dir and jid:
        paths.add(os.path.join(out_dir, f"pwtt_{jid}.tif"))
    for p in paths:
        if p and os.path.isfile(p):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total


def _job_has_disk_output(job: dict) -> bool:
    out_dir = (job.get("output_dir") or "").strip()
    if out_dir and os.path.isdir(out_dir):
        return True
    return _job_output_size_bytes(job) > 0


def _remove_job_output_from_disk(job: dict) -> None:
    """Remove job output folder or orphaned artifact files (best effort)."""
    out_dir = (job.get("output_dir") or "").strip()
    if out_dir:
        abs_out = os.path.abspath(os.path.expanduser(out_dir))
        if abs_out not in (os.path.abspath(os.sep), os.path.expanduser("~")):
            try:
                if os.path.isdir(abs_out):
                    shutil.rmtree(abs_out, ignore_errors=False)
                    return
            except OSError:
                pass

    paths = set()
    jid = job.get("id") or ""
    for cand in (job.get("output_tif"), job.get("footprints_gpkg")):
        if cand:
            paths.add(cand)
    for p in (job.get("footprints_gpkgs") or {}).values():
        if p:
            paths.add(p)
    if out_dir and jid:
        paths.add(os.path.join(out_dir, f"pwtt_{jid}.tif"))
    for p in paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


class PathRepairDialog(QDialog):
    """Dialog shown after import when imported jobs have broken file paths.

    Offers auto-search (recursive folder scan) and per-job manual browse.
    """

    def __init__(self, broken_jobs, parent=None):
        """
        Parameters
        ----------
        broken_jobs : list of {"job": dict, "broken_fields": list[str]}
        """
        super().__init__(parent)
        self.setWindowTitle("Path Repair")
        self.setMinimumWidth(660)
        self._broken_jobs = broken_jobs
        self._repaired = {}  # job_id -> in-memory copy with repaired paths

        layout = QVBoxLayout(self)

        n = len(broken_jobs)
        layout.addWidget(QLabel(f"{n} imported job(s) have missing file paths."))

        self._auto_search_btn = QPushButton("Auto-search folder…")
        self._auto_search_btn.setToolTip(
            "Recursively scan a folder for matching output files and fill in resolved paths"
        )
        self._auto_search_btn.clicked.connect(self._auto_search)
        layout.addWidget(self._auto_search_btn)

        self._table = QTableWidget(n, 4)
        self._table.setHorizontalHeaderLabels(["Job ID", "Missing files", "Status", "Action"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self._table)

        for row, entry in enumerate(broken_jobs):
            jid = entry["job"].get("id", "?")
            broken_fields = entry["broken_fields"]
            self._table.setItem(row, 0, QTableWidgetItem(jid))
            self._table.setItem(row, 1, QTableWidgetItem(", ".join(broken_fields)))
            self._table.setItem(row, 2, QTableWidgetItem("✗ Missing"))
            browse_btn = QPushButton("Browse…")
            browse_btn.setProperty("_row", row)
            browse_btn.clicked.connect(self._browse_row)
            self._table.setCellWidget(row, 3, browse_btn)

        btn_box = QDialogButtonBox()
        self._apply_btn = btn_box.addButton("Apply Repairs", QDialogButtonBox.AcceptRole)
        self._skip_btn = btn_box.addButton("Skip", QDialogButtonBox.RejectRole)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _auto_search(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder to search for job output files"
        )
        if not folder:
            return

        # Build filename → path index by walking the tree
        file_index = {}
        for root, _dirs, files in os.walk(folder):
            for fname in files:
                # First occurrence wins (shallowest path)
                file_index.setdefault(fname, os.path.join(root, fname))

        for row, entry in enumerate(self._broken_jobs):
            job = entry["job"]
            jid = job.get("id", "")
            working = dict(self._repaired.get(jid, job))
            changed = False

            tif_name = f"pwtt_{jid}.tif"
            if tif_name in file_index:
                working["output_tif"] = file_index[tif_name]
                working["output_dir"] = os.path.dirname(file_index[tif_name])
                changed = True

            # Note: pwtt_footprints.gpkg is not job-ID-keyed, so when multiple
            # broken jobs share this filename the same path is assigned to all.
            # For single-job imports (the common case) this is correct.
            gpkg_name = "pwtt_footprints.gpkg"
            if gpkg_name in file_index:
                working["footprints_gpkg"] = file_index[gpkg_name]
                changed = True

            old_gpkgs = working.get("footprints_gpkgs") or {}
            new_gpkgs = {}
            for key, old_path in old_gpkgs.items():
                basename = os.path.basename((old_path or "").strip())
                resolved = file_index[basename] if (basename and basename in file_index) else old_path
                new_gpkgs[key] = resolved
                if resolved != old_path:
                    changed = True
            working["footprints_gpkgs"] = new_gpkgs

            if changed:
                self._repaired[jid] = working
            self._update_row_status(row, working)

    def _browse_row(self):
        btn = self.sender()
        if btn is None:
            return
        row = btn.property("_row")
        entry = self._broken_jobs[row]
        job = entry["job"]
        jid = job.get("id", "")
        folder = QFileDialog.getExistingDirectory(
            self, f"Select output folder for job {jid}"
        )
        if not folder:
            return
        from ..core import job_store
        working = dict(self._repaired.get(jid, job))
        job_store.repair_job_paths(working, folder)
        self._repaired[jid] = working
        self._update_row_status(row, working)

    def _update_row_status(self, row: int, job: dict):
        tif = (job.get("output_tif") or "").strip()
        if tif and os.path.isfile(tif):
            self._table.item(row, 2).setText("✓ Resolved")
        elif (job.get("output_dir") or "").strip() and os.path.isdir(job["output_dir"]):
            self._table.item(row, 2).setText("~ Partial")
        else:
            self._table.item(row, 2).setText("✗ Missing")

    def get_repaired_jobs(self):
        """Return list of repaired job dicts (only jobs that had at least one path repaired)."""
        return list(self._repaired.values())


class PWTTJobsDock(QDockWidget):
    """Dockable jobs panel: job table, action buttons, and progress bar (log: PWTTJobLogDock)."""

    # Thread-safe bridge for status messages from background tasks
    _status_signal = pyqtSignal(str, str)   # (job_id, message)
    # Signal to auto-resume a job on the main thread
    _auto_resume_signal = pyqtSignal(str)   # job_id
    # Thread-safe: append a line to a job log (e.g. CDSE poll from background thread)
    _order_poll_log = pyqtSignal(str, str)  # (job_id, message)
    # Emitted after the job table is refreshed (e.g. GRD staging dock sync)
    jobs_changed = pyqtSignal()

    def __init__(self, parent=None, plugin_dir=None, job_log_dock=None):
        super().__init__(dock_title("PWTT \u2014 Jobs", plugin_dir), parent)
        self.setObjectName("PWTTJobsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        self._active_tasks = {}    # job_id -> PWTTRunTask
        self._job_logs = {}        # job_id -> [str]
        self._job_progress = {}    # job_id -> int (0-100)
        self._poll_running = False
        self.controls_dock = None  # set after construction by plugin
        self.job_log_dock = job_log_dock
        self.log_text = job_log_dock.log_text if job_log_dock else None

        self._activity_log_dirty_jobs = set()
        self._activity_log_flush_timer = QTimer(self)
        self._activity_log_flush_timer.setSingleShot(True)
        self._activity_log_flush_timer.setInterval(1200)
        self._activity_log_flush_timer.timeout.connect(self._flush_activity_logs_to_store)

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

    @staticmethod
    def _jobs_button_group(title: str) -> Tuple[QGroupBox, QHBoxLayout]:
        box = QGroupBox(title)
        inner = QHBoxLayout(box)
        inner.setContentsMargins(8, 10, 8, 8)
        inner.setSpacing(6)
        return box, inner

    def _build_ui(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        table_lbl = QLabel("Job list")
        f = table_lbl.font()
        f.setBold(True)
        table_lbl.setFont(f)
        layout.addWidget(table_lbl)

        # Job table
        self.job_table = QTableWidget(0, 8)
        self.job_table.setHorizontalHeaderLabels(
            [
                "Status",
                "Backend",
                "Remote Job",
                "Local ID",
                "Output folder",
                "Files size",
                "Dates",
                "Created",
            ]
        )
        self.job_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.job_table.setSelectionMode(QTableWidget.SingleSelection)
        self.job_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.job_table.setAlternatingRowColors(True)
        self.job_table.setShowGrid(True)
        self.job_table.verticalHeader().hide()
        hdr = self.job_table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.job_table.setMinimumHeight(140)
        self.job_table.setMaximumHeight(260)
        self.job_table.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.job_table.itemSelectionChanged.connect(self._on_job_selected)
        layout.addWidget(self.job_table)

        # Action buttons — grouped for scanability
        self.load_btn = QPushButton("Load parameters")
        self.open_output_btn = QPushButton("Open output folder")
        self.view_logs_btn = QPushButton("View logs…")
        self.load_local_btn = QPushButton("Load Local")
        self.apply_style_btn = QPushButton("Apply styling")
        self.apply_style_active_btn = QPushButton("Apply Styling (active layer)")
        self.footprints_btn = QPushButton("Per-building (OSM)")
        self.resume_btn = QPushButton("Resume")
        self.stop_btn = QPushButton("Stop")
        self.cancel_btn = QPushButton("Cancel")
        self.rerun_btn = QPushButton("Rerun")
        self.delete_btn = QPushButton("Delete")
        self.export_jobs_btn = QPushButton("Export jobs…")
        self.import_jobs_btn = QPushButton("Import jobs…")
        self.export_job_btn = QPushButton("Export job…")

        self.load_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mActionProjectProperties.svg",
                "/mActionOptions.svg",
            )
        )
        self.open_output_btn.setIcon(
            _jobs_dock_btn_icon("/mActionFileOpen.svg", "/mIconFolder.svg")
        )
        self.view_logs_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mMessageLog.svg",
                "/mIconConsole.svg",
                "/mActionEditHelpContent.svg",
            )
        )
        self.load_local_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mActionAddRasterLayer.svg",
                "/mActionAddLayer.svg",
                resource_fallback=":/pwtt/icon_grd.svg",
            )
        )
        self.apply_style_btn.setIcon(
            _jobs_dock_btn_icon("/mActionStyleManager.svg", "/mActionEditSymbol.svg")
        )
        self.apply_style_active_btn.setIcon(
            _jobs_dock_btn_icon("/mActionStyleManager.svg", "/mActionEditSymbol.svg")
        )
        self.apply_style_active_btn.setIconSize(_JOBS_BTN_ICON_SIZE)
        self.footprints_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mIconPolygonLayer.svg",
                "/mActionAddOgrLayer.svg",
            )
        )
        self.resume_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mMediaPlay.svg",
                "/mActionStart.svg",
                resource_fallback=":/pwtt/icon_run.svg",
            )
        )
        self.stop_btn.setIcon(_jobs_dock_btn_icon("/mActionStop.svg"))
        self.cancel_btn.setIcon(
            _jobs_dock_btn_icon("/mTaskCancel.svg", "/mActionCancel.svg")
        )
        self.rerun_btn.setIcon(
            _jobs_dock_btn_icon(
                "/mActionRepeat.svg",
                "/mActionRefresh.svg",
                resource_fallback=":/pwtt/icon_run.svg",
            )
        )
        self.delete_btn.setIcon(_jobs_dock_btn_icon("/mActionDeleteSelected.svg"))
        self.export_jobs_btn.setIcon(
            _jobs_dock_btn_icon("/mActionFileSave.svg", "/mActionSaveEdits.svg")
        )
        self.import_jobs_btn.setIcon(
            _jobs_dock_btn_icon("/mActionFileOpen.svg", "/mActionAddOgrLayer.svg")
        )
        self.export_job_btn.setIcon(
            _jobs_dock_btn_icon("/mActionFileSaveAs.svg", "/mActionSaveEdits.svg")
        )

        _primary_btns = (
            self.load_btn,
            self.open_output_btn,
            self.view_logs_btn,
            self.load_local_btn,
            self.apply_style_btn,
            self.footprints_btn,
            self.resume_btn,
            self.stop_btn,
            self.cancel_btn,
            self.rerun_btn,
            self.delete_btn,
            self.export_job_btn,
        )
        for btn in _primary_btns:
            btn.setEnabled(False)
            btn.setIconSize(_JOBS_BTN_ICON_SIZE)
        for btn in (self.export_jobs_btn, self.import_jobs_btn):
            btn.setIconSize(_JOBS_BTN_ICON_SIZE)

        self.load_btn.setToolTip("Load job AOI to map and parameters to controls panel (output folder unchanged)")
        self.open_output_btn.setToolTip("Open this job\u2019s output folder in the file manager (if it exists on disk)")
        self.view_logs_btn.setToolTip(
            "Open this job\u2019s activity log in a larger window (saved with the job for later)"
        )
        self.load_local_btn.setToolTip(
            "If the result GeoTIFF (and footprints) exist on disk, add them to the map"
        )
        self.apply_style_btn.setToolTip(
            "Re-apply PWTT band-1 pseudocolor (3\u20135) and layer opacity to this job\u2019s "
            "result raster already in the project (matches layer name or GeoTIFF path)"
        )
        self.apply_style_active_btn.setToolTip(
            "Apply PWTT band-1 pseudocolor styling to the raster layer currently "
            "selected in the QGIS Layers panel (reads threshold from job_info.json "
            "if present, otherwise asks)"
        )
        self.footprints_btn.setToolTip(
            "Fetch OSM buildings and mean damage (T-stat) per polygon using the job\u2019s result GeoTIFF"
        )
        self.export_jobs_btn.setToolTip(
            "Save all jobs to a JSON file (parameters and saved activity log text, not rasters)"
        )
        self.import_jobs_btn.setToolTip(
            "Add jobs from a JSON export or another machine\u2019s jobs list (merge into this profile)"
        )
        self.export_job_btn.setToolTip(
            "Export this job's parameters and output files to a zip archive"
        )

        self.load_btn.clicked.connect(self._load_selected)
        self.open_output_btn.clicked.connect(self._open_output_folder)
        self.view_logs_btn.clicked.connect(self._view_logs_selected)
        self.load_local_btn.clicked.connect(self._load_local_selected)
        self.apply_style_btn.clicked.connect(self._apply_styling_to_result_selected)
        self.apply_style_active_btn.clicked.connect(self._apply_styling_to_active_layer)
        self.footprints_btn.clicked.connect(self._footprints_for_local_selected)
        self.resume_btn.clicked.connect(self._resume_selected)
        self.stop_btn.clicked.connect(self._stop_selected)
        self.cancel_btn.clicked.connect(self._cancel_selected)
        self.rerun_btn.clicked.connect(self._rerun_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.export_jobs_btn.clicked.connect(self._export_jobs)
        self.import_jobs_btn.clicked.connect(self._import_jobs)
        self.export_job_btn.clicked.connect(self._export_single_job)

        g_inspect, row_inspect = self._jobs_button_group("Inspect")
        for btn in (self.load_btn, self.open_output_btn, self.view_logs_btn):
            row_inspect.addWidget(btn)
        row_inspect.addStretch(1)
        layout.addWidget(g_inspect)

        g_map, row_map = self._jobs_button_group("Map && layers")
        for btn in (self.load_local_btn, self.apply_style_btn, self.apply_style_active_btn, self.footprints_btn):
            row_map.addWidget(btn)
        row_map.addStretch(1)
        layout.addWidget(g_map)

        g_run, row_run = self._jobs_button_group("Run")
        for btn in (self.resume_btn, self.stop_btn, self.cancel_btn, self.rerun_btn):
            row_run.addWidget(btn)
        row_run.addStretch(1)
        layout.addWidget(g_run)

        g_manage, row_manage = self._jobs_button_group("Manage")
        for btn in (self.export_job_btn, self.export_jobs_btn, self.import_jobs_btn, self.delete_btn):
            row_manage.addWidget(btn)
        row_manage.addStretch(1)
        layout.addWidget(g_manage)

        prog_box = QGroupBox("Progress")
        prog_layout = QVBoxLayout(prog_box)
        prog_layout.setContentsMargins(8, 10, 8, 8)
        prog_layout.setSpacing(8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(22)
        prog_layout.addWidget(self.progress_bar)

        layout.addWidget(prog_box)
        layout.addStretch(1)

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
            self.job_table.setItem(row, 1, QTableWidgetItem(job_backend_log_label(job)))

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

            # On-disk output size
            sz_b = _job_output_size_bytes(job)
            sz_item = QTableWidgetItem(_format_bytes_short(sz_b))
            sz_item.setToolTip(
                f"{sz_b:,} bytes" if sz_b > 0 else "No files found for this job path"
            )
            sz_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.job_table.setItem(row, 5, sz_item)

            # Dates
            dates = f"{job['war_start'][:7]} \u2192 {job['inference_start'][:7]}"
            self.job_table.setItem(row, 6, QTableWidgetItem(dates))

            # Created
            created = _format_local_datetime(job.get("created_at", ""))
            self.job_table.setItem(row, 7, QTableWidgetItem(created))

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

    def _ensure_job_log_loaded(self, job):
        """Hydrate in-memory log from persisted job (e.g. new session)."""
        jid = job["id"]
        if jid in self._job_logs:
            return
        self._job_logs[jid] = list(job.get("activity_log") or [])

    def _schedule_activity_log_persist(self, job_id):
        self._activity_log_dirty_jobs.add(job_id)
        if not self._activity_log_flush_timer.isActive():
            self._activity_log_flush_timer.start()

    def _flush_activity_logs_to_store(self):
        from ..core import job_store

        for jid in list(self._activity_log_dirty_jobs):
            entries = self._job_logs.get(jid)
            if entries is not None:
                job_store.update_job(jid, activity_log=list(entries))
        self._activity_log_dirty_jobs.clear()

    def _job_log_theme_colors(self):
        """Hex colors tuned for light vs dark QTextEdit backgrounds."""
        base = self.log_text.palette().color(QPalette.ColorRole.Base)
        dark = base.lightness() < 140
        if dark:
            return {
                "default": "#b0bec5",
                "hdr": "#ffe082",
                "success": "#69f0ae",
                "error": "#ff8a80",
                "err_soft": "#ffcc80",
                "warn": "#ffd54f",
                "cdse": "#90caf9",
                "asf": "#ce93d8",
                "pc": "#80deea",
                "local": "#fff59d",
                "pre": "#a5d6a7",
                "post": "#90caf9",
                "info": "#80cbc4",
                "ui": "#b39ddb",
            }
        return {
            "default": "#37474f",
            "hdr": "#e65100",
            "success": "#1b5e20",
            "error": "#b71c1c",
            "err_soft": "#e65100",
            "warn": "#f57c00",
            "cdse": "#0d47a1",
            "asf": "#4a148c",
            "pc": "#006064",
            "local": "#827717",
            "pre": "#2e7d32",
            "post": "#1565c0",
            "info": "#00695c",
            "ui": "#4527a0",
        }

    @staticmethod
    def _log_entry_is_rich_html(msg: str) -> bool:
        s = _PWTT_LOG_TS_PREFIX_RE.sub("", (msg or "").strip(), count=1)
        low = s.lower()
        if "<br" in low:
            return True
        if not s.startswith("<"):
            return False
        return any(
            x in s
            for x in ("<b>", "<pre", "<div", "<i>", "<a ", "</")
        )

    def _pick_job_log_color(self, msg: str, c: dict) -> str:
        body = _PWTT_LOG_TS_PREFIX_RE.sub("", (msg or "").strip(), count=1)
        low = body.strip().lower()
        sl = low

        if "<pre>" in low:
            return c["error"]
        if "task failed" in low:
            return c["error"]
        if "task completed successfully" in low:
            return c["success"]
        if "products offline" in low or "staging from cold" in low:
            return c["warn"]
        if "cancelled" in low or "canceled" in low:
            return c["warn"]
        if "terminated unexpectedly" in low:
            return c["error"]
        if "download failed" in low or ("skip" in sl and "failed" in sl):
            return c["err_soft"]
        if "failed to" in low or "footprints error" in low:
            return c["error"]
        if sl.startswith("pre:"):
            return c["pre"]
        if sl.startswith("post:"):
            return c["post"]
        if sl.startswith("local:") or low.startswith("local processing"):
            return c["local"]
        if sl.startswith("cdse") or "cdse api" in low or sl.startswith("cdse:"):
            return c["cdse"]
        if sl.startswith("asf") or "asf api" in low or sl.startswith("asf:"):
            return c["asf"]
        if sl.startswith("pc ") or "pc api" in low or "pc get" in low[:24]:
            return c["pc"]
        if "task started" in low:
            return c["hdr"]
        if "remote job id saved" in low:
            return c["info"]
        if "openeo" in low:
            return c["cdse"]
        if "earth engine" in low:
            return c["asf"]
        if (
            "load local" in low
            or "apply styling" in low
            or "layer added" in low
            or "footprints" in low
        ):
            return c["ui"]
        return c["default"]

    def _format_job_log_entry_html(self, msg: str) -> str:
        c = self._job_log_theme_colors()
        color = self._pick_job_log_color(msg, c)
        margin = "margin:3px 0;line-height:1.4;"
        if self._log_entry_is_rich_html(msg):
            inner = msg
            if "<pre>" in msg:
                inner = msg.replace(
                    "<pre>",
                    "<pre style='opacity:0.95;white-space:pre-wrap;word-break:break-word;'>",
                    1,
                )
            return f'<div style="{margin}color:{color};">{inner}</div>'
        safe = html.escape(msg, quote=False).replace("\n", "<br/>")
        return f'<p style="{margin}color:{color};margin-block:3px;">{safe}</p>'

    def _job_log_document_html(self, entries):
        parts = [self._format_job_log_entry_html(m) for m in entries]
        bg = self.log_text.palette().color(QPalette.ColorRole.Base).name()
        _mono = (
            "ui-monospace,'Cascadia Mono','Source Code Pro',Menlo,Consolas,monospace"
        )
        return (
            "<html><head><meta charset=\"utf-8\"/></head>"
            f"<body style=\"background-color:{bg};font-family:{_mono};font-size:12px;\">"
            + "".join(parts)
            + "</body></html>"
        )

    def _append_job_log(self, msg: str):
        """Append one log line/block with PWTT color coding (raw *msg* stays in _job_logs)."""
        self.log_text.append(self._format_job_log_entry_html(msg))

    def _stamp_activity(self, message: str) -> str:
        """Prefix *message* with a timestamp (avoid double-stamping)."""
        s = (message or "").strip()
        if _PWTT_LOG_TS_PREFIX_RE.match(s):
            return message
        return pwtt_activity_ts_prefix() + message

    def _refresh_job_log_panel_if_selected(self, job_id: str):
        """Rebuild the log QTextEdit when the same row stays selected (no selectionChanged)."""
        if self._get_selected_job_id() != job_id:
            return
        entries = self._job_logs.get(job_id, [])
        self.log_text.clear()
        if entries:
            self.log_text.setHtml(self._job_log_document_html(entries))

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
            for btn in (self.load_btn, self.open_output_btn, self.view_logs_btn, self.load_local_btn, self.apply_style_btn, self.footprints_btn, self.resume_btn, self.stop_btn,
                         self.cancel_btn, self.rerun_btn, self.delete_btn, self.export_job_btn):
                btn.setEnabled(False)
            self.log_text.clear()
            self.progress_bar.setValue(0)
            return

        self._ensure_job_log_loaded(job)
        st = job["status"]
        self.load_btn.setEnabled(True)
        out_dir = (job.get("output_dir") or "").strip()
        self.open_output_btn.setEnabled(bool(out_dir and os.path.isdir(out_dir)))
        self.load_local_btn.setEnabled(True)
        self.apply_style_btn.setEnabled(True)
        self.export_job_btn.setEnabled(True)
        self.footprints_btn.setEnabled(self._local_result_tif_path(job) is not None)
        self.resume_btn.setEnabled(st in job_store.RESUMABLE_STATUSES)
        self.stop_btn.setEnabled(st == job_store.STATUS_RUNNING)
        self.cancel_btn.setEnabled(
            st in (job_store.STATUS_RUNNING, job_store.STATUS_WAITING_ORDERS,
                   job_store.STATUS_STOPPED)
        )
        self.rerun_btn.setEnabled(True)
        self.delete_btn.setEnabled(st != job_store.STATUS_RUNNING)
        self.view_logs_btn.setEnabled(bool(self._job_logs.get(job["id"])))

        self.resume_btn.setText(
            "Check && Resume" if st == job_store.STATUS_WAITING_ORDERS else "Resume"
        )

        # Log (colorized HTML; raw lines remain in _job_logs for JSON persist)
        self.log_text.clear()
        entries = self._job_logs.get(job["id"], [])
        if entries:
            self.log_text.setHtml(self._job_log_document_html(entries))

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
        job_id = job["id"]
        if job_id in self._active_tasks:
            note = self._stamp_activity(
                "Ignored: this job is already running (duplicate start/resume)."
            )
            self._job_logs.setdefault(job_id, []).append(note)
            self._schedule_activity_log_persist(job_id)
            self._refresh_job_log_panel_if_selected(job_id)
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
            data_source=job.get("data_source")
            if job.get("backend_id") == "local"
            else None,
        )

        self._ensure_job_log_loaded(job)
        self._active_tasks[job_id] = task
        bname = job_backend_log_label(job)
        remote_id = job.get("remote_job_id")
        log_parts = [f"Task started — backend: {bname}"]
        if remote_id:
            log_parts.append(f"remote job: {remote_id}")
        log_parts.append(f"dates: {job['war_start']} → {job['inference_start']}")
        log_parts.append(f"pre: {job['pre_interval']}mo, post: {job['post_interval']}mo")
        log_parts.append(f"output: {job['output_dir']}")
        self._job_logs.setdefault(job_id, []).append(
            self._stamp_activity("<br>".join(log_parts))
        )
        self._job_progress[job_id] = 0
        self._schedule_activity_log_persist(job_id)

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
        # Same-row reselection does not fire itemSelectionChanged — refresh log + buttons.
        self._on_job_selected()
        self.show()
        self.raise_()

    # ── Task callbacks (main thread) ──────────────────────────────────────────

    def _append_order_poll_log(self, job_id, msg):
        line = self._stamp_activity(msg)
        self._job_logs.setdefault(job_id, []).append(line)
        self._schedule_activity_log_persist(job_id)
        if self._get_selected_job_id() == job_id:
            self._append_job_log(line)

    def _handle_status_message(self, job_id, msg):
        line = self._stamp_activity(msg)
        self._job_logs.setdefault(job_id, []).append(line)
        self._schedule_activity_log_persist(job_id)
        if self._get_selected_job_id() == job_id:
            self._append_job_log(line)

        # Persist remote job id as soon as the backend sets it
        task = self._active_tasks.get(job_id)
        if task:
            remote_id = getattr(task, "remote_job_id", None)
            if remote_id:
                from ..core import job_store
                existing = job_store.get_job(job_id)
                if existing and existing.get("remote_job_id") != remote_id:
                    job_store.update_job(job_id, remote_job_id=remote_id)
                    note = self._stamp_activity(f"Remote job ID saved: {remote_id}")
                    self._job_logs.setdefault(job_id, []).append(note)
                    self._schedule_activity_log_persist(job_id)
                    if self._get_selected_job_id() == job_id:
                        self._append_job_log(note)
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
            ids_str = ", ".join(ids[:5])
            if len(ids) > 5:
                ids_str += f" (+{len(ids) - 5} more)"
            self._job_logs.setdefault(job_id, []).append(
                self._stamp_activity(
                    f"<b>Products offline</b> — staging from cold storage.<br>"
                    f"Product IDs: {ids_str}<br>"
                    f"Will auto-check every 2 min and resume when available."
                )
            )
            ow_fields = dict(
                status=job_store.STATUS_WAITING_ORDERS,
                offline_product_ids=ids,
                offline_products=offline_products,
                activity_log=list(self._job_logs[job_id]),
            )
            if remote_id:
                ow_fields["remote_job_id"] = remote_id
            job_store.update_job(job_id, **ow_fields)
            self._activity_log_dirty_jobs.discard(job_id)
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
        done_line = self._stamp_activity("<br>".join(done_parts))
        self._job_logs.setdefault(job_id, []).append(done_line)
        update_fields["activity_log"] = list(self._job_logs[job_id])
        job_store.update_job(job_id, **update_fields)
        self._job_progress[job_id] = 100
        self._activity_log_dirty_jobs.discard(job_id)
        self._refresh_job_list()
        if self._get_selected_job_id() == job_id:
            self.progress_bar.setValue(100)
            self._append_job_log(done_line)

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
            msg = "Task was cancelled."
            if remote_id:
                msg += f" Remote job: {remote_id}"
            self._job_logs.setdefault(job_id, []).append(self._stamp_activity(msg))
            log_snapshot = list(self._job_logs[job_id])
            if current and current["status"] not in (
                job_store.STATUS_STOPPED, job_store.STATUS_CANCELLED
            ):
                job_store.update_job(
                    job_id,
                    status=job_store.STATUS_CANCELLED,
                    activity_log=log_snapshot,
                )
            else:
                job_store.update_job(job_id, activity_log=log_snapshot)
        elif task.exception:
            err_parts = [
                f"<b>Task failed:</b> {html.escape(str(task.exception), quote=False)}"
            ]
            if remote_id:
                err_parts.append(html.escape(f"Remote job: {remote_id}", quote=False))
            self._job_logs.setdefault(job_id, []).append(
                self._stamp_activity("<br>".join(err_parts))
            )
            if task.error_detail:
                self._job_logs[job_id].append(
                    self._stamp_activity(
                        f"<pre>{html.escape(task.error_detail, quote=False)}</pre>"
                    )
                )
            job_store.update_job(
                job_id,
                status=job_store.STATUS_FAILED,
                error=str(task.exception),
                activity_log=list(self._job_logs[job_id]),
            )
        else:
            self._job_logs.setdefault(job_id, []).append(
                self._stamp_activity("Task terminated unexpectedly.")
            )
            job_store.update_job(
                job_id,
                status=job_store.STATUS_FAILED,
                error="Unknown error",
                activity_log=list(self._job_logs[job_id]),
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

    def _open_output_folder(self):
        job = self._get_selected_job()
        if not job:
            return
        out_dir = (job.get("output_dir") or "").strip()
        if not out_dir or not os.path.isdir(out_dir):
            QMessageBox.information(
                self,
                "PWTT",
                "Output folder does not exist or is not set for this job.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))

    def _view_logs_selected(self):
        job = self._get_selected_job()
        if not job:
            return
        jid = job["id"]
        self._ensure_job_log_loaded(job)
        entries = self._job_logs.get(jid) or []
        if not entries:
            QMessageBox.information(self, "PWTT", "No log entries for this job.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"PWTT job log — {jid}")
        v = QVBoxLayout(dlg)
        te = QTextEdit(dlg)
        te.setReadOnly(True)
        te.setHtml(self._job_log_document_html(entries))
        v.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(780, 520)
        dlg.exec_()

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
        grd_ds = job.get("data_source") if backend_id == "local" else None
        project = QgsProject.instance()
        thr = damage_threshold_from_job_meta(
            tif_path, default=float(job.get("damage_threshold", 3.3))
        )

        label = pwtt_damage_layer_name(jid, backend_id, data_source=grd_ds)
        rlayer = QgsRasterLayer(tif_path, label, "gdal")
        if not rlayer.isValid():
            QMessageBox.warning(
                self, "PWTT", f"Could not open raster as a QGIS layer:\n{tif_path}"
            )
            return
        style_pwtt_raster_layer(rlayer, damage_threshold=thr)
        add_map_layer_to_pwtt_job_group(
            project, rlayer, jid, backend_id, data_source=grd_ds
        )
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
                data_source=grd_ds,
                war_start=job.get("war_start"),
                inference_start=job.get("inference_start"),
            )
            vl = QgsVectorLayer(pth, fp_label, "ogr")
            if vl.isValid():
                style_pwtt_footprints_layer(vl)
                add_map_layer_to_pwtt_job_group(
                    project, vl, jid, backend_id, data_source=grd_ds
                )
                log_parts.append(f"Load Local: added {fp_label}")
            else:
                log_parts.append(f"Load Local: skipped invalid footprints \u2014 {pth}")

        msg = self._stamp_activity("<br>".join(log_parts))
        self._job_logs.setdefault(jid, []).append(msg)
        self._schedule_activity_log_persist(jid)
        if self._get_selected_job_id() == jid:
            self._append_job_log(msg)

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
        grd_ds = job.get("data_source") if backend_id == "local" else None
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

        expected_name = pwtt_damage_layer_name(jid, backend_id, data_source=grd_ds)
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

        msg = self._stamp_activity("Apply styling: updated " + ", ".join(matched))
        self._job_logs.setdefault(jid, []).append(msg)
        self._schedule_activity_log_persist(jid)
        if self._get_selected_job_id() == jid:
            self._append_job_log(msg)
        try:
            from qgis.utils import iface as qgis_iface

            if qgis_iface is not None:
                qgis_iface.mapCanvas().refresh()
        except ImportError:
            pass

    def _apply_styling_to_active_layer(self):
        """Apply PWTT pseudocolor styling to whichever raster is active in the Layers panel."""
        try:
            from qgis.utils import iface as qgis_iface
        except ImportError:
            qgis_iface = None

        if qgis_iface is None:
            return

        from qgis.core import QgsRasterLayer
        from ..core.qgis_output_style import damage_threshold_from_job_meta, style_pwtt_raster_layer

        layer = qgis_iface.activeLayer()
        if layer is None or not isinstance(layer, QgsRasterLayer) or not layer.isValid():
            qgis_iface.messageBar().pushWarning(
                "PWTT", "Please select a valid raster layer in the Layers panel first."
            )
            return

        src = layer.source() or ""
        # Strip GDAL URI parameters (e.g. "path.tif|layerid=0") before path operations.
        src_file = src.split("|")[0].strip() if "|" in src else src
        src_dir = os.path.dirname(src_file) if src_file else ""
        meta_path = os.path.join(src_dir, "job_info.json") if src_dir else ""
        found_meta = bool(meta_path and os.path.isfile(meta_path))

        if found_meta:
            thr = damage_threshold_from_job_meta(src_file, default=3.3)
        else:
            val, ok = QInputDialog.getDouble(
                self,
                "Damage threshold",
                "Enter damage threshold (T-statistic):",
                3.3,
                0.0,
                20.0,
                1,
            )
            if not ok:
                return
            thr = val

        style_pwtt_raster_layer(layer, damage_threshold=thr)
        qgis_iface.mapCanvas().refresh()
        qgis_iface.messageBar().pushSuccess(
            "PWTT", f"Styling applied to '{layer.name()}'"
        )

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
                note = self._stamp_activity(f"Footprints saved: {gpkg_path}")
                self._job_logs.setdefault(jid, []).append(note)
                self._schedule_activity_log_persist(jid)
                if self._get_selected_job_id() == jid:
                    self._append_job_log(note)
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
        grd_ds = (job or {}).get("data_source") if backend_id == "local" else None
        label = pwtt_footprints_layer_name(
            job_id,
            backend_id,
            "current_osm",
            data_source=grd_ds,
            war_start=(job or {}).get("war_start"),
            inference_start=(job or {}).get("inference_start"),
        )
        layer = QgsVectorLayer(path, label, "ogr")
        if layer.isValid():
            style_pwtt_footprints_layer(layer)
            add_map_layer_to_pwtt_job_group(
                QgsProject.instance(), layer, job_id, backend_id, data_source=grd_ds
            )
            msg = self._stamp_activity(f"Layer added: {label}")
            self._job_logs.setdefault(job_id, []).append(msg)
            self._schedule_activity_log_persist(job_id)
            if self._get_selected_job_id() == job_id:
                self._append_job_log(msg)
        else:
            msg = self._stamp_activity("Failed to load footprints GeoPackage.")
            self._job_logs.setdefault(job_id, []).append(msg)
            self._schedule_activity_log_persist(job_id)
            if self._get_selected_job_id() == job_id:
                self._append_job_log(msg)

    def _resume_selected(self):
        job = self._get_selected_job()
        if not job:
            return
        if job["backend_id"] == "local" and not confirm_local_processing_storage(self):
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
        jid = job["id"]
        if remote_id:
            self._job_logs.setdefault(jid, []).append(
                self._stamp_activity(
                    f"Resuming (was {prev_status}) — remote job: {remote_id}"
                )
            )
        else:
            self._job_logs.setdefault(jid, []).append(
                self._stamp_activity(
                    f"Resuming (was {prev_status}) — no remote job id (full re-run)"
                )
            )
        self._schedule_activity_log_persist(jid)
        self._refresh_job_log_panel_if_selected(jid)
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
            self._job_logs.setdefault(job["id"], []).append(self._stamp_activity(msg))
            self._schedule_activity_log_persist(job["id"])
            self._refresh_job_log_panel_if_selected(job["id"])
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
        self._job_logs.setdefault(job["id"], []).append(self._stamp_activity(msg))
        self._schedule_activity_log_persist(job["id"])
        self._refresh_job_list()
        if self._get_selected_job_id() == job["id"]:
            self._on_job_selected()

    def _rerun_selected(self):
        """Create a new job with the same parameters and launch it."""
        old = self._get_selected_job()
        if not old:
            return
        if old["backend_id"] == "local" and not confirm_local_processing_storage(self):
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

    def _export_jobs(self):
        from ..core import job_store

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export PWTT jobs",
            "",
            "JSON (*.json);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            n = job_store.export_jobs_to_file(path)
        except OSError as e:
            QMessageBox.critical(self, "Export jobs", str(e))
            return
        QMessageBox.information(
            self,
            "Jobs exported",
            f"Exported {n} job(s) to:\n{path}\n\n{_JOBS_IO_FILES_HINT}",
        )

    def _export_single_job(self):
        from ..core import job_store

        job = self._get_selected_job()
        if not job:
            return
        jid = job["id"]
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export PWTT job",
            f"pwtt_job_{jid}.zip",
            "ZIP archive (*.zip);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            result = job_store.export_single_job_zip(job, path)
        except OSError as e:
            QMessageBox.critical(self, "Export job", str(e))
            return
        n = result["files_included"]
        m = result["files_missing"]
        msg = f"Job {jid} exported to:\n{path}\n\nOutput files included: {n}"
        if m:
            msg += f"\nOutput files not found on disk (skipped): {m}"
        QMessageBox.information(self, "Job exported", msg)

    def _import_jobs(self):
        from ..core import job_store

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import PWTT jobs",
            "",
            "JSON (*.json);;All files (*)",
        )
        if not path:
            return

        # Snapshot existing IDs so we can identify newly imported jobs afterwards
        pre_import_ids = {j["id"] for j in job_store.load_jobs()}

        try:
            stats = job_store.merge_jobs_from_file(path)
        except ValueError as e:
            QMessageBox.critical(self, "Import jobs", str(e))
            return
        except json.JSONDecodeError as e:
            QMessageBox.critical(self, "Import jobs", f"Invalid JSON:\n{e}")
            return
        except OSError as e:
            QMessageBox.critical(self, "Import jobs", str(e))
            return

        added = stats["added"]
        if added == 0:
            msg = (
                "No jobs were added (file empty, invalid entries, or nothing to merge).\n\n"
                f"{_JOBS_IO_FILES_HINT}"
            )
            if stats["skipped_invalid"]:
                msg = (
                    f"No valid jobs found ({stats['skipped_invalid']} skipped).\n\n"
                    f"{_JOBS_IO_FILES_HINT}"
                )
            QMessageBox.warning(self, "Import jobs", msg)
            return

        self._refresh_job_list()
        lines = [f"Added {added} job(s) from:\n{path}"]
        if stats["skipped_invalid"]:
            lines.append(f"Skipped {stats['skipped_invalid']} invalid entr(y/ies).")
        if stats["ids_rewritten"]:
            lines.append(
                f"Local job id(s) were changed for {stats['ids_rewritten']} job(s) "
                "because those id(s) were already in use."
            )
        lines.append("")
        lines.append(_JOBS_IO_FILES_HINT)
        QMessageBox.information(self, "Jobs imported", "\n".join(lines))

        # Path repair: check only the newly added jobs
        all_jobs = job_store.load_jobs()
        new_jobs = [j for j in all_jobs if j["id"] not in pre_import_ids]
        broken = job_store.find_broken_path_jobs(new_jobs)
        if broken:
            dlg = PathRepairDialog(broken, parent=self)
            if dlg.exec_() == QDialog.Accepted:
                for repaired_job in dlg.get_repaired_jobs():
                    job_store.save_job(repaired_job)
                self._refresh_job_list()

    def _delete_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job or job["status"] == job_store.STATUS_RUNNING:
            return
        jid = job["id"]
        label = f"{jid[:8]}\u2026" if len(jid) > 10 else jid
        reply = QMessageBox.question(
            self,
            "PWTT",
            f"Remove job {label} from the jobs list?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        delete_files = False
        if _job_has_disk_output(job):
            sz_b = _job_output_size_bytes(job)
            sz_human = _format_bytes_short(sz_b) if sz_b > 0 else "folder exists"
            out_dir = (job.get("output_dir") or "").strip()
            extra = ""
            if out_dir:
                extra = f"\n\nOutput path:\n{out_dir}"
            r2 = QMessageBox.question(
                self,
                "PWTT",
                f"This job has files on disk (about {sz_human}).\n\n"
                f"Delete those files as well? This cannot be undone.{extra}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            delete_files = r2 == QMessageBox.Yes

        if delete_files:
            try:
                _remove_job_output_from_disk(job)
            except OSError as e:
                QMessageBox.warning(
                    self,
                    "PWTT",
                    f"Could not delete all job files:\n{e}\n\n"
                    "The job will stay in the list; fix permissions or remove files manually.",
                )
                return

        job_store.delete_job(jid)
        self._job_logs.pop(jid, None)
        self._job_progress.pop(jid, None)
        self._active_tasks.pop(jid, None)
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
        try:
            from ..core.downloader import get_token, _is_product_online

            token = get_token(username, password)
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
                        f"Auto-check CDSE API: all {n} product(s) online — resuming.",
                    )
                    self._auto_resume_signal.emit(jid)
                else:
                    self._order_poll_log.emit(
                        jid,
                        f"Auto-check CDSE API: {online_n}/{n} product(s) online; still waiting.",
                    )
        except Exception as e:
            for job in waiting_jobs:
                self._order_poll_log.emit(
                    job["id"],
                    f"Auto-check CDSE API failed (will retry on next timer): {e}",
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
            self._stamp_activity("Products now available — auto-resuming…")
        )
        self._schedule_activity_log_persist(job_id)
        self._refresh_job_log_panel_if_selected(job_id)
        self.launch_job(job, backend)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self):
        self._order_timer.stop()

