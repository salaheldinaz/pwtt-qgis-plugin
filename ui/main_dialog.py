# -*- coding: utf-8 -*-
"""PWTT dock panels: controls and jobs."""

import os
import threading
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QStackedWidget,
    QWidget,
    QLineEdit,
    QPushButton,
    QDateEdit,
    QSpinBox,
    QCheckBox,
    QProgressBar,
    QTextEdit,
    QGroupBox,
    QFormLayout,
    QMessageBox,
    QScrollArea,
    QFrame,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsSettings,
    QgsWkbTypes,
)
from qgis.gui import QgsFileWidget, QgsRubberBand


BACKENDS = [
    ("openeo", "openEO (recommended)"),
    ("gee", "Google Earth Engine"),
    ("local", "Local Processing"),
]

_STATUS_LABELS = {
    "pending": "Pending",
    "running": "Running\u2026",
    "waiting_orders": "Waiting",
    "stopped": "Stopped",
    "completed": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
_STATUS_COLORS = {
    "pending": "#888",
    "running": "#2196F3",
    "waiting_orders": "#FF9800",
    "stopped": "#888",
    "completed": "#4CAF50",
    "failed": "#F44336",
    "cancelled": "#888",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_plugin_version(plugin_dir):
    """Single source of truth: pwtt_qgis/metadata.txt version= line."""
    if not plugin_dir:
        return None
    meta = os.path.join(plugin_dir, "metadata.txt")
    try:
        with open(meta, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _dock_title(base, plugin_dir):
    v = _read_plugin_version(plugin_dir)
    return f"{base} ({v})" if v else base


def _get_backend_class(backend_id):
    try:
        if backend_id == "openeo":
            from ..core.openeo_backend import OpenEOBackend
            return OpenEOBackend
        if backend_id == "gee":
            from ..core.gee_backend import GEEBackend
            return GEEBackend
        if backend_id == "local":
            from ..core.local_backend import LocalBackend
            return LocalBackend
    except Exception:
        return None
    return None


def _auth_with_progress(backend, credentials, backend_id, parent=None):
    """Run backend.authenticate() in a background QThread with a progress dialog.

    Returns True on success, False on failure or cancellation.
    """
    from qgis.PyQt.QtCore import QThread
    from qgis.PyQt.QtWidgets import QProgressDialog

    class _Worker(QThread):
        def __init__(self, b, c):
            super().__init__()
            self.b, self.c, self.ok = b, c, False

        def run(self):
            self.ok = self.b.authenticate(self.c)

    hint = ""
    if backend_id == "openeo" and not (credentials or {}).get("client_id"):
        hint = "\nFor OIDC: open the Python console to see the login URL."

    dlg = QProgressDialog(f"Authenticating\u2026{hint}", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("PWTT")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)

    worker = _Worker(backend, credentials)
    worker.finished.connect(dlg.close)
    worker.start()
    dlg.exec_()

    if dlg.wasCanceled():
        worker.wait(2000)
        return False

    worker.wait()
    return worker.ok


def _create_and_auth_backend(backend_id, parent=None):
    """Create a backend instance and authenticate using stored credentials.

    If *parent* is given, authentication runs in a background thread with a
    progress dialog so the UI stays responsive (important for OIDC flows).
    """
    BackendClass = _get_backend_class(backend_id)
    if not BackendClass:
        raise RuntimeError(f"Backend '{backend_id}' is not available.")
    backend = BackendClass()
    ok, msg = backend.check_dependencies()
    if not ok:
        raise RuntimeError(msg)
    s = QgsSettings()
    s.beginGroup("PWTT")
    if backend_id == "openeo":
        creds = {
            "client_id": s.value("openeo_client_id", "") or None,
            "client_secret": s.value("openeo_client_secret", "") or None,
        }
    elif backend_id == "gee":
        creds = {"project": s.value("gee_project", "")}
    elif backend_id == "local":
        creds = {
            "username": s.value("cdse_username", ""),
            "password": s.value("cdse_password", ""),
        }
    else:
        creds = {}
    s.endGroup()
    if parent:
        auth_ok = _auth_with_progress(backend, creds, backend_id, parent)
    else:
        auth_ok = backend.authenticate(creds)
    if not auth_ok:
        raise RuntimeError("Authentication failed. Check credentials in the Controls panel.")
    return backend


# ═══════════════════════════════════════════════════════════════════════════════
#  PWTTJobsDock  — job list, actions, progress, log, order polling
# ═══════════════════════════════════════════════════════════════════════════════

class PWTTJobsDock(QDockWidget):
    """Dockable jobs panel: job table, action buttons, progress bar, and log."""

    # Thread-safe bridge for status messages from background tasks
    _status_signal = pyqtSignal(str, str)   # (job_id, message)
    # Signal to auto-resume a job on the main thread
    _auto_resume_signal = pyqtSignal(str)   # job_id
    # Tells the controls dock whether the Run button should be enabled
    run_button_enabled = pyqtSignal(bool)

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(_dock_title("PWTT \u2014 Jobs", plugin_dir), parent)
        self.setObjectName("PWTTJobsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        self._active_tasks = {}    # job_id -> PWTTRunTask
        self._job_logs = {}        # job_id -> [str]
        self._job_progress = {}    # job_id -> int (0-100)
        self._poll_running = False

        self._build_ui()

        self._status_signal.connect(self._handle_status_message)
        self._auto_resume_signal.connect(self._auto_resume_job)

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
        self.job_table = QTableWidget(0, 4)
        self.job_table.setHorizontalHeaderLabels(["Status", "Backend", "Dates", "Created"])
        self.job_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.job_table.setSelectionMode(QTableWidget.SingleSelection)
        self.job_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.job_table.verticalHeader().hide()
        hdr = self.job_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.job_table.setMaximumHeight(180)
        self.job_table.itemSelectionChanged.connect(self._on_job_selected)
        layout.addWidget(self.job_table)

        # Action buttons
        btn_row = QHBoxLayout()
        self.resume_btn = QPushButton("Resume")
        self.stop_btn = QPushButton("Stop")
        self.cancel_btn = QPushButton("Cancel")
        self.rerun_btn = QPushButton("Rerun")
        self.delete_btn = QPushButton("Delete")
        for btn in (self.resume_btn, self.stop_btn, self.cancel_btn,
                     self.rerun_btn, self.delete_btn):
            btn.setEnabled(False)
            btn_row.addWidget(btn)
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
            item = QTableWidgetItem(_STATUS_LABELS.get(st, st))
            item.setForeground(QColor(_STATUS_COLORS.get(st, "#000")))
            item.setData(Qt.UserRole, job["id"])
            self.job_table.setItem(row, 0, item)

            # Backend
            bname = {"openeo": "openEO", "gee": "GEE", "local": "Local"}.get(
                job["backend_id"], job["backend_id"]
            )
            self.job_table.setItem(row, 1, QTableWidgetItem(bname))

            # Dates
            dates = f"{job['war_start'][:7]} \u2192 {job['inference_start'][:7]}"
            self.job_table.setItem(row, 2, QTableWidgetItem(dates))

            # Created
            created = job.get("created_at", "")[:16].replace("T", " ")
            self.job_table.setItem(row, 3, QTableWidgetItem(created))

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

    def _select_job(self, job_id):
        for row in range(self.job_table.rowCount()):
            item = self.job_table.item(row, 0)
            if item and item.data(Qt.UserRole) == job_id:
                self.job_table.setCurrentCell(row, 0)
                return

    def _on_job_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            for btn in (self.resume_btn, self.stop_btn, self.cancel_btn,
                         self.rerun_btn, self.delete_btn):
                btn.setEnabled(False)
            self.log_text.clear()
            self.progress_bar.setValue(0)
            return

        st = job["status"]
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

        task = PWTTRunTask(
            backend=backend,
            aoi_wkt=job["aoi_wkt"],
            war_start=job["war_start"],
            inference_start=job["inference_start"],
            pre_interval=job["pre_interval"],
            post_interval=job["post_interval"],
            output_dir=job["output_dir"],
            include_footprints=job["include_footprints"],
            job_id=job["id"],
        )

        job_id = job["id"]
        self._active_tasks[job_id] = task
        self._job_logs.setdefault(job_id, []).append("Task started\u2026")
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

        self.run_button_enabled.emit(False)
        self._refresh_job_list()
        self._select_job(job_id)
        self.show()
        self.raise_()

    # ── Task callbacks (main thread) ──────────────────────────────────────────

    def _handle_status_message(self, job_id, msg):
        self._job_logs.setdefault(job_id, []).append(msg)
        if self._get_selected_job_id() == job_id:
            self.log_text.append(msg)

    def _on_task_progress(self, job_id, value):
        self._job_progress[job_id] = int(value)
        if self._get_selected_job_id() == job_id:
            self.progress_bar.setValue(int(value))

    def _on_task_completed(self, job_id):
        from ..core import job_store
        task = self._active_tasks.pop(job_id, None)
        job_store.update_job(
            job_id,
            status=job_store.STATUS_COMPLETED,
            output_tif=getattr(task, "output_tif", None),
            footprints_gpkg=getattr(task, "footprints_gpkg", None),
        )
        self._job_progress[job_id] = 100
        self._job_logs.setdefault(job_id, []).append("Done.")
        self._refresh_job_list()
        if self._get_selected_job_id() == job_id:
            self.progress_bar.setValue(100)
            self.log_text.append("Done.")
        self._emit_run_enabled()

    def _on_task_terminated(self, job_id):
        from ..core import job_store
        task = self._active_tasks.pop(job_id, None)
        if not task:
            self._emit_run_enabled()
            return

        if task.products_offline:
            job_store.update_job(
                job_id,
                status=job_store.STATUS_WAITING_ORDERS,
                offline_product_ids=task.offline_product_ids,
            )
            self._job_logs.setdefault(job_id, []).append(
                "Products are being staged from cold storage. "
                "Will auto-check every 2 min and resume when available."
            )
        elif task.isCanceled():
            current = job_store.get_job(job_id)
            if current and current["status"] not in (
                job_store.STATUS_STOPPED, job_store.STATUS_CANCELLED
            ):
                job_store.update_job(job_id, status=job_store.STATUS_CANCELLED)
            self._job_logs.setdefault(job_id, []).append("Task was cancelled.")
        elif task.exception:
            job_store.update_job(
                job_id, status=job_store.STATUS_FAILED, error=str(task.exception)
            )
            self._job_logs.setdefault(job_id, []).append(
                f"<b>Task failed:</b> {task.exception}"
            )
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
        self._emit_run_enabled()

    def _emit_run_enabled(self):
        self.run_button_enabled.emit(not bool(self._active_tasks))

    # ── Action handlers ───────────────────────────────────────────────────────

    def _resume_selected(self):
        job = self._get_selected_job()
        if not job:
            return
        try:
            backend = _create_and_auth_backend(job["backend_id"], parent=self)
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        self._job_logs.setdefault(job["id"], []).append("Resuming\u2026")
        self.launch_job(job, backend)

    def _stop_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            return
        task = self._active_tasks.get(job["id"])
        if task:
            job_store.update_job(job["id"], status=job_store.STATUS_STOPPED)
            task.cancel()
            self._job_logs.setdefault(job["id"], []).append("Stopping\u2026")
        self._refresh_job_list()

    def _cancel_selected(self):
        from ..core import job_store
        job = self._get_selected_job()
        if not job:
            return
        job_store.update_job(job["id"], status=job_store.STATUS_CANCELLED)
        task = self._active_tasks.pop(job["id"], None)
        if task:
            task.cancel()
        self._job_logs.setdefault(job["id"], []).append("Cancelled.")
        self._refresh_job_list()
        if self._get_selected_job_id() == job["id"]:
            self._on_job_selected()
        self._emit_run_enabled()

    def _rerun_selected(self):
        """Create a new job with the same parameters and launch it."""
        old = self._get_selected_job()
        if not old:
            return
        try:
            backend = _create_and_auth_backend(old["backend_id"], parent=self)
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        from ..core import job_store
        new_job = job_store.create_job(
            backend_id=old["backend_id"],
            aoi_wkt=old["aoi_wkt"],
            war_start=old["war_start"],
            inference_start=old["inference_start"],
            pre_interval=old["pre_interval"],
            post_interval=old["post_interval"],
            output_dir=old["output_dir"],
            include_footprints=old["include_footprints"],
        )
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
            j for j in job_store.load_jobs()
            if j["status"] == job_store.STATUS_WAITING_ORDERS
            and j["backend_id"] == "local"
            and j.get("offline_product_ids")
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
                all_online = all(
                    _is_product_online(token, pid)
                    for pid in job["offline_product_ids"]
                )
                if all_online:
                    self._auto_resume_signal.emit(job["id"])
        except Exception:
            pass
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
            backend = _create_and_auth_backend(job["backend_id"])
        except Exception:
            return
        self._job_logs.setdefault(job_id, []).append(
            "Products now available \u2014 auto-resuming\u2026"
        )
        self.launch_job(job, backend)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self):
        self._order_timer.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PWTTControlsDock  — backend, credentials, AOI, parameters, output, run
# ═══════════════════════════════════════════════════════════════════════════════

class PWTTControlsDock(QDockWidget):
    """Dockable controls panel: backend, credentials, AOI, parameters, output, run."""

    def __init__(self, iface, plugin_dir, jobs_dock, parent=None):
        super().__init__(_dock_title("PWTT \u2014 Damage Detection", plugin_dir), parent)
        self.setObjectName("PWTTControlsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.jobs_dock = jobs_dock
        self.aoi_wkt = None
        self.aoi_rect = None
        self.map_tool = None
        self._previous_map_tool = None

        self._rubber_band = None

        # Re-enable run button when jobs dock says so
        self.jobs_dock.run_button_enabled.connect(self._set_run_enabled)

        self._build_ui()
        self._load_settings()
        self._on_backend_changed(self.backend_combo.currentIndex())

    def _set_run_enabled(self, enabled):
        self.run_btn.setEnabled(enabled)

    @staticmethod
    def _hint(text: str) -> QLabel:
        """Small grey italic one-liner placed at the top of a group box."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray; font-style: italic; font-size: 0.85em;")
        return lbl

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)

        # Backend selection
        backend_group = QGroupBox("Processing backend")
        bl = QVBoxLayout(backend_group)
        bl.addWidget(self._hint("Choose which service runs the SAR analysis."))
        self.backend_combo = QComboBox()
        for bid, name in BACKENDS:
            self.backend_combo.addItem(name, bid)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        bl.addWidget(self.backend_combo)
        self.dep_label = QLabel("")
        self.dep_label.setWordWrap(True)
        self.dep_label.setStyleSheet("color: gray; font-size: 0.9em;")
        bl.addWidget(self.dep_label)
        layout.addWidget(backend_group)

        # Credentials stacked by backend
        cred_group = QGroupBox("Credentials")
        cred_layout = QVBoxLayout(cred_group)
        cred_layout.addWidget(self._hint("Login details for the selected backend."))
        self.cred_stacked = QStackedWidget()

        oe_page = QWidget()
        oe_layout = QFormLayout(oe_page)
        oe_layout.addRow(QLabel("Use 'Run' to sign in in browser (OIDC), or set client credentials below:"))
        self.openeo_client_id = QLineEdit()
        self.openeo_client_id.setPlaceholderText("Client ID (optional)")
        oe_layout.addRow("Client ID:", self.openeo_client_id)
        self.openeo_client_secret = QLineEdit()
        self.openeo_client_secret.setEchoMode(QLineEdit.Password)
        self.openeo_client_secret.setPlaceholderText("Client secret (optional)")
        oe_layout.addRow("Client secret:", self.openeo_client_secret)
        self.cred_stacked.addWidget(oe_page)

        gee_page = QWidget()
        gee_layout = QFormLayout(gee_page)
        self.gee_project = QLineEdit()
        self.gee_project.setPlaceholderText("your-gee-project")
        gee_layout.addRow("GEE project name:", self.gee_project)
        self.cred_stacked.addWidget(gee_page)

        local_page = QWidget()
        local_layout = QFormLayout(local_page)
        self.cdse_username = QLineEdit()
        self.cdse_username.setPlaceholderText("CDSE username")
        local_layout.addRow("Username:", self.cdse_username)
        self.cdse_password = QLineEdit()
        self.cdse_password.setEchoMode(QLineEdit.Password)
        self.cdse_password.setPlaceholderText("CDSE password")
        local_layout.addRow("Password:", self.cdse_password)
        self.cred_stacked.addWidget(local_page)

        cred_layout.addWidget(self.cred_stacked)
        layout.addWidget(cred_group)

        # AOI
        aoi_group = QGroupBox("Area of interest")
        aoi_layout = QVBoxLayout(aoi_group)
        aoi_layout.addWidget(self._hint("Draw a rectangle on the map to define the analysis area."))
        self.draw_aoi_btn = QPushButton(QIcon(":/pwtt/icon_draw_aoi.svg"), "Draw rectangle on map")
        self.draw_aoi_btn.clicked.connect(self._activate_aoi_tool)
        aoi_layout.addWidget(self.draw_aoi_btn)
        self.clear_aoi_btn = QPushButton("Clear AOI")
        self.clear_aoi_btn.clicked.connect(self._clear_aoi)
        self.clear_aoi_btn.setEnabled(False)
        aoi_layout.addWidget(self.clear_aoi_btn)
        self.aoi_label = QLabel("No area drawn. Click the button, then draw a rectangle on the map.")
        self.aoi_label.setWordWrap(True)
        aoi_layout.addWidget(self.aoi_label)
        layout.addWidget(aoi_group)

        # Parameters
        params_group = QGroupBox("Parameters")
        params_layout = QFormLayout(params_group)
        params_layout.setVerticalSpacing(2)

        self.war_start = QDateEdit()
        self.war_start.setCalendarPopup(True)
        self.war_start.setDate(QDate(2022, 2, 22))
        params_layout.addRow("War start date:", self.war_start)
        params_layout.addRow(self._hint(
            "When hostilities began. Imagery before this date becomes the undamaged baseline."
        ))

        self.inference_start = QDateEdit()
        self.inference_start.setCalendarPopup(True)
        self.inference_start.setDate(QDate(2024, 7, 1))
        params_layout.addRow("Inference start date:", self.inference_start)
        params_layout.addRow(self._hint(
            "Start of the window to assess damage in. Must be on or after war start. "
            "Move this forward to assess damage at a later point in the conflict."
        ))

        self.pre_interval = QSpinBox()
        self.pre_interval.setRange(1, 60)
        self.pre_interval.setValue(12)
        params_layout.addRow("Pre-war interval (months):", self.pre_interval)
        params_layout.addRow(self._hint(
            "How many months before war start to collect baseline imagery. "
            "12 months gives a stable reference; use fewer if pre-war data is scarce."
        ))

        self.post_interval = QSpinBox()
        self.post_interval.setRange(1, 24)
        self.post_interval.setValue(2)
        params_layout.addRow("Post-war interval (months):", self.post_interval)
        params_layout.addRow(self._hint(
            "How many months of post-war imagery to collect from inference start. "
            "1\u20132 months is typical; longer windows capture more passes but may mix damage events."
        ))

        self.include_footprints = QCheckBox("Include building footprints (OSM)")
        self.include_footprints.setChecked(False)
        params_layout.addRow(self.include_footprints)
        params_layout.addRow(self._hint(
            "Overlay OpenStreetMap building footprints on the result to assess per-building damage."
        ))
        layout.addWidget(params_group)

        # Output
        out_group = QGroupBox("Output")
        out_layout = QVBoxLayout(out_group)
        out_layout.addWidget(self._hint("Folder where the result GeoTIFF will be saved."))
        self.output_dir = QgsFileWidget()
        self.output_dir.setStorageMode(QgsFileWidget.GetDirectory)
        out_layout.addWidget(self.output_dir)
        layout.addWidget(out_group)

        # Run button
        self.run_btn = QPushButton(QIcon(":/pwtt/icon_run.svg"), "Run")
        self.run_btn.clicked.connect(self._run)
        layout.addWidget(self.run_btn)

        layout.addStretch()
        scroll.setWidget(container)
        self.setWidget(scroll)

    # ── Backend / credentials ─────────────────────────────────────────────────

    def _on_backend_changed(self, index):
        backend_id = self.backend_combo.currentData()
        self.cred_stacked.setCurrentIndex([b[0] for b in BACKENDS].index(backend_id))
        BackendClass = _get_backend_class(backend_id)
        if BackendClass is None:
            self.dep_label.setText("Backend not available (module missing).")
            self.dep_label.setStyleSheet("color: orange; font-size: 0.9em;")
        else:
            backend = BackendClass()
            ok, msg = backend.check_dependencies()
            if ok:
                self.dep_label.setText("Dependencies: OK")
                self.dep_label.setStyleSheet("color: green; font-size: 0.9em;")
            else:
                self.dep_label.setText(msg if msg else "Missing dependencies.")
                self.dep_label.setStyleSheet("color: orange; font-size: 0.9em;")

    def _get_credentials(self, backend_id):
        if backend_id == "openeo":
            return {
                "client_id": self.openeo_client_id.text().strip() or None,
                "client_secret": self.openeo_client_secret.text().strip() or None,
            }
        if backend_id == "gee":
            return {"project": self.gee_project.text().strip()}
        if backend_id == "local":
            return {
                "username": self.cdse_username.text().strip(),
                "password": self.cdse_password.text(),
            }
        return {}

    # ── AOI ───────────────────────────────────────────────────────────────────

    def _activate_aoi_tool(self):
        self._clear_rubber_band()
        canvas = self.iface.mapCanvas()
        if self.map_tool is None:
            from .aoi_tool import PWTTMapToolExtent
            self.map_tool = PWTTMapToolExtent(canvas, self._on_aoi_drawn)
        self._previous_map_tool = canvas.mapTool()
        canvas.setMapTool(self.map_tool)
        self.iface.messageBar().pushMessage(
            "PWTT", "Draw a rectangle on the map to set the area of interest.",
            level=Qgis.Info, duration=5,
        )

    def _on_aoi_drawn(self, wkt, rect):
        if wkt is None or rect is None:
            self.iface.messageBar().pushMessage(
                "PWTT", "Please draw a rectangle with non-zero area.",
                level=Qgis.Warning, duration=5,
            )
            try:
                self.iface.mapCanvas().setMapTool(self._previous_map_tool)
            except Exception:
                pass
            return
        self.aoi_wkt = wkt
        self.aoi_rect = rect
        self.aoi_label.setText(
            f"AOI: {rect.xMinimum():.4f}, {rect.yMinimum():.4f} \u2014 "
            f"{rect.xMaximum():.4f}, {rect.yMaximum():.4f} (WGS84)"
        )
        self.clear_aoi_btn.setEnabled(True)
        self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        self._draw_rubber_band(rect)

    def _draw_rubber_band(self, rect_4326):
        """Draw a persistent rectangle on the canvas for the current AOI (in EPSG:4326)."""
        canvas = self.iface.mapCanvas()
        self._clear_rubber_band()
        geom = QgsGeometry.fromRect(rect_4326)
        canvas_crs = canvas.mapSettings().destinationCrs()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != src_crs:
            transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
            geom.transform(transform)
        self._rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._rubber_band.setColor(QColor(255, 100, 0, 50))
        self._rubber_band.setStrokeColor(QColor(255, 100, 0, 220))
        self._rubber_band.setWidth(2)
        self._rubber_band.setToGeometry(geom, None)

    def _clear_rubber_band(self):
        if self._rubber_band is not None:
            self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self._rubber_band = None

    def _clear_aoi(self):
        self._clear_rubber_band()
        self.aoi_wkt = None
        self.aoi_rect = None
        self.aoi_label.setText("No area drawn. Click the button, then draw a rectangle on the map.")
        self.clear_aoi_btn.setEnabled(False)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        self.gee_project.setText(s.value("gee_project", ""))
        self.openeo_client_id.setText(s.value("openeo_client_id", ""))
        self.openeo_client_secret.setText(s.value("openeo_client_secret", ""))
        self.cdse_username.setText(s.value("cdse_username", ""))
        self.cdse_password.setText(s.value("cdse_password", ""))
        out = s.value("output_dir", "")
        if out:
            self.output_dir.setFilePath(out)
        s.endGroup()

    def _save_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        s.setValue("gee_project", self.gee_project.text())
        s.setValue("openeo_client_id", self.openeo_client_id.text())
        s.setValue("openeo_client_secret", self.openeo_client_secret.text())
        s.setValue("cdse_username", self.cdse_username.text())
        s.setValue("cdse_password", self.cdse_password.text())
        s.setValue("output_dir", self.output_dir.filePath())
        s.endGroup()

    def closeEvent(self, event):
        self._clear_rubber_band()
        canvas = self.iface.mapCanvas()
        if self.map_tool and canvas.mapTool() == self.map_tool and self._previous_map_tool:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except Exception:
                pass
        super().closeEvent(event)

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        if not self.aoi_wkt:
            QMessageBox.warning(self, "PWTT", "Please draw an area of interest on the map first.")
            return
        war = self.war_start.date()
        inf = self.inference_start.date()
        if inf < war:
            QMessageBox.warning(
                self, "PWTT",
                "Inference start date should be on or after war start date.",
            )
            return
        backend_id = self.backend_combo.currentData()
        BackendClass = _get_backend_class(backend_id)
        if BackendClass is None:
            QMessageBox.warning(
                self, "PWTT",
                f"Backend '{backend_id}' is not available. Check that the required package is installed.",
            )
            return
        backend = BackendClass()
        ok, msg = backend.check_dependencies()
        if not ok:
            QMessageBox.warning(self, "PWTT", msg)
            return
        credentials = self._get_credentials(backend_id)
        if not _auth_with_progress(backend, credentials, backend_id, parent=self):
            QMessageBox.warning(self, "PWTT", "Authentication failed. Check your credentials.")
            return
        self._save_settings()
        out_dir = self.output_dir.filePath()
        if not out_dir:
            QMessageBox.warning(self, "PWTT", "Please choose an output directory.")
            return
        os.makedirs(out_dir, exist_ok=True)

        from ..core import job_store
        job = job_store.create_job(
            backend_id=backend_id,
            aoi_wkt=self.aoi_wkt,
            war_start=self.war_start.date().toString("yyyy-MM-dd"),
            inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
            pre_interval=self.pre_interval.value(),
            post_interval=self.post_interval.value(),
            output_dir=out_dir,
            include_footprints=self.include_footprints.isChecked(),
        )
        self.jobs_dock.launch_job(job, backend)
