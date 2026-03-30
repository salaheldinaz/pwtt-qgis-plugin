# -*- coding: utf-8 -*-
"""PWTT dock panels: controls (damage detection)."""

import os

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
)
from qgis.PyQt.QtCore import QDate, Qt
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
    ensure_footprint_dependencies,
    is_message_box_yes,
)
from .dock_common import BACKENDS, dock_title, job_footprints_sources

class PWTTControlsDock(QDockWidget):
    """Dockable controls panel: backend, credentials, AOI, parameters, output, run."""

    def __init__(self, iface, plugin_dir, jobs_dock, parent=None):
        super().__init__(dock_title("PWTT \u2014 Damage Detection", plugin_dir), parent)
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

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_aoi_rubber_band()
        # Re-probe imports (e.g. deps installed via MacOS/python while QGIS stayed open).
        self._on_backend_changed(self.backend_combo.currentIndex())

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self.aoi_wkt:
            self._clear_rubber_band()

    def _sync_aoi_rubber_band(self):
        """Keep canvas overlay aligned with AOI state (handles hide/show without closeEvent)."""
        if self.aoi_wkt and self.aoi_rect is not None:
            self._draw_rubber_band(self.aoi_rect)
        else:
            self._clear_rubber_band()

    def cleanup_map_canvas(self):
        """Remove AOI overlay and extent map tool; call before dock teardown / plugin unload."""
        self._clear_rubber_band()
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

        # AOI
        aoi_group = QGroupBox("Area of interest")
        aoi_layout = QVBoxLayout(aoi_group)
        aoi_layout.addWidget(self._hint(
            "Draw a rectangle on the map, or enter a WGS84 bounding box below (west, south, east, north)."
        ))
        self.draw_aoi_btn = QPushButton(QIcon(":/pwtt/icon_draw_aoi.svg"), "Draw rectangle on map")
        self.draw_aoi_btn.clicked.connect(self._activate_aoi_tool)
        aoi_layout.addWidget(self.draw_aoi_btn)
        coord_form = QFormLayout()
        coord_form.setVerticalSpacing(2)
        self.aoi_west = QDoubleSpinBox()
        self.aoi_west.setRange(-180.0, 180.0)
        self.aoi_west.setDecimals(6)
        self.aoi_west.setValue(0.0)
        coord_form.addRow("West (min lon):", self.aoi_west)
        self.aoi_south = QDoubleSpinBox()
        self.aoi_south.setRange(-90.0, 90.0)
        self.aoi_south.setDecimals(6)
        self.aoi_south.setValue(0.0)
        coord_form.addRow("South (min lat):", self.aoi_south)
        self.aoi_east = QDoubleSpinBox()
        self.aoi_east.setRange(-180.0, 180.0)
        self.aoi_east.setDecimals(6)
        self.aoi_east.setValue(0.0)
        coord_form.addRow("East (max lon):", self.aoi_east)
        self.aoi_north = QDoubleSpinBox()
        self.aoi_north.setRange(-90.0, 90.0)
        self.aoi_north.setDecimals(6)
        self.aoi_north.setValue(0.0)
        coord_form.addRow("North (max lat):", self.aoi_north)
        aoi_layout.addLayout(coord_form)
        self.set_aoi_coords_btn = QPushButton("Set AOI from coordinates")
        self.set_aoi_coords_btn.clicked.connect(self._apply_aoi_from_coordinates)
        aoi_layout.addWidget(self.set_aoi_coords_btn)
        self.clear_aoi_btn = QPushButton("Clear AOI")
        self.clear_aoi_btn.clicked.connect(self._clear_aoi)
        self.clear_aoi_btn.setEnabled(False)
        aoi_layout.addWidget(self.clear_aoi_btn)
        self.aoi_label = QLabel(
            "No area set. Draw on the map or enter coordinates and click \u201cSet AOI from coordinates\u201d."
        )
        self.aoi_label.setWordWrap(True)
        aoi_layout.addWidget(self.aoi_label)
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

        self.damage_mask_group = QGroupBox("Damage mask (T-statistic threshold)")
        dm_form = QFormLayout(self.damage_mask_group)
        self.damage_threshold_spin = QDoubleSpinBox()
        self.damage_threshold_spin.setRange(0.5, 20.0)
        self.damage_threshold_spin.setDecimals(2)
        self.damage_threshold_spin.setSingleStep(0.1)
        self.damage_threshold_spin.setValue(3.3)
        self.damage_threshold_spin.setToolTip(
            "Smoothed T-statistic cutoff for the binary damage band (band 2). "
            "Same statistic for openEO, GEE, and local backends."
        )
        dm_form.addRow("T-statistic >", self.damage_threshold_spin)
        dm_form.addRow(self._hint(
            "All backends classify damage where T exceeds this value after smoothing. "
            "Reference (UNOSAT building footprints): T>2 \u2248 max sensitivity; "
            "T>3.3 balanced default; T>4 fewer false positives; T>5 only strongest change. "
            "See github.com/oballinger/PWTT#recommended-thresholds"
        ))
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
        self._refresh_cred_storage_indicator()

    @staticmethod
    def _saved_credentials_snapshot():
        """Non-secret flags for what is persisted under PWTT/ in QgsSettings."""
        s = QgsSettings()
        s.beginGroup("PWTT")
        cid = (s.value("openeo_client_id", "") or "").strip()
        csec = (s.value("openeo_client_secret", "") or "").strip()
        gee = (s.value("gee_project", "") or "").strip()
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
                    "Not stored: no client credentials in settings — Run uses browser sign-in (OIDC)."
                )
                self.cred_storage_label.setStyleSheet("color: gray; font-size: 0.9em;")
        elif bid == "gee":
            if snap["gee_project"]:
                self.cred_storage_label.setText(
                    "Stored: GEE project name in QGIS settings."
                )
                self.cred_storage_label.setStyleSheet("color: #2e7d32; font-size: 0.9em;")
            else:
                self.cred_storage_label.setText(
                    "Not stored: no GEE project in settings yet."
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

    def _sync_aoi_coord_spinboxes(self, rect):
        """Keep manual bbox fields aligned with current AOI (EPSG:4326)."""
        if rect is None or rect.isEmpty():
            return
        for sb, val in (
            (self.aoi_west, rect.xMinimum()),
            (self.aoi_south, rect.yMinimum()),
            (self.aoi_east, rect.xMaximum()),
            (self.aoi_north, rect.yMaximum()),
        ):
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

    def _apply_aoi(self, wkt, rect):
        """Set AOI from WKT and rectangle in EPSG:4326 (axis-aligned bbox)."""
        self.aoi_wkt = wkt
        self.aoi_rect = rect
        self.aoi_label.setText(
            f"AOI: {rect.xMinimum():.4f}, {rect.yMinimum():.4f} \u2014 "
            f"{rect.xMaximum():.4f}, {rect.yMaximum():.4f} (WGS84)"
        )
        self.clear_aoi_btn.setEnabled(True)
        self._sync_aoi_coord_spinboxes(rect)
        self._draw_rubber_band(rect)

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
        self._apply_aoi(wkt, rect)
        try:
            self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        except Exception:
            pass

    def _apply_aoi_from_coordinates(self):
        west = self.aoi_west.value()
        south = self.aoi_south.value()
        east = self.aoi_east.value()
        north = self.aoi_north.value()
        if west >= east or south >= north:
            self.iface.messageBar().pushMessage(
                "PWTT",
                "Invalid bbox: need west < east and south < north (decimal degrees, WGS84).",
                level=Qgis.Warning,
                duration=6,
            )
            return
        rect = QgsRectangle(west, south, east, north)
        if rect.isEmpty():
            self.iface.messageBar().pushMessage(
                "PWTT", "AOI rectangle is empty.", level=Qgis.Warning, duration=5,
            )
            return
        geom = QgsGeometry.fromRect(rect)
        wkt = geom.asWkt()
        self._apply_aoi(wkt, rect)
        self.iface.messageBar().pushMessage(
            "PWTT", "Area of interest set from coordinates.", level=Qgis.Success, duration=4,
        )

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
        self.aoi_label.setText(
            "No area set. Draw on the map or enter coordinates and click \u201cSet AOI from coordinates\u201d."
        )
        self.clear_aoi_btn.setEnabled(False)

    # ── Load job parameters ──────────────────────────────────────────────────

    def load_job_params(self, job):
        """Populate controls from a saved job and show its AOI on the map."""
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

        fp_sources = job_footprints_sources(job)
        self.include_footprints.setChecked(bool(fp_sources))
        self.fp_current_osm.setChecked("current_osm" in fp_sources)
        self.fp_historical_war_start.setChecked("historical_war_start" in fp_sources)
        self.fp_historical_inference_start.setChecked("historical_inference_start" in fp_sources)

        self.damage_threshold_spin.setValue(float(job.get("damage_threshold", 3.3)))
        self.gee_map_preview_cb.setChecked(job.get("gee_viz", False))

        # AOI — parse WKT, set rubber band, zoom
        aoi_wkt = job.get("aoi_wkt")
        if aoi_wkt:
            bbox = wkt_to_bbox(aoi_wkt)
            if bbox:
                west, south, east, north = bbox
                rect = QgsRectangle(west, south, east, north)
                self._apply_aoi(aoi_wkt, rect)

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
        s.endGroup()
        self._refresh_cred_storage_indicator()

    def _save_settings(self):
        s = QgsSettings()
        s.beginGroup("PWTT")
        s.setValue("gee_project", self.gee_project.text())
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
        s.endGroup()
        self._refresh_cred_storage_indicator()

    def closeEvent(self, event):
        self.cleanup_map_canvas()
        super().closeEvent(event)

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        from ..core import deps

        if not self.aoi_wkt:
            QMessageBox.warning(
                self,
                "PWTT",
                "Please set an area of interest: draw on the map or enter WGS84 coordinates and "
                "click \u201cSet AOI from coordinates\u201d.",
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

        # ── Check backend dependencies (offer install if missing) ─────────
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
                # Re-check after install
                missing, _ = deps.backend_missing(backend_id, local_src)
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
            if not ensure_footprint_dependencies(self):
                return

        # ── Create backend and authenticate (single source of truth) ──────
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
            # Default to project folder or home
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

        from ..core import job_store
        job = job_store.create_job(
            backend_id=backend_id,
            aoi_wkt=self.aoi_wkt,
            war_start=self.war_start.date().toString("yyyy-MM-dd"),
            inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
            pre_interval=self.pre_interval.value(),
            post_interval=self.post_interval.value(),
            output_dir="",  # will be set below
            include_footprints=bool(fp_sources),
            footprints_sources=fp_sources,
            damage_threshold=self.damage_threshold_spin.value(),
            gee_viz=self.gee_map_preview_cb.isChecked() if backend_id == "gee" else False,
            data_source=self._local_data_source_id() if backend_id == "local" else "cdse",
        )
        # Output folder: base_dir / job_id
        job["output_dir"] = os.path.join(base_dir, job["id"])
        os.makedirs(job["output_dir"], exist_ok=True)
        job_store.save_job(job)
        self.jobs_dock.launch_job(job, backend)
