# -*- coding: utf-8 -*-
"""PWTT dock panels: controls and run log."""

import os
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QVBoxLayout,
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
)
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsSettings
from qgis.gui import QgsFileWidget


BACKENDS = [
    ("openeo", "openEO (recommended)"),
    ("gee", "Google Earth Engine"),
    ("local", "Local Processing"),
]


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


class PWTTLogDock(QDockWidget):
    """Dockable run-log panel: progress bar + scrollable log."""

    def __init__(self, parent=None):
        super().__init__("PWTT — Run Log", parent)
        self.setObjectName("PWTTLogDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.WidgetWidth)
        layout.addWidget(self.log_text)

        self.setWidget(w)


class PWTTControlsDock(QDockWidget):
    """Dockable controls panel: backend, credentials, AOI, parameters, output, run."""

    # Emitted from background thread via _on_status_message; connected to log dock on main thread
    _status_signal = pyqtSignal(str)

    def __init__(self, iface, plugin_dir, log_dock, parent=None):
        super().__init__("PWTT — Damage Detection", parent)
        self.setObjectName("PWTTControlsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.log_dock = log_dock
        self.aoi_wkt = None
        self.aoi_rect = None
        self.map_tool = None
        self._previous_map_tool = None
        self._task = None

        self._status_signal.connect(self.log_dock.log_text.append)
        self._build_ui()
        self._load_settings()
        self._on_backend_changed(self.backend_combo.currentIndex())

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
        self.draw_aoi_btn = QPushButton(QIcon(":/pwtt/icon_draw_aoi.svg"), "Draw rectangle on map")
        self.draw_aoi_btn.clicked.connect(self._activate_aoi_tool)
        aoi_layout.addWidget(self.draw_aoi_btn)
        self.aoi_label = QLabel("No area drawn. Click the button, then draw a rectangle on the map.")
        self.aoi_label.setWordWrap(True)
        aoi_layout.addWidget(self.aoi_label)
        layout.addWidget(aoi_group)

        # Parameters
        params_group = QGroupBox("Parameters")
        params_layout = QFormLayout(params_group)
        self.war_start = QDateEdit()
        self.war_start.setCalendarPopup(True)
        self.war_start.setDate(QDate(2022, 2, 22))
        params_layout.addRow("War start date:", self.war_start)
        self.inference_start = QDateEdit()
        self.inference_start.setCalendarPopup(True)
        self.inference_start.setDate(QDate(2024, 7, 1))
        params_layout.addRow("Inference start date:", self.inference_start)
        self.pre_interval = QSpinBox()
        self.pre_interval.setRange(1, 60)
        self.pre_interval.setValue(12)
        params_layout.addRow("Pre-war interval (months):", self.pre_interval)
        self.post_interval = QSpinBox()
        self.post_interval.setRange(1, 24)
        self.post_interval.setValue(2)
        params_layout.addRow("Post-war interval (months):", self.post_interval)
        self.include_footprints = QCheckBox("Include building footprints (OSM)")
        self.include_footprints.setChecked(False)
        params_layout.addRow(self.include_footprints)
        layout.addWidget(params_group)

        # Output
        out_group = QGroupBox("Output")
        out_layout = QVBoxLayout(out_group)
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

    def _activate_aoi_tool(self):
        canvas = self.iface.mapCanvas()
        if self.map_tool is None:
            from .aoi_tool import PWTTMapToolExtent
            self.map_tool = PWTTMapToolExtent(canvas, self._on_aoi_drawn)
        self._previous_map_tool = canvas.mapTool()
        canvas.setMapTool(self.map_tool)
        self._status_signal.emit("Draw a rectangle on the map to set the area of interest.")

    def _on_aoi_drawn(self, wkt, rect):
        if wkt is None or rect is None:
            self._status_signal.emit("Please draw a rectangle with non-zero area.")
            try:
                self.iface.mapCanvas().setMapTool(self._previous_map_tool)
            except Exception:
                pass
            return
        self.aoi_wkt = wkt
        self.aoi_rect = rect
        self.aoi_label.setText(
            f"AOI: {rect.xMinimum():.4f}, {rect.yMinimum():.4f} — "
            f"{rect.xMaximum():.4f}, {rect.yMaximum():.4f} (WGS84)"
        )
        self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        self._status_signal.emit("AOI set.")

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
        canvas = self.iface.mapCanvas()
        if self.map_tool and canvas.mapTool() == self.map_tool and self._previous_map_tool:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except Exception:
                pass
        super().closeEvent(event)

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
        if not backend.authenticate(credentials):
            QMessageBox.warning(self, "PWTT", "Authentication failed. Check your credentials.")
            return
        self._save_settings()
        out_dir = self.output_dir.filePath()
        if not out_dir:
            QMessageBox.warning(self, "PWTT", "Please choose an output directory.")
            return
        os.makedirs(out_dir, exist_ok=True)

        from ..core.pwtt_task import PWTTRunTask
        from qgis.core import QgsApplication
        self._task = PWTTRunTask(
            backend=backend,
            aoi_wkt=self.aoi_wkt,
            war_start=self.war_start.date().toString("yyyy-MM-dd"),
            inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
            pre_interval=self.pre_interval.value(),
            post_interval=self.post_interval.value(),
            output_dir=out_dir,
            include_footprints=self.include_footprints.isChecked(),
        )
        self._task.taskCompleted.connect(self._on_task_completed)
        self._task.taskTerminated.connect(self._on_task_terminated)
        if hasattr(self._task, "progressChanged"):
            self._task.progressChanged.connect(
                lambda v: self.log_dock.progress_bar.setValue(int(v))
            )
        self._task.on_status_message(self._on_status_message)
        QgsApplication.taskManager().addTask(self._task)

        self.run_btn.setEnabled(False)
        self.log_dock.progress_bar.setValue(0)
        self.log_dock.log_text.clear()
        self.log_dock.log_text.append("Task started…")
        # Bring the log dock into view so the user can watch progress
        self.log_dock.show()
        self.log_dock.raise_()

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

    def _on_status_message(self, msg: str):
        """Called from background thread — signal bridges to main thread."""
        self._status_signal.emit(msg)

    def _on_task_completed(self):
        self.run_btn.setEnabled(True)
        self.log_dock.progress_bar.setValue(100)
        self.log_dock.log_text.append("Done.")
        self._task = None

    def _on_task_terminated(self):
        self.run_btn.setEnabled(True)
        task = self._task
        self._task = None
        if task and task.exception:
            err = str(task.exception)
            self.log_dock.log_text.append(f"<b>Task failed:</b> {err}")
            if task.error_detail:
                self.log_dock.log_text.append(f"<pre>{task.error_detail}</pre>")
            QMessageBox.critical(self, "PWTT — Error", err)
        elif task and task.isCanceled():
            self.log_dock.log_text.append("Task was cancelled by user.")
        else:
            self.log_dock.log_text.append("Task terminated unexpectedly (no error details available).")
