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
    """Run backend.authenticate() in a background QThread with a progress/auth dialog.

    Raises RuntimeError on authentication failure or user cancellation.
    """
    import webbrowser as _wb
    from qgis.PyQt.QtCore import QThread, pyqtSignal
    from qgis.PyQt.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QProgressDialog, QApplication,
    )

    is_oidc = backend_id == "openeo" and not (credentials or {}).get("client_id")

    class _Worker(QThread):
        auth_url_ready = pyqtSignal(str)

        def __init__(self, b, c):
            super().__init__()
            self.b = b
            self.c = c
            self.ok = False
            self.error_msg = ""

        def run(self):
            try:
                self.ok = self.b.authenticate(self.c)
                if not self.ok:
                    self.error_msg = "Authentication failed. Check your credentials."
            except Exception as e:
                self.ok = False
                self.error_msg = str(e)

    worker = _Worker(backend, credentials)
    canceled = [False]

    if is_oidc and parent is not None:
        # ── openEO OIDC device code flow ──────────────────────────────────────
        dlg = QDialog(parent)
        dlg.setWindowTitle("PWTT \u2014 openEO Sign In")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumWidth(440)
        layout = QVBoxLayout(dlg)

        status_lbl = QLabel("Connecting to openEO CDSE\u2026")
        status_lbl.setWordWrap(True)
        layout.addWidget(status_lbl)

        url_lbl = QLabel()
        url_lbl.setWordWrap(True)
        url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_lbl.hide()
        layout.addWidget(url_lbl)

        url_btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy URL")
        copy_btn.setEnabled(False)
        open_btn = QPushButton("Open in Browser")
        open_btn.setEnabled(False)
        url_btn_row.addWidget(copy_btn)
        url_btn_row.addWidget(open_btn)
        layout.addLayout(url_btn_row)

        cancel_btn = QPushButton("Cancel")
        layout.addWidget(cancel_btn)

        detected_url = [None]

        def _on_url_ready(url):
            detected_url[0] = url
            url_lbl.setText(url)
            url_lbl.show()
            copy_btn.setEnabled(True)
            open_btn.setEnabled(True)
            status_lbl.setText(
                "Visit the URL below and approve the sign-in, then wait here:"
            )
            dlg.adjustSize()

        def _on_copy():
            if detected_url[0]:
                QApplication.clipboard().setText(detected_url[0])

        def _on_open():
            if detected_url[0]:
                _wb.open(detected_url[0])

        def _on_cancel():
            canceled[0] = True
            try:
                worker.finished.disconnect()
            except Exception:
                pass
            dlg.reject()

        worker.auth_url_ready.connect(_on_url_ready)
        worker.finished.connect(dlg.accept)
        copy_btn.clicked.connect(_on_copy)
        open_btn.clicked.connect(_on_open)
        cancel_btn.clicked.connect(_on_cancel)

        # Intercept webbrowser calls made by the openEO OIDC library so the
        # URL appears in our dialog instead of auto-opening or printing to stdout.
        _orig_open = _wb.open
        _orig_open_new = _wb.open_new
        _orig_open_tab = _wb.open_new_tab

        def _intercept(url, *a, **kw):
            worker.auth_url_ready.emit(url)
            return True  # Tell openEO the browser "opened" successfully

        _wb.open = _intercept
        _wb.open_new = _intercept
        _wb.open_new_tab = _intercept
        try:
            worker.start()
            dlg.exec_()
        finally:
            _wb.open = _orig_open
            _wb.open_new = _orig_open_new
            _wb.open_new_tab = _orig_open_tab

        if canceled[0]:
            worker.wait(2000)
            raise RuntimeError("Authentication cancelled.")

        worker.wait()

    else:
        # ── Standard busy-spinner progress dialog ─────────────────────────────
        dlg = QProgressDialog("Authenticating\u2026", "Cancel", 0, 0, parent)
        dlg.setWindowTitle("PWTT")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)

        worker.finished.connect(dlg.close)
        worker.start()
        dlg.exec_()

        if dlg.wasCanceled():
            worker.wait(2000)
            raise RuntimeError("Authentication cancelled.")

        worker.wait()

    if not worker.ok:
        raise RuntimeError(worker.error_msg or "Authentication failed. Check your credentials.")


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
            "verify_ssl": s.value("openeo_verify_ssl", True, type=bool),
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
        _auth_with_progress(backend, creds, backend_id, parent)  # raises on failure
    else:
        try:
            if not backend.authenticate(creds):
                raise RuntimeError("Authentication failed. Check your credentials.")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(str(e)) from e
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

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(_dock_title("PWTT \u2014 Jobs", plugin_dir), parent)
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
        self.job_table = QTableWidget(0, 5)
        self.job_table.setHorizontalHeaderLabels(["Status", "Backend", "Remote Job", "Dates", "Created"])
        self.job_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.job_table.setSelectionMode(QTableWidget.SingleSelection)
        self.job_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.job_table.verticalHeader().hide()
        hdr = self.job_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.job_table.setMaximumHeight(180)
        self.job_table.itemSelectionChanged.connect(self._on_job_selected)
        layout.addWidget(self.job_table)

        # Action buttons
        btn_row = QHBoxLayout()
        self.load_btn = QPushButton("Load")
        self.resume_btn = QPushButton("Resume")
        self.stop_btn = QPushButton("Stop")
        self.cancel_btn = QPushButton("Cancel")
        self.rerun_btn = QPushButton("Rerun")
        self.delete_btn = QPushButton("Delete")
        for btn in (self.load_btn, self.resume_btn, self.stop_btn, self.cancel_btn,
                     self.rerun_btn, self.delete_btn):
            btn.setEnabled(False)
            btn_row.addWidget(btn)
        self.load_btn.setToolTip("Load job AOI to map and parameters to controls panel")
        self.load_btn.clicked.connect(self._load_selected)
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

            # Remote Job ID (e.g. openEO batch job id)
            remote_id = job.get("remote_job_id") or ""
            # Show truncated id for readability
            display_id = remote_id[-12:] if len(remote_id) > 12 else remote_id
            rid_item = QTableWidgetItem(display_id)
            rid_item.setToolTip(remote_id)  # full id on hover
            self.job_table.setItem(row, 2, rid_item)

            # Dates
            dates = f"{job['war_start'][:7]} \u2192 {job['inference_start'][:7]}"
            self.job_table.setItem(row, 3, QTableWidgetItem(dates))

            # Created
            created = job.get("created_at", "")[:16].replace("T", " ")
            self.job_table.setItem(row, 4, QTableWidgetItem(created))

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
            for btn in (self.load_btn, self.resume_btn, self.stop_btn,
                         self.cancel_btn, self.rerun_btn, self.delete_btn):
                btn.setEnabled(False)
            self.log_text.clear()
            self.progress_bar.setValue(0)
            return

        st = job["status"]
        self.load_btn.setEnabled(True)
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
            remote_job_id=job.get("remote_job_id"),
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
        output_tif = getattr(task, "output_tif", None)
        footprints = getattr(task, "footprints_gpkg", None)
        update_fields = dict(
            status=job_store.STATUS_COMPLETED,
            output_tif=output_tif,
            footprints_gpkg=footprints,
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
        if footprints:
            done_parts.append(f"Footprints: {footprints}")
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

        if task.products_offline:
            job_store.update_job(
                job_id,
                status=job_store.STATUS_WAITING_ORDERS,
                offline_product_ids=task.offline_product_ids,
            )
            ids_str = ", ".join(task.offline_product_ids[:5])
            if len(task.offline_product_ids) > 5:
                ids_str += f" (+{len(task.offline_product_ids) - 5} more)"
            self._job_logs.setdefault(job_id, []).append(
                f"<b>Products offline</b> — staging from cold storage.<br>"
                f"Product IDs: {ids_str}<br>"
                f"Will auto-check every 2 min and resume when available."
            )
        elif task.isCanceled():
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

    def _resume_selected(self):
        job = self._get_selected_job()
        if not job:
            return
        try:
            backend = _create_and_auth_backend(job["backend_id"], parent=self)
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
            output_dir="",  # set below
            include_footprints=old["include_footprints"],
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
#  PWTTOpenEOJobsDock — list all openEO remote jobs, download results
# ═══════════════════════════════════════════════════════════════════════════════

class PWTTOpenEOJobsDock(QDockWidget):
    """List openEO batch jobs from the server and download/add results."""

    _jobs_loaded = pyqtSignal(list)  # emitted from worker thread
    _log_signal = pyqtSignal(str)   # thread-safe log append

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(_dock_title("PWTT \u2014 openEO Jobs", plugin_dir), parent)
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
        self.job_table = QTableWidget(0, 6)
        self.job_table.setHorizontalHeaderLabels(
            ["Job ID", "Status", "Progress", "Created", "Updated", "Title"]
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
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)
        layout.addWidget(self.job_table)

        # Action buttons
        btn_row = QHBoxLayout()
        self.download_btn = QPushButton("Download && Add to Map")
        self.download_btn.setEnabled(False)
        self.download_btn.setToolTip("Download result GeoTIFF and add as layer")
        self.download_btn.clicked.connect(self._download_selected)
        btn_row.addWidget(self.download_btn)
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
                backend = _create_and_auth_backend("openeo", parent=self)
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

            # Title
            self.job_table.setItem(row, 5, QTableWidgetItem(j.get("title") or ""))

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
            self.logs_btn.setEnabled(False)
            self.delete_remote_btn.setEnabled(False)
            return
        j = self._remote_jobs[row]
        self.download_btn.setEnabled(j["status"] == "finished")
        self.logs_btn.setEnabled(True)
        self.delete_remote_btn.setEnabled(j["status"] not in ("running", "queued"))

    def _get_selected_remote_id(self):
        row = self.job_table.currentRow()
        if row < 0:
            return None
        item = self.job_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

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

        # Determine output directory
        proj_path = QgsProject.instance().absolutePath()
        base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
        out_dir = os.path.join(base_dir, job_id)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"pwtt_{job_id}.tif")

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
        from qgis.core import QgsRasterLayer
        layer = QgsRasterLayer(path, f"openEO result ({job_id})", "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self.log_text.append(f"Layer added: openEO result ({job_id})")
        else:
            self.log_text.append("Failed to load layer \u2014 file may be invalid.")

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

        self._build_ui()
        self._load_settings()
        self._on_backend_changed(self.backend_combo.currentIndex())

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
        self.install_deps_btn = QPushButton("Install Dependencies")
        self.install_deps_btn.hide()
        self.install_deps_btn.clicked.connect(self._install_backend_deps)
        bl.addWidget(self.install_deps_btn)
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
        self.openeo_verify_ssl = QCheckBox("Verify TLS certificates (HTTPS)")
        self.openeo_verify_ssl.setChecked(True)
        self.openeo_verify_ssl.setToolTip(
            "Turn off only if listing jobs or downloading results fails with "
            "SSL/certificate errors. Result files are served from a different host than the API."
        )
        self.openeo_verify_ssl.stateChanged.connect(self._persist_openeo_verify_ssl)
        oe_layout.addRow(self.openeo_verify_ssl)
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
        from ..core import deps
        backend_id = self.backend_combo.currentData()
        self.cred_stacked.setCurrentIndex([b[0] for b in BACKENDS].index(backend_id))

        missing_imports, pip_names = deps.backend_missing(backend_id)
        self._pending_pip_install = pip_names  # stash for install button

        if missing_imports:
            if pip_names:
                self.dep_label.setText(
                    f"Missing: {', '.join(missing_imports)}"
                )
                self.install_deps_btn.show()
            else:
                self.dep_label.setText(
                    f"Missing: {', '.join(missing_imports)} "
                    f"(should be provided by QGIS \u2014 check your installation)"
                )
                self.install_deps_btn.hide()
            self.dep_label.setStyleSheet("color: orange; font-size: 0.9em;")
        else:
            self.dep_label.setText("Dependencies: OK")
            self.dep_label.setStyleSheet("color: green; font-size: 0.9em;")
            self.install_deps_btn.hide()

    def _install_backend_deps(self):
        """Install missing backend packages via the deps module."""
        from ..core import deps
        names = getattr(self, "_pending_pip_install", [])
        if not names:
            return
        if deps.install_with_dialog(names, parent=self):
            self._on_backend_changed(self.backend_combo.currentIndex())

    def _get_credentials(self, backend_id):
        if backend_id == "openeo":
            return {
                "client_id": self.openeo_client_id.text().strip() or None,
                "client_secret": self.openeo_client_secret.text().strip() or None,
                "verify_ssl": self.openeo_verify_ssl.isChecked(),
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

    # ── Load job parameters ──────────────────────────────────────────────────

    def load_job_params(self, job):
        """Populate controls from a saved job and show its AOI on the map."""
        from qgis.core import QgsRectangle
        from ..core.utils import wkt_to_bbox

        # Backend
        backend_ids = [b[0] for b in BACKENDS]
        if job["backend_id"] in backend_ids:
            self.backend_combo.setCurrentIndex(backend_ids.index(job["backend_id"]))

        # Dates & intervals
        ws = job.get("war_start", "")
        if ws:
            self.war_start.setDate(QDate.fromString(ws, "yyyy-MM-dd"))
        ins = job.get("inference_start", "")
        if ins:
            self.inference_start.setDate(QDate.fromString(ins, "yyyy-MM-dd"))
        if job.get("pre_interval"):
            self.pre_interval.setValue(job["pre_interval"])
        if job.get("post_interval"):
            self.post_interval.setValue(job["post_interval"])

        self.include_footprints.setChecked(job.get("include_footprints", False))

        # Output directory
        out = job.get("output_dir", "")
        if out:
            self.output_dir.setFilePath(out)

        # AOI — parse WKT, set rubber band, zoom
        aoi_wkt = job.get("aoi_wkt")
        if aoi_wkt:
            bbox = wkt_to_bbox(aoi_wkt)
            if bbox:
                west, south, east, north = bbox
                rect = QgsRectangle(west, south, east, north)
                self._on_aoi_drawn(aoi_wkt, rect)

                # Zoom to AOI
                canvas = self.iface.mapCanvas()
                canvas_crs = canvas.mapSettings().destinationCrs()
                src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
                geom = QgsGeometry.fromRect(rect)
                if canvas_crs != src_crs:
                    transform = QgsCoordinateTransform(
                        src_crs, canvas_crs, QgsProject.instance()
                    )
                    geom.transform(transform)
                canvas.setExtent(geom.boundingBox())
                canvas.refresh()

        # Make sure controls dock is visible
        self.show()
        self.raise_()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _persist_openeo_verify_ssl(self, _state=None):
        s = QgsSettings()
        s.beginGroup("PWTT")
        s.setValue("openeo_verify_ssl", self.openeo_verify_ssl.isChecked())
        s.endGroup()
        od = getattr(self, "openeo_dock", None)
        if od is not None:
            od._conn = None

    def _load_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        self.gee_project.setText(s.value("gee_project", ""))
        self.openeo_client_id.setText(s.value("openeo_client_id", ""))
        self.openeo_client_secret.setText(s.value("openeo_client_secret", ""))
        self.openeo_verify_ssl.blockSignals(True)
        self.openeo_verify_ssl.setChecked(s.value("openeo_verify_ssl", True, type=bool))
        self.openeo_verify_ssl.blockSignals(False)
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
        s.setValue("openeo_verify_ssl", self.openeo_verify_ssl.isChecked())
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
        from ..core import deps

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

        # ── Check backend dependencies (offer install if missing) ─────────
        missing, pip_names = deps.backend_missing(backend_id)
        if missing:
            reply = QMessageBox.question(
                self, "PWTT",
                f"Missing packages: {', '.join(pip_names)}\n\nInstall now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                if not deps.install_with_dialog(pip_names, parent=self):
                    return
                # Re-check after install
                missing, _ = deps.backend_missing(backend_id)
            if missing:
                QMessageBox.warning(
                    self, "PWTT",
                    f"Cannot run: missing {', '.join(missing)}.",
                )
                return
            # Refresh the deps label
            self._on_backend_changed(self.backend_combo.currentIndex())

        # ── Check footprint dependencies if enabled ───────────────────────
        if self.include_footprints.isChecked():
            fp_missing, fp_pip = deps.footprint_missing()
            if fp_missing:
                reply = QMessageBox.question(
                    self, "PWTT",
                    f"Building footprints require: {', '.join(fp_pip)}\n\nInstall now?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    if not deps.install_with_dialog(fp_pip, parent=self):
                        return
                    fp_missing, _ = deps.footprint_missing()
                if fp_missing:
                    QMessageBox.warning(
                        self, "PWTT",
                        f"Cannot compute footprints: missing {', '.join(fp_missing)}.\n"
                        f"Uncheck the footprints option or install the packages.",
                    )
                    return

        # ── Create backend and authenticate ───────────────────────────────
        BackendClass = _get_backend_class(backend_id)
        if BackendClass is None:
            QMessageBox.warning(
                self, "PWTT",
                f"Backend '{backend_id}' is not available.",
            )
            return
        backend = BackendClass()
        ok, msg = backend.check_dependencies()
        if not ok:
            QMessageBox.warning(self, "PWTT", msg)
            return
        credentials = self._get_credentials(backend_id)
        try:
            _auth_with_progress(backend, credentials, backend_id, parent=self)
        except RuntimeError as e:
            if str(e) != "Authentication cancelled.":
                QMessageBox.warning(self, "PWTT", str(e))
            return
        self._save_settings()
        base_dir = self.output_dir.filePath()
        if not base_dir:
            # Default to project folder or home
            proj_path = QgsProject.instance().absolutePath()
            base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
            self.output_dir.setFilePath(base_dir)

        from ..core import job_store
        job = job_store.create_job(
            backend_id=backend_id,
            aoi_wkt=self.aoi_wkt,
            war_start=self.war_start.date().toString("yyyy-MM-dd"),
            inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
            pre_interval=self.pre_interval.value(),
            post_interval=self.post_interval.value(),
            output_dir="",  # will be set below
            include_footprints=self.include_footprints.isChecked(),
        )
        # Output folder: base_dir / job_id
        job["output_dir"] = os.path.join(base_dir, job["id"])
        os.makedirs(job["output_dir"], exist_ok=True)
        job_store.save_job(job)
        self.jobs_dock.launch_job(job, backend)
