# -*- coding: utf-8 -*-
"""PWTT dock panels: controls (damage detection)."""

import os
import webbrowser

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
    QDoubleSpinBox,
    QCheckBox,
    QProgressBar,
    QGroupBox,
    QFormLayout,
    QMessageBox,
    QScrollArea,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QInputDialog,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
)
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsWkbTypes,
)
from qgis.gui import QgsFileWidget, QgsRubberBand

from .backend_auth import (
    create_and_auth_backend as backend_auth_create_and_auth_backend,
    clear_gee_credentials_from_storage,
    clear_openeo_credentials_from_storage,
    confirm_local_processing_storage,
    ensure_footprint_dependencies,
    is_message_box_yes,
    save_openeo_credentials_to_settings,
    test_remote_backend_credentials,
)
from .dock_common import BACKENDS, dock_title, job_footprints_sources
from ..core.utils import format_iso_date_display, format_ymd_display


class _BatchConfirmDialog:
    """
    Shows run summary + per-AOI checkboxes so user can deselect individual AOIs before confirming.
    """

    def __init__(self, parent, summary_text: str, aois: list):
        self._dialog = QDialog(parent)
        self._dialog.setWindowTitle("PWTT — Confirm run")
        self._dialog.setMinimumWidth(480)

        outer = QVBoxLayout(self._dialog)

        summary_label = QLabel(summary_text)
        summary_label.setWordWrap(True)
        outer.addWidget(summary_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        if len(aois) >= 4:
            warn_frame = QFrame()
            warn_frame.setStyleSheet(
                "background-color: #fff3cd; border: 1px solid #ffc107; border-radius: 4px;"
            )
            warn_layout = QVBoxLayout(warn_frame)
            warn_layout.setContentsMargins(8, 6, 8, 6)
            warn_text = QLabel(
                f"⚠  <b>{len(aois)} jobs queued</b> — this may take a long time and consume "
                "a significant portion of your monthly API quota."
            )
            warn_text.setWordWrap(True)
            warn_layout.addWidget(warn_text)
            try:
                backend_id = parent.backend_combo.currentData()
            except AttributeError:
                backend_id = None
            if backend_id == "openeo":
                link = QLabel(
                    '<a href="https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings">'
                    "Check CDSE balance ↗</a>"
                )
                link.setOpenExternalLinks(True)
                warn_layout.addWidget(link)
            outer.addWidget(warn_frame)

        outer.addWidget(QLabel(f"<b>AOIs to run ({len(aois)}):</b>"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(200)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(2)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        self._checkboxes = []
        for aoi in aois:
            cb = QCheckBox(aoi["name"])
            cb.setChecked(True)
            cb.setProperty("aoi_data", aoi)
            cb.stateChanged.connect(self._update_button)
            inner_layout.addWidget(cb)
            self._checkboxes.append(cb)

        self._buttons = QDialogButtonBox()
        self._run_btn = self._buttons.addButton("Run 0 jobs", QDialogButtonBox.AcceptRole)
        self._buttons.addButton(QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._dialog.accept)
        self._buttons.rejected.connect(self._dialog.reject)
        outer.addWidget(self._buttons)

        self._update_button()

    def _update_button(self, _state=None):
        count = sum(1 for cb in self._checkboxes if cb.isChecked())
        self._run_btn.setText(f"Run {count} job{'s' if count != 1 else ''}")
        self._run_btn.setEnabled(count > 0)

    def exec(self) -> list:
        """Show dialog; return list of confirmed AOI dicts (empty if cancelled)."""
        from qgis.PyQt.QtWidgets import QDialog
        result = self._dialog.exec_()
        if result != QDialog.Accepted:
            return []
        return [
            cb.property("aoi_data")
            for cb in self._checkboxes
            if cb.isChecked()
        ]


class _AoiSplitDialog:
    """
    Shown when a drawn AOI exceeds the backend's per-job size limit.

    Lets the user preview a tile grid on the map, adjust overlap, then confirm.
    exec() returns "tiles", "single", or "cancel".
    confirmed_tiles() returns list of [west, south, east, north] bboxes.
    """

    TILES  = "tiles"
    SINGLE = "single"
    CANCEL = "cancel"

    def __init__(self, parent, bbox: list, backend_id: str, canvas):
        from .dock_common import BACKENDS
        self._bbox       = bbox
        self._backend_id = backend_id
        self._canvas     = canvas
        self._preview_bands: list = []
        self._confirmed_tiles: list = []
        self._action = self.CANCEL

        backend_name = next((n for bid, n in BACKENDS if bid == backend_id), backend_id)

        self._dialog = QDialog(parent)
        self._dialog.setWindowTitle(f"PWTT \u2014 AOI too large for {backend_name}")
        self._dialog.setMinimumWidth(520)
        self._dialog.finished.connect(self._on_dialog_finished)

        outer = QVBoxLayout(self._dialog)

        # ── Info header ──────────────────────────────────────────────────────
        self._info_label = QLabel()
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("color: #b85c00; font-weight: bold;")
        outer.addWidget(self._info_label)

        # ── Overlap control ───────────────────────────────────────────────────
        overlap_row = QHBoxLayout()
        overlap_row.addWidget(QLabel("Tile overlap:"))
        self._overlap_spin = QDoubleSpinBox()
        self._overlap_spin.setRange(0.0, 0.1)
        self._overlap_spin.setSingleStep(0.001)
        self._overlap_spin.setDecimals(3)
        self._overlap_spin.setValue(0.01)
        self._overlap_spin.setSuffix("\u00b0")
        self._overlap_spin.valueChanged.connect(self._on_overlap_changed)
        overlap_row.addWidget(self._overlap_spin)
        overlap_row.addWidget(QLabel("  (extends each tile edge outward)"))
        overlap_row.addStretch()
        outer.addLayout(overlap_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        # ── Quota section ────────────────────────────────────────────────────
        outer.addWidget(QLabel("<b>Quota / processing time</b>"))
        self._quota_label = QLabel()
        self._quota_label.setWordWrap(True)
        outer.addWidget(self._quota_label)

        if backend_id == "openeo":
            link_label = QLabel(
                "Free tier: 10,000 PU/month  "
                '<a href="https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings">'
                "Check balance \u2197</a>"
            )
            link_label.setOpenExternalLinks(True)
            outer.addWidget(link_label)

        warn_lbl = QLabel(
            "Running multiple jobs will take significantly longer than a single job.\n"
            "Large batches may exhaust your monthly API quota."
        )
        warn_lbl.setWordWrap(True)
        warn_lbl.setStyleSheet("color: #666;")
        outer.addWidget(warn_lbl)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep2)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._add_tiles_btn = QPushButton()
        self._add_tiles_btn.clicked.connect(self._on_add_tiles)
        btn_row.addWidget(self._add_tiles_btn)
        add_single_btn = QPushButton("Add as single AOI")
        add_single_btn.clicked.connect(self._on_add_single)
        btn_row.addWidget(add_single_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

        self._refresh_labels()
        self._draw_preview()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _current_tiles(self) -> list:
        from ..core import aoi_splitter
        return aoi_splitter.split_bbox(
            self._bbox, self._backend_id, self._overlap_spin.value()
        )

    def _refresh_labels(self):
        from ..core import aoi_splitter
        tiles      = self._current_tiles()
        n          = len(tiles)
        cols, rows = aoi_splitter.tile_grid_dims(self._bbox, self._backend_id)
        west, south, east, north = self._bbox
        width  = east - west
        height = north - south

        self._info_label.setText(
            f"\u26a0  This area ({width:.2f}\u00b0 \u00d7 {height:.2f}\u00b0) exceeds the backend per-job limit.\n"
            f"It has been split into a {cols} \u00d7 {rows} grid ({n} tiles)."
        )
        self._add_tiles_btn.setText(f"Add {n} tiles to queue")

        if self._backend_id == "gee" and tiles:
            per_mb  = aoi_splitter.estimate_gee_bytes(tiles[0]) / (1024 * 1024)
            safe_mb = aoi_splitter.GEE_GETDOWNLOAD_EFFECTIVE_MAX_BYTES / (1024 * 1024)
            cap_mb  = aoi_splitter.GEE_GETDOWNLOAD_MAX_BYTES / (1024 * 1024)
            self._quota_label.setText(
                f"Estimated per tile: ~{per_mb:.0f} MiB  "
                f"(budget ~{safe_mb:.0f} MiB; EE hard cap ~{cap_mb:.0f} MiB)"
            )
        elif self._backend_id == "openeo" and tiles:
            per_pu   = aoi_splitter.estimate_openeo_pu(tiles[0])
            total_pu = per_pu * n
            self._quota_label.setText(
                f"Estimated per tile: ~{per_pu:.0f} PU  |  Total: ~{total_pu:.0f} PU"
            )
        else:
            self._quota_label.setText("")

    def _draw_preview(self):
        self._clear_preview()
        colours = [
            (255, 100,   0),
            ( 30, 120, 255),
            ( 50, 180,  50),
            (180,  50, 180),
            (220, 180,   0),
        ]
        src_crs    = QgsCoordinateReferenceSystem("EPSG:4326")
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        for i, tile_bbox in enumerate(self._current_tiles()):
            west, south, east, north = tile_bbox
            rect = QgsRectangle(west, south, east, north)
            geom = QgsGeometry.fromRect(rect)
            if canvas_crs != src_crs:
                transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
                geom.transform(transform)
            r, g, b = colours[i % len(colours)]
            rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            rb.setColor(QColor(r, g, b, 30))
            rb.setStrokeColor(QColor(r, g, b, 180))
            rb.setWidth(2)
            rb.setLineStyle(Qt.DashLine)
            rb.setToGeometry(geom, None)
            self._preview_bands.append(rb)

    def _clear_preview(self):
        for rb in self._preview_bands:
            rb.reset(QgsWkbTypes.PolygonGeometry)
        self._preview_bands.clear()

    def _on_overlap_changed(self, _value):
        self._refresh_labels()
        self._draw_preview()

    def _on_add_tiles(self):
        self._confirmed_tiles = self._current_tiles()
        self._action = self.TILES
        self._clear_preview()
        self._dialog.accept()

    def _on_add_single(self):
        self._action = self.SINGLE
        self._clear_preview()
        self._dialog.accept()

    def _on_cancel(self):
        self._action = self.CANCEL
        self._clear_preview()
        self._dialog.reject()

    def _on_dialog_finished(self, _result):
        # Safety net: clear preview if dialog closed via window X button
        self._clear_preview()

    # ── Public API ────────────────────────────────────────────────────────────

    def exec(self) -> str:
        """Show dialog. Returns "tiles", "single", or "cancel"."""
        self._dialog.exec_()
        return self._action

    def confirmed_tiles(self) -> list:
        """List of [west, south, east, north] bboxes confirmed by user."""
        return self._confirmed_tiles


class _LibraryTree(QTreeWidget):
    """QTreeWidget with AOI-to-project drag-and-drop support.
    Emits aoi_moved(aoi_id, target_project_id) when an AOI is dropped onto a project."""

    aoi_moved = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setHeaderHidden(True)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(100)

    def dragEnterEvent(self, event):
        item = self.currentItem()
        if item is not None and item.parent() is not None:
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        target = self.itemAt(event.pos())
        if target is None:
            event.ignore()
            return
        dragged = self.currentItem()
        if dragged is None or dragged.parent() is None:
            event.ignore()
            return
        # Resolve target to its project root
        target_proj = target if target.parent() is None else target.parent()
        # Reject same-project drops (no visual feedback of a no-op)
        if target_proj is dragged.parent():
            event.ignore()
            return
        target_data = target_proj.data(0, Qt.UserRole)
        if target_data and target_data.get("type") == "project":
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event):
        target = self.itemAt(event.pos())
        if target is None:
            event.ignore()
            return
        # Resolve drop target to a project item
        data = target.data(0, Qt.UserRole)
        if data and data.get("type") == "aoi":
            target = target.parent()
            data = target.data(0, Qt.UserRole) if target else None
        if not data or data.get("type") != "project":
            event.ignore()
            return
        dragged = self.currentItem()
        if dragged is None or dragged.parent() is None:
            event.ignore()
            return
        dragged_data = dragged.data(0, Qt.UserRole)
        if not dragged_data or dragged_data.get("type") != "aoi":
            event.ignore()
            return
        # No-op if same project
        current_proj_data = dragged.parent().data(0, Qt.UserRole)
        if current_proj_data and current_proj_data.get("id") == data.get("id"):
            event.ignore()
            return
        self.aoi_moved.emit(dragged_data["id"], data["id"])
        event.accept()


class PWTTControlsDock(QDockWidget):
    """Dockable controls panel: backend, credentials, AOI, parameters, output, run."""

    def __init__(self, iface, plugin_dir, jobs_dock, parent=None):
        super().__init__(dock_title("PWTT \u2014 Damage Detection", plugin_dir), parent)
        self.setObjectName("PWTTControlsDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.jobs_dock = jobs_dock
        # AOI queue: list of dicts with keys id, name, wkt, bbox, tag ('drawn'|'saved'), checked (bool)
        self._queue: list = []
        # Rubber bands keyed by AOI id (including tmp_ ids)
        self._rubber_bands: dict = {}
        self.map_tool = None
        self._previous_map_tool = None

        self._build_ui()
        self._load_settings()
        self._on_backend_changed(self.backend_combo.currentIndex())

    def showEvent(self, event):
        super().showEvent(event)
        # Restore rubber bands after dock is re-shown
        for aoi_entry in self._queue:
            if aoi_entry["id"] not in self._rubber_bands:
                self._draw_rubber_band_for(aoi_entry)
        self._on_backend_changed(self.backend_combo.currentIndex())

    def hideEvent(self, event):
        super().hideEvent(event)
        # Rubber bands remain visible when dock is hidden

    def cleanup_map_canvas(self):
        """Remove all AOI overlays and extent map tool; call before dock teardown."""
        self._clear_all_rubber_bands()
        canvas = self.iface.mapCanvas()
        if self.map_tool and canvas.mapTool() == self.map_tool and self._previous_map_tool:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except Exception:
                pass

    @staticmethod
    def _hint(text: str) -> QLabel:
        """Small grey italic one-liner placed at the top of a group box."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray; font-style: italic; font-size: 0.85em;")
        return lbl

    _AOI_COLOURS = [
        (255, 100,   0),   # orange (first drawn AOI)
        ( 30, 120, 255),   # blue
        ( 50, 180,  50),   # green
        (180,  50, 180),   # purple
        (220, 180,   0),   # amber
    ]

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
        cred_layout.addWidget(self._hint(
            "Login details for the selected backend. "
            "Credentials are stored in QGIS settings (not encrypted). "
            "Clear them after use on shared machines."
        ))
        self.cred_storage_label = QLabel("")
        self.cred_storage_label.setWordWrap(True)
        self.cred_storage_label.setStyleSheet("font-size: 0.9em;")
        cred_layout.addWidget(self.cred_storage_label)
        self.cred_stacked = QStackedWidget()

        oe_page = QWidget()
        oe_layout = QFormLayout(oe_page)
        oe_layout.addRow(QLabel("Create OAuth2 client credentials at the Copernicus Data Space dashboard:"))
        _cdse_url = "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings"
        _get_creds_btn = QPushButton("Get credentials \u2192")
        _get_creds_btn.clicked.connect(lambda: webbrowser.open(_cdse_url))
        oe_layout.addRow(_get_creds_btn)
        self.openeo_client_id = QLineEdit()
        self.openeo_client_id.setPlaceholderText("Client ID")
        oe_layout.addRow("Client ID:", self.openeo_client_id)
        self.openeo_client_secret = QLineEdit()
        self.openeo_client_secret.setEchoMode(QLineEdit.Password)
        self.openeo_client_secret.setPlaceholderText("Client secret")
        oe_layout.addRow("Client secret:", self.openeo_client_secret)
        self.openeo_client_id.editingFinished.connect(self._persist_openeo_credentials)
        self.openeo_client_secret.editingFinished.connect(self._persist_openeo_credentials)
        self.openeo_verify_ssl = QCheckBox("Verify TLS certificates (HTTPS)")
        self.openeo_verify_ssl.setChecked(True)
        self.openeo_verify_ssl.setToolTip(
            "Turn off only if listing jobs or downloading results fails with "
            "SSL/certificate errors. Result files are served from a different host than the API."
        )
        self.openeo_verify_ssl.stateChanged.connect(self._persist_openeo_verify_ssl)
        oe_layout.addRow(self.openeo_verify_ssl)
        self.openeo_test_creds_btn = QPushButton("Test openEO credentials")
        self.openeo_test_creds_btn.setToolTip(
            "Connect to Copernicus openEO and verify Client ID / Client Secret (client-credentials flow)."
        )
        self.openeo_test_creds_btn.clicked.connect(self._test_openeo_credentials)
        oe_layout.addRow(self.openeo_test_creds_btn)
        self.openeo_clear_creds_btn = QPushButton("Clear saved openEO credentials")
        self.openeo_clear_creds_btn.setToolTip(
            "Remove Client ID and Client Secret from QGIS settings and reset the openEO connection."
        )
        self.openeo_clear_creds_btn.clicked.connect(self._clear_saved_openeo_credentials)
        oe_layout.addRow(self.openeo_clear_creds_btn)
        self.cred_stacked.addWidget(oe_page)

        gee_page = QWidget()
        gee_layout = QFormLayout(gee_page)
        self.gee_project = QLineEdit()
        self.gee_project.setPlaceholderText("your-gee-project")
        gee_layout.addRow("GEE project name:", self.gee_project)
        # OAuth 2.0 client credentials (preferred)
        # Create at: https://console.cloud.google.com/apis/credentials
        # → Create credentials → OAuth client ID → Desktop app
        gee_layout.addRow(
            QLabel(
                "Create OAuth 2.0 client credentials (Desktop app) in Google Cloud Console:"
            )
        )
        _gee_creds_url = "https://console.cloud.google.com/apis/credentials"
        _gee_get_creds_btn = QPushButton("Get credentials \u2192")
        _gee_get_creds_btn.setToolTip(
            "Opens the Google Cloud APIs & Services credentials page. "
            "Create credentials → OAuth client ID → Desktop app, then paste Client ID and secret below."
        )
        _gee_get_creds_btn.clicked.connect(lambda: webbrowser.open(_gee_creds_url))
        gee_layout.addRow(_gee_get_creds_btn)
        self.gee_client_id = QLineEdit()
        self.gee_client_id.setPlaceholderText(
            "123456-abc.apps.googleusercontent.com (preferred)"
        )
        gee_layout.addRow("Client ID:", self.gee_client_id)
        self.gee_client_secret = QLineEdit()
        self.gee_client_secret.setEchoMode(QLineEdit.Password)
        self.gee_client_secret.setPlaceholderText("Client secret")
        gee_layout.addRow("Client secret:", self.gee_client_secret)
        self.gee_test_creds_btn = QPushButton("Test GEE credentials")
        self.gee_test_creds_btn.setToolTip(
            "Initialize Earth Engine with the project and OAuth client above "
            "(OAuth may open a browser once to complete sign-in)."
        )
        self.gee_test_creds_btn.clicked.connect(self._test_gee_credentials)
        gee_layout.addRow(self.gee_test_creds_btn)
        self.gee_clear_creds_btn = QPushButton("Clear saved GEE credentials")
        self.gee_clear_creds_btn.setToolTip(
            "Remove project and OAuth client from QGIS settings; delete the Earth Engine "
            "token file (used by browser OAuth and installed-app OAuth); clear in-memory EE state."
        )
        self.gee_clear_creds_btn.clicked.connect(self._clear_saved_gee_credentials)
        gee_layout.addRow(self.gee_clear_creds_btn)
        self.cred_stacked.addWidget(gee_page)

        local_page = QWidget()
        local_outer = QVBoxLayout(local_page)
        local_outer.setContentsMargins(0, 0, 0, 0)
        self.local_source_combo = QComboBox()
        self.local_source_combo.addItem(
            "Copernicus Data Space (CDSE)", "cdse"
        )
        self.local_source_combo.addItem(
            "ASF (Earthdata Login)", "asf"
        )
        self.local_source_combo.addItem(
            "Microsoft Planetary Computer", "pc"
        )
        self.local_source_combo.setToolTip(
            "Where to download Sentinel-1 IW GRD for local processing.\n"
            "ASF and Planetary Computer avoid CDSE cold-storage delays."
        )
        local_outer.addWidget(QLabel("GRD data source:"))
        local_outer.addWidget(self.local_source_combo)

        self.local_cred_stack = QStackedWidget()
        cdse_page = QWidget()
        cdse_form = QFormLayout(cdse_page)
        self.cdse_username = QLineEdit()
        self.cdse_username.setPlaceholderText("CDSE username")
        cdse_form.addRow("Username:", self.cdse_username)
        self.cdse_password = QLineEdit()
        self.cdse_password.setEchoMode(QLineEdit.Password)
        self.cdse_password.setPlaceholderText("CDSE password")
        cdse_form.addRow("Password:", self.cdse_password)
        self.local_cred_stack.addWidget(cdse_page)

        asf_page = QWidget()
        asf_form = QFormLayout(asf_page)
        self.earthdata_username = QLineEdit()
        self.earthdata_username.setPlaceholderText("NASA Earthdata username")
        asf_form.addRow("Earthdata username:", self.earthdata_username)
        self.earthdata_password = QLineEdit()
        self.earthdata_password.setEchoMode(QLineEdit.Password)
        self.earthdata_password.setPlaceholderText("Earthdata password")
        asf_form.addRow("Earthdata password:", self.earthdata_password)
        asf_hint = QLabel(
            "<a href=\"https://urs.earthdata.nasa.gov/\">Earthdata Login</a> "
            "(free). Bulk download: "
            "<a href=\"https://bulk-download.asf.alaska.edu/help\">ASF bulk download</a>."
        )
        asf_hint.setOpenExternalLinks(True)
        asf_hint.setWordWrap(True)
        asf_form.addRow(asf_hint)
        self.local_cred_stack.addWidget(asf_page)

        pc_page = QWidget()
        pc_form = QFormLayout(pc_page)
        self.pc_subscription_key = QLineEdit()
        self.pc_subscription_key.setPlaceholderText("Optional (higher rate limits)")
        self.pc_subscription_key.setEchoMode(QLineEdit.Password)
        pc_form.addRow("Subscription key:", self.pc_subscription_key)
        pc_hint = QLabel(
            "<a href=\"https://planetarycomputer.microsoft.com/dataset/sentinel-1-grd\">"
            "Sentinel-1 GRD on Planetary Computer</a> — key optional for reads."
        )
        pc_hint.setOpenExternalLinks(True)
        pc_hint.setWordWrap(True)
        pc_form.addRow(pc_hint)
        self.local_cred_stack.addWidget(pc_page)

        local_outer.addWidget(self.local_cred_stack)
        self.local_source_combo.currentIndexChanged.connect(
            self._on_local_source_changed
        )
        self.cred_stacked.addWidget(local_page)

        cred_layout.addWidget(self.cred_stacked)
        layout.addWidget(cred_group)

        # ── AOI ──────────────────────────────────────────────────────────────
        aoi_group = QGroupBox("Area of interest")
        aoi_outer = QVBoxLayout(aoi_group)

        # --- Run Queue sub-section ---
        self.draw_aoi_btn = QPushButton(QIcon(":/pwtt/icon_draw_aoi.svg"), "Draw rectangle on map")
        self.draw_aoi_btn.clicked.connect(self._activate_aoi_tool)
        aoi_outer.addWidget(self.draw_aoi_btn)

        self.queue_label = QLabel("Queue  (0 selected)")
        self.queue_label.setStyleSheet("font-weight: bold;")
        aoi_outer.addWidget(self.queue_label)

        self._queue_warning_label = QLabel(
            "⚠ Large batch — check API quota before running."
        )
        self._queue_warning_label.setStyleSheet("color: #b85c00; font-size: 0.9em;")
        self._queue_warning_label.setVisible(False)
        aoi_outer.addWidget(self._queue_warning_label)

        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(80)
        self.queue_list.setAlternatingRowColors(True)
        aoi_outer.addWidget(self.queue_list)

        queue_btn_row = QHBoxLayout()
        self.clear_queue_btn = QPushButton("Clear queue")
        self.clear_queue_btn.clicked.connect(self._clear_queue)
        self.clear_queue_btn.setEnabled(False)
        queue_btn_row.addWidget(self.clear_queue_btn)
        self.toggle_all_map_btn = QPushButton("Hide all on map")
        self.toggle_all_map_btn.clicked.connect(self._toggle_all_map_visibility)
        self.toggle_all_map_btn.setEnabled(False)
        queue_btn_row.addWidget(self.toggle_all_map_btn)
        aoi_outer.addLayout(queue_btn_row)

        # --- Saved AOI Library sub-section (collapsible) ---
        self._library_toggle_btn = QPushButton("▶  Saved AOI Library  (0 saved)")
        self._library_toggle_btn.setCheckable(True)
        self._library_toggle_btn.setChecked(False)
        self._library_toggle_btn.setFlat(True)
        aoi_outer.addWidget(self._library_toggle_btn)

        self._library_widget = QWidget()
        lib_layout = QVBoxLayout(self._library_widget)
        lib_layout.setContentsMargins(0, 0, 0, 0)
        lib_layout.setSpacing(4)

        self.library_tree = _LibraryTree()
        lib_layout.addWidget(self.library_tree)

        lib_btn_row1 = QHBoxLayout()
        self.lib_new_project_btn = QPushButton("New project…")
        self.lib_new_project_btn.clicked.connect(self._lib_new_project)
        lib_btn_row1.addWidget(self.lib_new_project_btn)
        self.lib_load_btn = QPushButton("Load into queue")
        self.lib_load_btn.clicked.connect(self._lib_load_selected)
        self.lib_load_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_load_btn)
        self.lib_rename_btn = QPushButton("Rename")
        self.lib_rename_btn.clicked.connect(self._lib_rename_selected)
        self.lib_rename_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_rename_btn)
        self.lib_delete_btn = QPushButton("Delete")
        self.lib_delete_btn.clicked.connect(self._lib_delete_selected)
        self.lib_delete_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_delete_btn)
        lib_layout.addLayout(lib_btn_row1)

        lib_btn_row2 = QHBoxLayout()
        self.lib_export_btn = QPushButton("Export…")
        self.lib_export_btn.clicked.connect(self._lib_export)
        lib_btn_row2.addWidget(self.lib_export_btn)
        self.lib_import_btn = QPushButton("Import…")
        self.lib_import_btn.clicked.connect(self._lib_import)
        lib_btn_row2.addWidget(self.lib_import_btn)
        lib_layout.addLayout(lib_btn_row2)

        self._library_widget.setVisible(False)
        aoi_outer.addWidget(self._library_widget)

        self._library_toggle_btn.toggled.connect(self._on_library_toggled)
        self.library_tree.itemSelectionChanged.connect(self._on_library_selection_changed)
        self.library_tree.aoi_moved.connect(self._lib_move_aoi)
        self.library_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.library_tree.customContextMenuRequested.connect(self._show_library_context_menu)

        layout.addWidget(aoi_group)

        # Parameters
        params_group = QGroupBox("Parameters")
        params_layout = QFormLayout(params_group)
        params_layout.setVerticalSpacing(2)

        self.war_start = QDateEdit()
        self.war_start.setCalendarPopup(True)
        self.war_start.setDate(QDate(2023, 10, 7))
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

        self.include_footprints = QCheckBox("Include building footprints")
        self.include_footprints.setChecked(False)
        params_layout.addRow(self.include_footprints)

        # Sub-options: which OSM snapshot(s) to fetch
        self._fp_options_widget = QWidget()
        _fp_opts_layout = QVBoxLayout(self._fp_options_widget)
        _fp_opts_layout.setContentsMargins(20, 2, 0, 2)
        _fp_opts_layout.setSpacing(2)
        self.fp_current_osm = QCheckBox("Current OSM buildings")
        self.fp_current_osm.setChecked(True)
        self.fp_historical_war_start = QCheckBox("Historical OSM at war start date")
        self.fp_historical_war_start.setChecked(False)
        self.fp_historical_inference_start = QCheckBox("Historical OSM at inference start date")
        self.fp_historical_inference_start.setChecked(False)
        _fp_opts_layout.addWidget(self.fp_current_osm)
        _fp_opts_layout.addWidget(self.fp_historical_war_start)
        _fp_opts_layout.addWidget(self.fp_historical_inference_start)
        _fp_opts_layout.addWidget(self._hint(
            "Each selected source is added as a separate layer. "
            "Historical snapshots use Overpass API with a date filter."
        ))
        self._fp_options_widget.setVisible(False)
        params_layout.addRow(self._fp_options_widget)
        self.include_footprints.toggled.connect(self._fp_options_widget.setVisible)
        params_layout.addRow(self._hint(
            "Overlay OpenStreetMap building footprints on the result to assess per-building damage."
        ))

        self.damage_mask_group = QGroupBox("Damage mask (T-statistic cutoff)")
        dm_form = QFormLayout(self.damage_mask_group)
        self.damage_threshold_spin = QDoubleSpinBox()
        self.damage_threshold_spin.setRange(0.5, 20.0)
        self.damage_threshold_spin.setDecimals(2)
        self.damage_threshold_spin.setSingleStep(0.1)
        self.damage_threshold_spin.setValue(3.3)
        self.damage_threshold_spin.setToolTip(
            "Band 2 is 1 where exported T-statistic exceeds this value. "
            "Higher \u2192 stricter (fewer pixels). Not a probability; backends build T differently."
        )
        dm_form.addRow("T-statistic >", self.damage_threshold_spin)
        dm_form.addRow(self._hint(
            "Test-statistic threshold, not a damage probability. "
            "Rough guide: T>2 sensitive; 3.3 default; >4 fewer false positives; >5 strongest only. "
            "(github.com/oballinger/PWTT#recommended-thresholds)."
        ))
        # ── GEE: Detection method ──────────────────────────────────────────
        self.gee_method_group = QGroupBox("Detection method (GEE only)")
        gm_layout = QVBoxLayout(self.gee_method_group)
        self.gee_method_combo = QComboBox()
        _method_items = [
            ("stouffer",     "Stouffer weighted Z  (default — recommended)"),
            ("max",          "Max t-value across orbits"),
            ("ztest",        "Z-test: latest image vs baseline"),
            ("hotelling",    "Hotelling T²  (joint VV+VH)"),
            ("mahalanobis",  "Mahalanobis effect size  (n-invariant)"),
        ]
        for value, label in _method_items:
            self.gee_method_combo.addItem(label, value)
        gm_layout.addWidget(self.gee_method_combo)
        self._gee_method_hint = self._hint("")
        gm_layout.addWidget(self._gee_method_hint)
        self.gee_method_combo.currentIndexChanged.connect(self._on_gee_method_changed)
        self._on_gee_method_changed(0)  # populate hint for default selection
        params_layout.addRow(self.gee_method_group)

        # ── GEE: Advanced options (collapsed) ─────────────────────────────
        self.gee_advanced_group = QGroupBox("Advanced GEE options")
        ga_outer = QVBoxLayout(self.gee_advanced_group)

        self._gee_advanced_toggle_btn = QPushButton("▶  Advanced options")
        self._gee_advanced_toggle_btn.setCheckable(True)
        self._gee_advanced_toggle_btn.setChecked(False)
        self._gee_advanced_toggle_btn.setFlat(True)
        ga_outer.addWidget(self._gee_advanced_toggle_btn)

        self._gee_advanced_widget = QWidget()
        ga_adv = QFormLayout(self._gee_advanced_widget)
        ga_adv.setVerticalSpacing(4)

        self.gee_ttest_type_combo = QComboBox()
        self.gee_ttest_type_combo.addItem("welch  (default — unequal variance)", "welch")
        self.gee_ttest_type_combo.addItem("pooled  (assumes equal variance)", "pooled")
        ga_adv.addRow("T-test type:", self.gee_ttest_type_combo)

        self.gee_smoothing_combo = QComboBox()
        self.gee_smoothing_combo.addItem("default  (focal median + 50/100/150 m kernels)", "default")
        self.gee_smoothing_combo.addItem("focal_only  (focal median only, no convolution)", "focal_only")
        ga_adv.addRow("Smoothing:", self.gee_smoothing_combo)

        self.gee_mask_before_smooth_cb = QCheckBox("Mask urban pixels before focal median")
        self.gee_mask_before_smooth_cb.setChecked(True)
        ga_adv.addRow(self.gee_mask_before_smooth_cb)

        self.gee_lee_mode_combo = QComboBox()
        self.gee_lee_mode_combo.addItem("per_image  (default — filter each scene)", "per_image")
        self.gee_lee_mode_combo.addItem("composite  (filter composites only, ~37% less cost)", "composite")
        ga_adv.addRow("Lee filter mode:", self.gee_lee_mode_combo)

        ga_adv.addRow(self._hint(
            "T-test type: Welch does not assume equal variance (more robust). "
            "Smoothing: 'default' applies multi-scale convolutions after focal median. "
            "Lee mode: 'composite' saves EE compute units on large AOIs."
        ))

        self._gee_advanced_widget.setVisible(False)
        ga_outer.addWidget(self._gee_advanced_widget)
        self._gee_advanced_toggle_btn.toggled.connect(self._on_gee_advanced_toggled)
        params_layout.addRow(self.gee_advanced_group)

        params_layout.addRow(self.damage_mask_group)

        self.gee_preview_group = QGroupBox("Earth Engine preview")
        gp_l = QVBoxLayout(self.gee_preview_group)
        self.gee_map_preview_cb = QCheckBox("Open interactive map in browser (requires geemap)")
        self.gee_map_preview_cb.setToolTip(
            "Exports a short HTML preview and opens your default browser after the EE image "
            "is built, before the GeoTIFF downloads."
        )
        gp_l.addWidget(self.gee_map_preview_cb)
        gp_l.addWidget(self._hint(
            "Install geemap in the same environment as QGIS if preview fails."
        ))
        params_layout.addRow(self.gee_preview_group)

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

    def _local_data_source_id(self):
        """Local GRD source: cdse | asf | pc (from combo user data)."""
        if not hasattr(self, "local_source_combo"):
            return "cdse"
        v = self.local_source_combo.currentData()
        return v if v in ("cdse", "asf", "pc") else "cdse"

    def _on_local_source_changed(self, _index=None):
        """Swap credential fields and refresh deps when Local source changes."""
        if hasattr(self, "local_cred_stack") and hasattr(self, "local_source_combo"):
            self.local_cred_stack.setCurrentIndex(self.local_source_combo.currentIndex())
        # Persist immediately so LocalBackend.check_dependencies() matches the UI before Run.
        if hasattr(self, "local_source_combo"):
            s = QgsSettings()
            s.beginGroup("PWTT")
            s.setValue("local_data_source", self._local_data_source_id())
            s.endGroup()
        if self.backend_combo.currentData() == "local":
            self._on_backend_changed(self.backend_combo.currentIndex())
            self._refresh_cred_storage_indicator()

    _GEE_METHOD_HINTS = {
        "stouffer": (
            "Stouffer's weighted Z-score: combines orbits by √df. "
            "Statistically principled default."
        ),
        "max": (
            "Takes the maximum t-value across orbits and Bonferroni-corrects. "
            "Original PWTT behavior."
        ),
        "ztest": (
            "Compares the single most-recent post-war image to the pre-war baseline. "
            "Useful for near-real-time monitoring."
        ),
        "hotelling": (
            "Hotelling T²: joint multivariate test on VV and VH simultaneously. "
            "More powerful when both polarizations change together."
        ),
        "mahalanobis": (
            "Mahalanobis effect size: n-invariant, useful for comparing areas with "
            "different image counts."
        ),
    }

    def _on_gee_method_changed(self, _index):
        method = self.gee_method_combo.currentData()
        self._gee_method_hint.setText(self._GEE_METHOD_HINTS.get(method, ""))

    def _on_gee_advanced_toggled(self, checked: bool):
        self._gee_advanced_widget.setVisible(checked)
        self._gee_advanced_toggle_btn.setText(
            "▼  Advanced options" if checked else "▶  Advanced options"
        )

    def _on_backend_changed(self, index):
        from ..core import deps
        backend_id = self.backend_combo.currentData()
        self.cred_stacked.setCurrentIndex([b[0] for b in BACKENDS].index(backend_id))

        local_src = self._local_data_source_id() if backend_id == "local" else None
        missing_imports, pip_names = deps.backend_missing(backend_id, local_src)
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

        self.damage_mask_group.setVisible(True)
        self.gee_preview_group.setVisible(backend_id == "gee")
        self.gee_method_group.setVisible(backend_id == "gee")
        self.gee_advanced_group.setVisible(backend_id == "gee")
        self._refresh_cred_storage_indicator()

    @staticmethod
    def _saved_credentials_snapshot():
        """Non-secret flags for what is persisted under PWTT/ in QgsSettings."""
        s = QgsSettings()
        s.beginGroup("PWTT")
        cid = (s.value("openeo_client_id", "") or "").strip()
        csec = (s.value("openeo_client_secret", "") or "").strip()
        gee = (s.value("gee_project", "") or "").strip()
        gee_cid = (s.value("gee_client_id", "") or "").strip()
        gee_csec = (s.value("gee_client_secret", "") or "").strip()
        cu = (s.value("cdse_username", "") or "").strip()
        cp = (s.value("cdse_password", "") or "").strip()
        eu = (s.value("earthdata_username", "") or "").strip()
        ep = (s.value("earthdata_password", "") or "").strip()
        pc_k = (s.value("pc_subscription_key", "") or "").strip()
        s.endGroup()
        return {
            "openeo_id": bool(cid),
            "openeo_secret": bool(csec),
            "gee_project": bool(gee),
            "gee_client_id": bool(gee_cid),
            "gee_client_secret": bool(gee_csec),

            "cdse_user": bool(cu),
            "cdse_pass": bool(cp),
            "earthdata_user": bool(eu),
            "earthdata_pass": bool(ep),
            "pc_key": bool(pc_k),
        }

    def _refresh_cred_storage_indicator(self):
        """Show whether credentials for the current backend exist in QGIS settings."""
        if not hasattr(self, "cred_storage_label"):
            return
        snap = self._saved_credentials_snapshot()
        bid = self.backend_combo.currentData()
        if bid == "openeo":
            if snap["openeo_id"] and snap["openeo_secret"]:
                self.cred_storage_label.setText(
                    "Stored: client ID & secret in QGIS settings (client-credentials flow)."
                )
                self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
            elif snap["openeo_id"] or snap["openeo_secret"]:
                self.cred_storage_label.setText(
                    "Stored: incomplete client credentials in settings (need both ID and secret)."
                )
                self.cred_storage_label.setStyleSheet("color: #e65100; font-size: 0.9em;")
            else:
                self.cred_storage_label.setText(
                    "Not stored: enter Client ID and Client Secret above."
                )
                self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
        elif bid == "gee":
            has_oauth = snap["gee_client_id"] and snap["gee_client_secret"]
            if has_oauth:
                self.cred_storage_label.setText(
                    "Stored: OAuth 2.0 client ID & secret in QGIS settings."
                )
                self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
            elif snap["gee_project"]:
                self.cred_storage_label.setText(
                    "Stored: project name only — add Client ID & Secret."
                )
                self.cred_storage_label.setStyleSheet("color: #e65100; font-size: 0.9em;")
            else:
                self.cred_storage_label.setText(
                    "Not stored: enter credentials above. "
                    "Create at console.cloud.google.com/apis/credentials"
                )
                self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
        elif bid == "local":
            src = self._local_data_source_id()
            if src == "cdse":
                if snap["cdse_user"] and snap["cdse_pass"]:
                    self.cred_storage_label.setText(
                        "Stored: CDSE username & password in QGIS settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
                elif snap["cdse_user"] or snap["cdse_pass"]:
                    self.cred_storage_label.setText(
                        "Partial: incomplete CDSE credentials in settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: #e65100; font-size: 0.9em;")
                else:
                    self.cred_storage_label.setText(
                        "Not stored: no CDSE credentials in QGIS settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
            elif src == "asf":
                if snap["earthdata_user"] and snap["earthdata_pass"]:
                    self.cred_storage_label.setText(
                        "Stored: Earthdata username & password in QGIS settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
                elif snap["earthdata_user"] or snap["earthdata_pass"]:
                    self.cred_storage_label.setText(
                        "Partial: incomplete Earthdata credentials in settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: #e65100; font-size: 0.9em;")
                else:
                    self.cred_storage_label.setText(
                        "Not stored: no Earthdata credentials in QGIS settings."
                    )
                    self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
            else:
                if snap["pc_key"]:
                    self.cred_storage_label.setText(
                        "Stored: Planetary Computer subscription key in settings (optional)."
                    )
                    self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
                else:
                    self.cred_storage_label.setText(
                        "No PC key stored (optional). Anonymous access often works."
                    )
                    self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
        else:
            self.cred_storage_label.clear()
            self.cred_storage_label.setStyleSheet("font-size: 0.9em;")

    def _install_backend_deps(self):
        """Install missing backend packages via the deps module."""
        from ..core import deps
        names = getattr(self, "_pending_pip_install", [])
        if not names:
            return
        if deps.install_with_dialog(names, parent=self):
            self._on_backend_changed(self.backend_combo.currentIndex())
            # Re-check: if still missing, show diagnostic
            backend_id = self.backend_combo.currentData()
            local_src = self._local_data_source_id() if backend_id == "local" else None
            still_missing, _ = deps.backend_missing(backend_id, local_src)
            if still_missing:
                detail = deps.diagnose_import_failures(still_missing)
                QMessageBox.information(
                    self, "PWTT",
                    f"Packages were installed but {', '.join(still_missing)} "
                    f"still cannot be imported.\n\n"
                    f"Technical detail:\n{detail}\n\n"
                    f"Retry: open the PWTT **Damage Detection** panel "
                    f"(toolbar or Plugins menu), then press **Install Dependencies** "
                    f"under Processing backend (orange “Missing” state).\n\n"
                    f"Or delete the folder for a clean reinstall:\n"
                    f"{deps.plugin_deps_dir()}",
                )

    def _get_credentials(self, backend_id):
        if backend_id == "openeo":
            return {
                "client_id": self.openeo_client_id.text().strip() or None,
                "client_secret": self.openeo_client_secret.text().strip() or None,
                "verify_ssl": self.openeo_verify_ssl.isChecked(),
            }
        if backend_id == "gee":
            return {
                "project": self.gee_project.text().strip(),
                "client_id": self.gee_client_id.text().strip() or None,
                "client_secret": self.gee_client_secret.text().strip() or None,
            }
        if backend_id == "local":
            return {
                "source": self._local_data_source_id(),
                "username": self.cdse_username.text().strip(),
                "password": self.cdse_password.text(),
                "earthdata_username": self.earthdata_username.text().strip(),
                "earthdata_password": self.earthdata_password.text(),
                "pc_subscription_key": self.pc_subscription_key.text().strip(),
            }
        return {}

    # ── AOI ───────────────────────────────────────────────────────────────────

    def _activate_aoi_tool(self):
        canvas = self.iface.mapCanvas()
        if self.map_tool is None:
            from .aoi_tool import PWTTMapToolExtent
            self.map_tool = PWTTMapToolExtent(canvas, self._on_aoi_drawn)
        self._previous_map_tool = canvas.mapTool()
        canvas.setMapTool(self.map_tool)
        self.iface.messageBar().pushMessage(
            "PWTT", "Draw a rectangle on the map to add it to the AOI queue.",
            level=Qgis.Info, duration=5,
        )

    def _queue_colour(self, index: int) -> tuple:
        return self._AOI_COLOURS[index % len(self._AOI_COLOURS)]

    def _add_to_queue(self, aoi_entry: dict):
        """Add an AOI dict to the queue (no-op if same id already present)."""
        if any(a["id"] == aoi_entry["id"] for a in self._queue):
            return
        self._queue.append(aoi_entry)
        self._rebuild_queue_list()
        self._draw_rubber_band_for(aoi_entry)
        self._update_queue_buttons()

    def _rebuild_queue_list(self):
        """Sync the QListWidget with self._queue."""
        try:
            self.queue_list.itemChanged.disconnect(self._on_queue_item_changed)
        except Exception:
            pass
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for aoi in self._queue:
            tag = aoi.get("tag", "drawn")
            label = f"{aoi['name']}  [{tag}]"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if aoi.get("checked", True) else Qt.Unchecked)
            item.setData(Qt.UserRole, aoi["id"])
            self.queue_list.addItem(item)

            # Add Save / Remove buttons via a widget in the item
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 4, 0)
            row_layout.addStretch()

            if tag == "drawn":
                save_btn = QPushButton("Save")
                aoi_id = aoi["id"]
                save_btn.clicked.connect(lambda _checked, aid=aoi_id: self._queue_save_aoi(aid))
                row_layout.addWidget(save_btn)

            remove_btn = QPushButton("Remove")
            aoi_id = aoi["id"]
            remove_btn.clicked.connect(lambda _checked, aid=aoi_id: self._queue_remove_aoi(aid))
            row_layout.addWidget(remove_btn)

            self.queue_list.setItemWidget(item, row_widget)

        self.queue_list.blockSignals(False)
        self.queue_list.itemChanged.connect(self._on_queue_item_changed)
        self._update_queue_label()

    def _on_queue_item_changed(self, item):
        aoi_id = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        for aoi in self._queue:
            if aoi["id"] == aoi_id:
                aoi["checked"] = checked
                break
        self._update_queue_label()

    def _update_queue_label(self):
        selected = sum(1 for a in self._queue if a.get("checked", True))
        self.queue_label.setText(f"Queue  ({selected} selected)")
        self._queue_warning_label.setVisible(selected >= 4)

    def _update_queue_buttons(self):
        has_items = bool(self._queue)
        self.clear_queue_btn.setEnabled(has_items)
        self.toggle_all_map_btn.setEnabled(has_items)

    def _draw_rubber_band_for(self, aoi_entry: dict):
        """Draw a rubber band for a single AOI entry."""
        aoi_id = aoi_entry["id"]
        self._remove_rubber_band(aoi_id)
        bbox = aoi_entry.get("bbox")
        if not bbox or len(bbox) < 4:
            return
        west, south, east, north = bbox
        rect = QgsRectangle(west, south, east, north)
        geom = QgsGeometry.fromRect(rect)
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != src_crs:
            transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
            geom.transform(transform)
        try:
            idx = next(i for i, a in enumerate(self._queue) if a["id"] == aoi_entry["id"])
        except StopIteration:
            idx = len(self._rubber_bands)
        r, g, b = self._queue_colour(idx)
        rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        rb.setColor(QColor(r, g, b, 50))
        rb.setStrokeColor(QColor(r, g, b, 220))
        rb.setWidth(2)
        rb.setToGeometry(geom, None)
        self._rubber_bands[aoi_id] = rb

    def _remove_rubber_band(self, aoi_id: str):
        rb = self._rubber_bands.pop(aoi_id, None)
        if rb is not None:
            rb.reset(QgsWkbTypes.PolygonGeometry)

    def _clear_all_rubber_bands(self):
        for rb in self._rubber_bands.values():
            rb.reset(QgsWkbTypes.PolygonGeometry)
        self._rubber_bands.clear()

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

        try:
            self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        except Exception:
            pass

        backend_id = self.backend_combo.currentData()
        bbox = [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]

        from ..core import aoi_splitter
        if aoi_splitter.needs_split(bbox, backend_id):
            dlg = _AoiSplitDialog(self, bbox, backend_id, self.iface.mapCanvas())
            action = dlg.exec()
            if action == "cancel":
                return
            elif action == "single":
                self._add_drawn_aoi_to_queue(wkt, rect)
            else:  # "tiles"
                for i, tile_bbox in enumerate(dlg.confirmed_tiles(), start=1):
                    self._add_tile_aoi_to_queue(tile_bbox, i)
        else:
            self._add_drawn_aoi_to_queue(wkt, rect)

    def _add_drawn_aoi_to_queue(self, wkt: str, rect) -> None:
        """Create a tmp_ AOI entry from a drawn rect and add it to the queue."""
        import uuid as _uuid
        aoi_id = "tmp_" + _uuid.uuid4().hex[:8]
        bbox = [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]
        name = f"Drawn AOI {len(self._queue) + 1}"
        aoi_entry = {
            "id": aoi_id,
            "name": name,
            "wkt": wkt,
            "bbox": bbox,
            "tag": "drawn",
            "checked": True,
        }
        self._add_to_queue(aoi_entry)

    def _add_tile_aoi_to_queue(self, tile_bbox: list, tile_index: int) -> None:
        """Create a tmp_ AOI entry from a tile bbox and add it to the queue."""
        import uuid as _uuid
        west, south, east, north = tile_bbox
        rect = QgsRectangle(west, south, east, north)
        geom = QgsGeometry.fromRect(rect)
        wkt = geom.asWkt()
        aoi_id = "tmp_" + _uuid.uuid4().hex[:8]
        aoi_entry = {
            "id": aoi_id,
            "name": f"Tile {tile_index}",
            "wkt": wkt,
            "bbox": list(tile_bbox),
            "tag": "drawn",
            "checked": True,
        }
        self._add_to_queue(aoi_entry)

    def _queue_save_aoi(self, aoi_id: str):
        """Prompt for name and project, save to library, update queue row tag."""
        from ..core import aoi_store
        aoi_entry = next((a for a in self._queue if a["id"] == aoi_id), None)
        if aoi_entry is None:
            return
        name, ok = QInputDialog.getText(
            self, "Save AOI", "AOI name:", text=aoi_entry["name"]
        )
        if not ok or not name.strip():
            return

        # Project selection
        projects = aoi_store.load_projects()
        if not projects:
            project_id = ""  # save_aoi will auto-create Default
        elif len(projects) == 1:
            project_id = projects[0]["id"]
        else:
            proj_names = [p["name"] for p in projects]
            proj_name, ok2 = QInputDialog.getItem(
                self, "Save AOI", "Save into project:", proj_names, 0, False
            )
            if not ok2:
                return
            project_id = next(p["id"] for p in projects if p["name"] == proj_name)

        new_aoi = aoi_store.make_aoi(
            name.strip(), aoi_entry["wkt"], aoi_entry["bbox"], project_id=project_id
        )
        aoi_store.save_aoi(new_aoi)
        # Move rubber band to new id before updating entry
        rb = self._rubber_bands.pop(aoi_id, None)
        if rb is not None:
            self._rubber_bands[new_aoi["id"]] = rb
        # Update queue entry in place
        aoi_entry.update({"id": new_aoi["id"], "name": new_aoi["name"], "tag": "saved"})
        self._rebuild_queue_list()
        self._refresh_library_tree()
        self.iface.messageBar().pushMessage(
            "PWTT", f'AOI "{name.strip()}" saved to library.', level=Qgis.Success, duration=4,
        )

    def _queue_remove_aoi(self, aoi_id: str):
        self._queue = [a for a in self._queue if a["id"] != aoi_id]
        self._remove_rubber_band(aoi_id)
        self._rebuild_queue_list()
        self._update_queue_buttons()

    def _clear_queue(self):
        self._queue.clear()
        self._clear_all_rubber_bands()
        self._rebuild_queue_list()
        self._update_queue_buttons()

    def _toggle_all_map_visibility(self):
        if not self._rubber_bands:
            return
        first = next(iter(self._rubber_bands.values()))
        visible = first.isVisible()
        for rb in self._rubber_bands.values():
            rb.setVisible(not visible)
        self.toggle_all_map_btn.setText(
            "Show all on map" if visible else "Hide all on map"
        )

    # ── Library ───────────────────────────────────────────────────────────────

    def _refresh_library_tree(self):
        """Rebuild the library QTreeWidget from aoi_store."""
        from ..core import aoi_store
        self.library_tree.blockSignals(True)
        self.library_tree.clear()
        projects = aoi_store.load_projects()
        aois_by_project: dict = {p["id"]: [] for p in projects}
        for aoi in aoi_store.load_aois():
            pid = aoi.get("project_id", "")
            if pid in aois_by_project:
                aois_by_project[pid].append(aoi)
            # else: orphan AOI — the store's _read_raw() repairs orphans on load,
            # so this path is only reachable if the store is bypassed externally.
        total = 0
        for proj in projects:
            proj_aois = sorted(
                aois_by_project[proj["id"]],
                key=lambda a: a.get("created_at", ""),
                reverse=True,
            )
            proj_item = QTreeWidgetItem(self.library_tree)
            proj_item.setText(0, f"{proj['name']}  ({len(proj_aois)})")
            font = proj_item.font(0)
            font.setBold(True)
            proj_item.setFont(0, font)
            proj_item.setData(0, Qt.UserRole, {"type": "project", "id": proj["id"]})
            proj_item.setExpanded(True)
            for aoi in proj_aois:
                date_str = format_iso_date_display(aoi.get("created_at", "") or "")
                aoi_item = QTreeWidgetItem(proj_item)
                aoi_item.setText(0, f"{aoi['name']}  {date_str}")
                aoi_item.setData(0, Qt.UserRole, {"type": "aoi", "id": aoi["id"]})
            total += len(proj_aois)
        self.library_tree.blockSignals(False)
        checked = self._library_toggle_btn.isChecked()
        self._library_toggle_btn.setText(
            f"{'▼' if checked else '▶'}  Saved AOI Library  ({total} saved)"
        )
        self._on_library_selection_changed()

    def _on_library_toggled(self, checked: bool):
        self._library_widget.setVisible(checked)
        self._refresh_library_tree()

    def _on_library_selection_changed(self):
        items = self.library_tree.selectedItems()
        types = {item.data(0, Qt.UserRole).get("type")
                 for item in items if item.data(0, Qt.UserRole)}
        has_sel = bool(items)
        single_type = len(types) == 1
        is_mixed = has_sel and not single_type
        self.lib_load_btn.setEnabled(has_sel)
        self.lib_rename_btn.setEnabled(has_sel and single_type and len(items) == 1)
        is_aoi = types == {"aoi"}
        is_project = types == {"project"}
        self.lib_delete_btn.setEnabled(
            (is_aoi and has_sel) or (is_project and len(items) == 1)
        )
        self.lib_export_btn.setEnabled(not is_mixed)

    def _lib_load_selected(self):
        from ..core import aoi_store
        all_aois = {a["id"]: a for a in aoi_store.load_aois()}
        aoi_ids_to_load: list = []
        for item in self.library_tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            if data["type"] == "aoi":
                aoi_ids_to_load.append(data["id"])
            elif data["type"] == "project":
                # Load all AOIs in this project
                for i in range(item.childCount()):
                    child_data = item.child(i).data(0, Qt.UserRole)
                    if child_data and child_data["type"] == "aoi":
                        aoi_ids_to_load.append(child_data["id"])
        for aoi_id in aoi_ids_to_load:
            aoi = all_aois.get(aoi_id)
            if aoi is None:
                continue
            entry = {
                "id": aoi["id"],
                "name": aoi["name"],
                "wkt": aoi["wkt"],
                "bbox": aoi["bbox"],
                "tag": "saved",
                "checked": True,
            }
            self._add_to_queue(entry)

    def _lib_rename_selected(self):
        from ..core import aoi_store
        items = self.library_tree.selectedItems()
        if not items:
            return
        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if data["type"] == "aoi":
            aoi = next((a for a in aoi_store.load_aois() if a["id"] == data["id"]), None)
            if aoi is None:
                return
            name, ok = QInputDialog.getText(self, "Rename AOI", "New name:", text=aoi["name"])
            if not ok or not name.strip():
                return
            aoi["name"] = name.strip()
            aoi_store.save_aoi(aoi)
            for q_aoi in self._queue:
                if q_aoi["id"] == data["id"]:
                    q_aoi["name"] = name.strip()
            self._rebuild_queue_list()

        elif data["type"] == "project":
            proj = next((p for p in aoi_store.load_projects() if p["id"] == data["id"]), None)
            if proj is None:
                return
            name, ok = QInputDialog.getText(self, "Rename Project", "New name:", text=proj["name"])
            if not ok or not name.strip():
                return
            proj["name"] = name.strip()
            try:
                aoi_store.save_project(proj)
            except ValueError as e:
                QMessageBox.warning(self, "PWTT", str(e))
                return

        self._refresh_library_tree()

    def _lib_delete_selected(self):
        from ..core import aoi_store
        items = self.library_tree.selectedItems()
        if not items:
            return
        types = {item.data(0, Qt.UserRole).get("type")
                 for item in items if item.data(0, Qt.UserRole)}
        if len(types) != 1:
            return

        if "aoi" in types:
            names = []
            for item in items:
                d = item.data(0, Qt.UserRole)
                if d and d["type"] == "aoi":
                    names.append(item.text(0).split("  ")[0])
            confirm = QMessageBox.question(
                self, "PWTT",
                f"Delete {len(items)} saved AOI(s)?\n" + "\n".join(f"  \u2022 {n}" for n in names),
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            for item in items:
                d = item.data(0, Qt.UserRole)
                if d and d["type"] == "aoi":
                    aoi_store.delete_aoi(d["id"])

        elif "project" in types:
            if len(items) != 1:
                return
            item = items[0]
            data = item.data(0, Qt.UserRole)
            proj = next((p for p in aoi_store.load_projects() if p["id"] == data["id"]), None)
            if proj is None:
                return
            aoi_count = item.childCount()
            confirm = QMessageBox.question(
                self, "PWTT",
                f"Delete project '{proj['name']}' and its {aoi_count} AOI(s)?\nThis cannot be undone.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            try:
                aoi_store.delete_project(data["id"], cascade=True)
            except ValueError as e:
                QMessageBox.warning(self, "PWTT", str(e))
                return

        self._refresh_library_tree()

    def _lib_export(self):
        from ..core import aoi_store
        import json as _json
        from datetime import datetime as _dt
        items = self.library_tree.selectedItems()
        types = {item.data(0, Qt.UserRole).get("type")
                 for item in items if item.data(0, Qt.UserRole)}

        if types == {"project"} and len(items) == 1:
            self._lib_export_project(items[0].data(0, Qt.UserRole)["id"])
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export saved AOIs", "", "JSON files (*.json)"
        )
        if not path:
            return

        try:
            if types == {"aoi"}:
                selected_ids = {item.data(0, Qt.UserRole)["id"] for item in items}
                aois = [a for a in aoi_store.load_aois() if a["id"] in selected_ids]
                proj_ids_needed = {a.get("project_id") for a in aois if a.get("project_id")}
                projects = [p for p in aoi_store.load_projects() if p["id"] in proj_ids_needed]
                payload = {
                    "format": aoi_store.AOI_EXPORT_FORMAT,
                    "version": aoi_store.AOI_EXPORT_VERSION,
                    "exported_at": _dt.now().isoformat(timespec="seconds"),
                    "projects": projects,
                    "aois": aois,
                }
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(payload, f, indent=2, ensure_ascii=False)
                count = len(aois)
            else:
                count = aoi_store.export_aois_to_file(path)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Export failed: {e}")
            return

        self.iface.messageBar().pushMessage(
            "PWTT", f"Exported {count} AOI(s) to {path}.", level=Qgis.Success, duration=5,
        )

    def _lib_export_project(self, project_id: str):
        from ..core import aoi_store
        path, _ = QFileDialog.getSaveFileName(
            self, "Export project", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            count = aoi_store.export_project_to_file(project_id, path)
        except ValueError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        self.iface.messageBar().pushMessage(
            "PWTT", f"Exported {count} AOI(s) to {path}.", level=Qgis.Success, duration=5,
        )

    def _lib_import(self):
        from ..core import aoi_store
        import json as _json
        path, _ = QFileDialog.getOpenFileName(
            self, "Import AOIs", "", "JSON files (*.json)"
        )
        if not path:
            return

        # Peek at the file to see if it's a single-project export
        target_project_id = None
        try:
            with open(path, encoding="utf-8") as f:
                peek = _json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Could not read file: {e}")
            return

        if isinstance(peek, dict) and "project" in peek and isinstance(peek["project"], dict):
            proj_name = peek["project"].get("name", "")
            projects = aoi_store.load_projects()
            choices = [f"Create new project '{proj_name}'"] + [p["name"] for p in projects]
            choice, ok = QInputDialog.getItem(
                self,
                "Import AOIs",
                f"The file contains project '{proj_name}'.\nImport into:",
                choices,
                0,
                False,
            )
            if not ok:
                return
            if choice != choices[0]:
                # User picked an existing project
                target_project_id = next(p["id"] for p in projects if p["name"] == choice)

        try:
            result = aoi_store.import_aois_from_file(path, target_project_id=target_project_id)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Import failed: {e}")
            return
        self._refresh_library_tree()
        self.iface.messageBar().pushMessage(
            "PWTT",
            f"Imported {result['added']} AOI(s). Skipped invalid: {result['skipped_invalid']}.",
            level=Qgis.Success, duration=5,
        )

    def _lib_new_project(self):
        from ..core import aoi_store
        name, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not name.strip():
            return
        proj = aoi_store.make_project(name.strip())
        try:
            aoi_store.save_project(proj)
        except ValueError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        self._refresh_library_tree()

    def _lib_move_aoi(self, aoi_id: str, target_project_id: str):
        from ..core import aoi_store
        try:
            aoi_store.move_aoi(aoi_id, target_project_id)
        except ValueError as e:
            self.iface.messageBar().pushMessage("PWTT", str(e), level=Qgis.Warning, duration=5)
            return
        self._refresh_library_tree()

    def _show_library_context_menu(self, pos):
        from ..core import aoi_store
        item = self.library_tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        menu = QMenu(self)

        if data["type"] == "aoi":
            menu.addAction("Load into queue", self._lib_load_selected)
            menu.addAction("Rename", self._lib_rename_selected)
            move_menu = menu.addMenu("Move to project")
            current_proj_data = item.parent().data(0, Qt.UserRole) if item.parent() else None
            current_pid = current_proj_data.get("id") if current_proj_data else None
            for proj in aoi_store.load_projects():
                action = move_menu.addAction(proj["name"])
                if proj["id"] == current_pid:
                    action.setEnabled(False)
                else:
                    action.triggered.connect(
                        lambda checked, aid=data["id"], pid=proj["id"]:
                            self._lib_move_aoi(aid, pid)
                    )
            menu.addSeparator()
            menu.addAction("Delete", self._lib_delete_selected)

        elif data["type"] == "project":
            menu.addAction("Rename", self._lib_rename_selected)
            menu.addAction("New project…", self._lib_new_project)
            menu.addSeparator()
            menu.addAction("Delete project and AOIs", self._lib_delete_selected)
            menu.addAction(
                "Export project…",
                lambda: self._lib_export_project(data["id"]),
            )

        menu.exec_(self.library_tree.viewport().mapToGlobal(pos))

    # ── Load job parameters ──────────────────────────────────────────────────

    def load_job_params(self, job):
        """Populate controls from a saved job and show its AOI on the map."""
        # Backend — for local jobs, apply saved GRD catalog (cdse/asf/pc) before switching
        # backend so credential stack, QgsSettings, and deps match the job.
        backend_ids = [b[0] for b in BACKENDS]
        if job["backend_id"] in backend_ids:
            if job["backend_id"] == "local":
                ds = (job.get("data_source") or "cdse").strip().lower()
                lidx = {"cdse": 0, "asf": 1, "pc": 2}.get(ds, 0)
                self.local_source_combo.setCurrentIndex(lidx)
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

        fp_sources = job_footprints_sources(job)
        self.include_footprints.setChecked(bool(fp_sources))
        self.fp_current_osm.setChecked("current_osm" in fp_sources)
        self.fp_historical_war_start.setChecked("historical_war_start" in fp_sources)
        self.fp_historical_inference_start.setChecked("historical_inference_start" in fp_sources)

        self.damage_threshold_spin.setValue(float(job.get("damage_threshold", 3.3)))
        self.gee_map_preview_cb.setChecked(job.get("gee_viz", False))

        _method_val = job.get("gee_method", "stouffer")
        _method_idx = next(
            (i for i in range(self.gee_method_combo.count())
             if self.gee_method_combo.itemData(i) == _method_val),
            0,
        )
        self.gee_method_combo.setCurrentIndex(_method_idx)
        _ttest_val = job.get("gee_ttest_type", "welch")
        self.gee_ttest_type_combo.setCurrentIndex(
            next((i for i in range(self.gee_ttest_type_combo.count())
                  if self.gee_ttest_type_combo.itemData(i) == _ttest_val), 0)
        )
        _smoothing_val = job.get("gee_smoothing", "default")
        self.gee_smoothing_combo.setCurrentIndex(
            next((i for i in range(self.gee_smoothing_combo.count())
                  if self.gee_smoothing_combo.itemData(i) == _smoothing_val), 0)
        )
        self.gee_mask_before_smooth_cb.setChecked(job.get("gee_mask_before_smooth", True))
        _lee_val = job.get("gee_lee_mode", "per_image")
        self.gee_lee_mode_combo.setCurrentIndex(
            next((i for i in range(self.gee_lee_mode_combo.count())
                  if self.gee_lee_mode_combo.itemData(i) == _lee_val), 0)
        )

        # AOI — load into queue and zoom to it
        aoi_wkt = job.get("aoi_wkt")
        if aoi_wkt:
            from ..core.utils import wkt_to_bbox
            import uuid as _uuid
            bbox = wkt_to_bbox(aoi_wkt)
            if bbox:
                west, south, east, north = bbox
                aoi_entry = {
                    "id": "tmp_" + _uuid.uuid4().hex[:8],
                    "name": f"Job {job.get('id', '?')} AOI",
                    "wkt": aoi_wkt,
                    "bbox": list(bbox),
                    "tag": "drawn",
                    "checked": True,
                }
                self._add_to_queue(aoi_entry)

                # Zoom to AOI
                rect = QgsRectangle(west, south, east, north)
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
        save_openeo_credentials_to_settings(
            self.openeo_client_id.text(),
            self.openeo_client_secret.text(),
            self.openeo_verify_ssl.isChecked(),
        )
        self._refresh_cred_storage_indicator()
        od = getattr(self, "openeo_dock", None)
        if od is not None:
            od._conn = None

    def _test_openeo_credentials(self):
        try:
            test_remote_backend_credentials(
                "openeo",
                self._get_credentials("openeo"),
                parent=self,
                controls_dock=self,
            )
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT — openEO", str(e))
            return
        except Exception as e:
            QMessageBox.warning(
                self,
                "PWTT — openEO",
                f"Unexpected error: {e}",
            )
            return
        self._refresh_cred_storage_indicator()
        od = getattr(self, "openeo_dock", None)
        if od is not None:
            od._conn = None
        QMessageBox.information(
            self,
            "PWTT — openEO",
            "Credentials are valid — authenticated to Copernicus openEO.",
        )

    def _test_gee_credentials(self):
        try:
            test_remote_backend_credentials(
                "gee",
                self._get_credentials("gee"),
                parent=self,
                controls_dock=self,
            )
        except RuntimeError as e:
            QMessageBox.warning(self, "PWTT — Earth Engine", str(e))
            return
        except Exception as e:
            QMessageBox.warning(
                self,
                "PWTT — Earth Engine",
                f"Unexpected error: {e}",
            )
            return
        self._refresh_cred_storage_indicator()
        QMessageBox.information(
            self,
            "PWTT — Earth Engine",
            "Credentials are valid — Earth Engine initialized successfully.",
        )

    def _clear_saved_openeo_credentials(self):
        reply = QMessageBox.question(
            self,
            "PWTT",
            "Remove openEO Client ID and Client Secret from QGIS settings?\n\n"
            "The openEO connection in this session will be reset.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if not is_message_box_yes(reply):
            return
        clear_openeo_credentials_from_storage()
        self._sync_openeo_widgets_from_settings()
        od = getattr(self, "openeo_dock", None)
        if od is not None:
            od._conn = None

    def _clear_saved_gee_credentials(self):
        reply = QMessageBox.question(
            self,
            "PWTT",
            "Remove all GEE credentials from QGIS settings and delete the Earth Engine "
            "credentials file (OAuth refresh token) if it exists?\n\n"
            "This covers OAuth client and default browser login flows.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if not is_message_box_yes(reply):
            return
        clear_gee_credentials_from_storage()
        self.gee_project.setText("")
        self.gee_client_id.setText("")
        self.gee_client_secret.setText("")
        self._refresh_cred_storage_indicator()

    def _persist_openeo_credentials(self):
        save_openeo_credentials_to_settings(
            self.openeo_client_id.text(),
            self.openeo_client_secret.text(),
            self.openeo_verify_ssl.isChecked(),
        )
        self._refresh_cred_storage_indicator()
        od = getattr(self, "openeo_dock", None)
        if od is not None:
            od._conn = None

    def _sync_openeo_widgets_from_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        self.openeo_client_id.blockSignals(True)
        self.openeo_client_secret.blockSignals(True)
        self.openeo_client_id.setText(s.value("openeo_client_id", ""))
        self.openeo_client_secret.setText(s.value("openeo_client_secret", ""))
        self.openeo_verify_ssl.blockSignals(True)
        self.openeo_verify_ssl.setChecked(s.value("openeo_verify_ssl", True, type=bool))
        self.openeo_verify_ssl.blockSignals(False)
        self.openeo_client_id.blockSignals(False)
        self.openeo_client_secret.blockSignals(False)
        s.endGroup()
        self._refresh_cred_storage_indicator()

    def _load_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        self.gee_project.setText(s.value("gee_project", ""))
        self.gee_client_id.setText(s.value("gee_client_id", ""))
        self.gee_client_secret.setText(s.value("gee_client_secret", ""))
        s.endGroup()
        self._sync_openeo_widgets_from_settings()
        s = QgsSettings()
        s.beginGroup("PWTT")
        self.cdse_username.setText(s.value("cdse_username", ""))
        self.cdse_password.setText(s.value("cdse_password", ""))
        self.earthdata_username.setText(s.value("earthdata_username", ""))
        self.earthdata_password.setText(s.value("earthdata_password", ""))
        self.pc_subscription_key.setText(s.value("pc_subscription_key", ""))
        lsrc = (s.value("local_data_source", "cdse") or "cdse").strip().lower()
        lidx = {"cdse": 0, "asf": 1, "pc": 2}.get(lsrc, 0)
        self.local_source_combo.blockSignals(True)
        self.local_source_combo.setCurrentIndex(lidx)
        self.local_source_combo.blockSignals(False)
        self.local_cred_stack.setCurrentIndex(lidx)
        out = s.value("output_dir", "")
        if out:
            self.output_dir.setFilePath(out)
        self.damage_threshold_spin.setValue(
            float(s.value("damage_threshold", 3.3))
        )
        self.gee_map_preview_cb.setChecked(
            s.value("gee_map_preview", False, type=bool)
        )
        _method_val = s.value("gee_method", "stouffer")
        _method_idx = next(
            (i for i in range(self.gee_method_combo.count())
             if self.gee_method_combo.itemData(i) == _method_val),
            0,
        )
        self.gee_method_combo.setCurrentIndex(_method_idx)
        _ttest_val = s.value("gee_ttest_type", "welch")
        self.gee_ttest_type_combo.setCurrentIndex(
            next((i for i in range(self.gee_ttest_type_combo.count())
                  if self.gee_ttest_type_combo.itemData(i) == _ttest_val), 0)
        )
        _smoothing_val = s.value("gee_smoothing", "default")
        self.gee_smoothing_combo.setCurrentIndex(
            next((i for i in range(self.gee_smoothing_combo.count())
                  if self.gee_smoothing_combo.itemData(i) == _smoothing_val), 0)
        )
        self.gee_mask_before_smooth_cb.setChecked(
            s.value("gee_mask_before_smooth", True, type=bool)
        )
        _lee_val = s.value("gee_lee_mode", "per_image")
        self.gee_lee_mode_combo.setCurrentIndex(
            next((i for i in range(self.gee_lee_mode_combo.count())
                  if self.gee_lee_mode_combo.itemData(i) == _lee_val), 0)
        )
        s.endGroup()
        self._refresh_cred_storage_indicator()

    def _save_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        s.setValue("gee_project", self.gee_project.text())
        s.setValue("gee_client_id", self.gee_client_id.text())
        s.setValue("gee_client_secret", self.gee_client_secret.text())
        s.setValue("openeo_client_id", self.openeo_client_id.text())
        s.setValue("openeo_client_secret", self.openeo_client_secret.text())
        s.setValue("openeo_verify_ssl", self.openeo_verify_ssl.isChecked())
        s.setValue("cdse_username", self.cdse_username.text())
        s.setValue("cdse_password", self.cdse_password.text())
        s.setValue("earthdata_username", self.earthdata_username.text())
        s.setValue("earthdata_password", self.earthdata_password.text())
        s.setValue("pc_subscription_key", self.pc_subscription_key.text())
        s.setValue("local_data_source", self._local_data_source_id())
        s.setValue("output_dir", self.output_dir.filePath())
        s.setValue("damage_threshold", self.damage_threshold_spin.value())
        s.setValue("gee_map_preview", self.gee_map_preview_cb.isChecked())
        s.setValue("gee_method", self.gee_method_combo.currentData())
        s.setValue("gee_ttest_type", self.gee_ttest_type_combo.currentData())
        s.setValue("gee_smoothing", self.gee_smoothing_combo.currentData())
        s.setValue("gee_mask_before_smooth", self.gee_mask_before_smooth_cb.isChecked())
        s.setValue("gee_lee_mode", self.gee_lee_mode_combo.currentData())
        s.endGroup()
        self._refresh_cred_storage_indicator()

    def closeEvent(self, event):
        self.cleanup_map_canvas()
        super().closeEvent(event)

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run_confirmation_summary_text(self):
        """Human-readable summary of current panel settings (no secrets)."""
        backend_id = self.backend_combo.currentData()
        backend_name = next((n for bid, n in BACKENDS if bid == backend_id), str(backend_id))

        lines = [
            "Start this damage detection run with the following settings?",
            "",
            f"Backend: {backend_name}",
        ]
        if backend_id == "local":
            lines.append(f"GRD data source: {self.local_source_combo.currentText()}")

        n_aois = sum(1 for a in self._queue if a.get("checked", True))
        lines.append(f"AOIs selected: {n_aois}")

        wsd = self.war_start.date()
        insd = self.inference_start.date()
        lines.append(
            f"War start: {format_ymd_display(wsd.year(), wsd.month(), wsd.day())}"
        )
        lines.append(
            f"Inference start: {format_ymd_display(insd.year(), insd.month(), insd.day())}"
        )
        lines.append(f"Pre-war interval: {self.pre_interval.value()} month(s)")
        lines.append(f"Post-war interval: {self.post_interval.value()} month(s)")
        lines.append(
            f"Damage mask cutoff: T-statistic > {self.damage_threshold_spin.value():.2f}"
        )

        if self.include_footprints.isChecked():
            fp_labels = []
            if self.fp_current_osm.isChecked():
                fp_labels.append("current OSM buildings")
            if self.fp_historical_war_start.isChecked():
                fp_labels.append("historical OSM at war start")
            if self.fp_historical_inference_start.isChecked():
                fp_labels.append("historical OSM at inference start")
            if not fp_labels:
                fp_labels.append("current OSM buildings")
            lines.append("Building footprints: " + ", ".join(fp_labels))
        else:
            lines.append("Building footprints: off")

        if backend_id == "gee":
            prev = "on" if self.gee_map_preview_cb.isChecked() else "off"
            lines.append(f"Earth Engine browser preview: {prev}")

        base_dir = (self.output_dir.filePath() or "").strip()
        if not base_dir:
            proj_path = QgsProject.instance().absolutePath()
            base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
        lines.append(f"Output base folder:\n{base_dir}")
        lines.append("(A new job subfolder will be created under this path.)")

        return "\n".join(lines)

    def _run(self):
        from ..core import deps, job_store

        checked_aois = [a for a in self._queue if a.get("checked", True)]
        if not checked_aois:
            QMessageBox.warning(
                self,
                "PWTT",
                "Please add at least one area of interest to the queue: "
                "draw on the map or load from the saved library.",
            )
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
        local_src = self._local_data_source_id() if backend_id == "local" else None

        if backend_id == "local" and not confirm_local_processing_storage(self):
            return

        # ── Batch confirmation dialog ────────────────────────────────────────
        dlg = _BatchConfirmDialog(
            self,
            self._run_confirmation_summary_text(),
            checked_aois,
        )
        confirmed_aois = dlg.exec()
        if not confirmed_aois:
            return

        # ── Check backend dependencies ───────────────────────────────────────
        missing, pip_names = deps.backend_missing(backend_id, local_src)
        if missing:
            reply = QMessageBox.question(
                self, "PWTT",
                f"Missing packages: {', '.join(pip_names)}\n\nInstall now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if is_message_box_yes(reply):
                if not deps.install_with_dialog(pip_names, parent=self):
                    return
                missing, _ = deps.backend_missing(backend_id, local_src)
            if missing:
                QMessageBox.warning(
                    self, "PWTT",
                    f"Cannot run: missing {', '.join(missing)}.",
                )
                return
            self._on_backend_changed(self.backend_combo.currentIndex())

        # ── Check footprint dependencies ─────────────────────────────────────
        if self.include_footprints.isChecked():
            if not ensure_footprint_dependencies(self):
                return

        # ── Authenticate backend ─────────────────────────────────────────────
        credentials = self._get_credentials(backend_id)
        try:
            backend = backend_auth_create_and_auth_backend(
                backend_id,
                parent=self,
                controls_dock=self,
                local_data_source=credentials.get("source") if backend_id == "local" else None,
            )
        except RuntimeError as e:
            if str(e) != "Authentication cancelled.":
                QMessageBox.warning(self, "PWTT", str(e))
            else:
                self.iface.messageBar().pushMessage(
                    "PWTT", "Authentication cancelled.", level=Qgis.Info, duration=5,
                )
            return
        except Exception as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return

        self._save_settings()
        base_dir = self.output_dir.filePath()
        if not base_dir:
            proj_path = QgsProject.instance().absolutePath()
            base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
            self.output_dir.setFilePath(base_dir)

        fp_sources = []
        if self.include_footprints.isChecked():
            if self.fp_current_osm.isChecked():
                fp_sources.append("current_osm")
            if self.fp_historical_war_start.isChecked():
                fp_sources.append("historical_war_start")
            if self.fp_historical_inference_start.isChecked():
                fp_sources.append("historical_inference_start")
            if not fp_sources:
                fp_sources = ["current_osm"]

        # ── Create and launch one job per confirmed AOI ──────────────────────
        launched_ids = []
        for aoi_entry in confirmed_aois:
            job = job_store.create_job(
                backend_id=backend_id,
                aoi_wkt=aoi_entry["wkt"],
                war_start=self.war_start.date().toString("yyyy-MM-dd"),
                inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
                pre_interval=self.pre_interval.value(),
                post_interval=self.post_interval.value(),
                output_dir="",
                include_footprints=bool(fp_sources),
                footprints_sources=fp_sources,
                damage_threshold=self.damage_threshold_spin.value(),
                gee_viz=self.gee_map_preview_cb.isChecked() if backend_id == "gee" else False,
                data_source=self._local_data_source_id() if backend_id == "local" else "cdse",
                gee_method=self.gee_method_combo.currentData() if backend_id == "gee" else "stouffer",
                gee_ttest_type=self.gee_ttest_type_combo.currentData() if backend_id == "gee" else "welch",
                gee_smoothing=self.gee_smoothing_combo.currentData() if backend_id == "gee" else "default",
                gee_mask_before_smooth=self.gee_mask_before_smooth_cb.isChecked() if backend_id == "gee" else True,
                gee_lee_mode=self.gee_lee_mode_combo.currentData() if backend_id == "gee" else "per_image",
            )
            job["output_dir"] = os.path.join(base_dir, job["id"])
            os.makedirs(job["output_dir"], exist_ok=True)
            job_store.save_job(job)
            if self.jobs_dock.launch_job(job, backend):
                launched_ids.append(aoi_entry["id"])

        # ── Post-run cleanup ─────────────────────────────────────────────────
        for aoi_id in launched_ids:
            self._queue = [a for a in self._queue if a["id"] != aoi_id]
            self._remove_rubber_band(aoi_id)
        self._rebuild_queue_list()
        self._update_queue_buttons()
