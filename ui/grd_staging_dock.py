# -*- coding: utf-8 -*-
"""GRD staging dock: local CDSE jobs waiting on offline GRD."""

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
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsSettings

from .dock_common import dock_title, offline_grd_catalog_rows

from ..core.utils import format_iso_datetime_display

class PWTTGrdStagingDock(QDockWidget):
    """Panel: Local jobs with Sentinel-1 GRD products staging from CDSE cold storage."""

    _check_done = pyqtSignal(str, list)  # job_id, list of (product_id, online_bool)
    _check_log = pyqtSignal(str)

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(dock_title("PWTT \u2014 GRD staging", plugin_dir), parent)
        self.setObjectName("PWTTGrdStagingDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.jobs_dock = None
        self.controls_dock = None
        self._check_running = False
        self._build_ui()
        self._check_done.connect(self._on_check_done)
        self._check_log.connect(self._append_log)

    def _build_ui(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        hint = QLabel(
            "Local backend: jobs waiting for GRD products to come online on CDSE. "
            "The Jobs panel auto-checks every 2 minutes and resumes when all products are online."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555; font-size: 0.9em;")
        layout.addWidget(hint)

        top_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh list")
        self.refresh_btn.clicked.connect(self.refresh_list)
        top_row.addWidget(self.refresh_btn)
        self.check_btn = QPushButton("Check CDSE now")
        self.check_btn.setToolTip(
            "Query the CDSE catalogue for the selected job\u2019s products (Online flag)."
        )
        self.check_btn.clicked.connect(self._check_selected_job)
        top_row.addWidget(self.check_btn)
        self.resume_btn = QPushButton("Resume job")
        self.resume_btn.setToolTip("Same as Check && Resume in the Jobs panel.")
        self.resume_btn.clicked.connect(self._resume_selected_job)
        top_row.addWidget(self.resume_btn)
        self.focus_jobs_btn = QPushButton("Show in Jobs")
        self.focus_jobs_btn.setToolTip("Open the Jobs panel and select this job.")
        self.focus_jobs_btn.clicked.connect(self._focus_in_jobs)
        top_row.addWidget(self.focus_jobs_btn)
        top_row.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray; font-size: 0.9em;")
        top_row.addWidget(self.status_label)
        layout.addLayout(top_row)

        layout.addWidget(QLabel("Waiting jobs (Local, CDSE staging):"))
        self.jobs_table = QTableWidget(0, 4)
        self.jobs_table.setHorizontalHeaderLabels(
            ["Job", "Period (YYYY-MM)", "GRD #", "Output folder"]
        )
        self.jobs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.jobs_table.setSelectionMode(QTableWidget.SingleSelection)
        self.jobs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.jobs_table.verticalHeader().hide()
        hj = self.jobs_table.horizontalHeader()
        hj.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hj.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hj.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hj.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.jobs_table)

        layout.addWidget(QLabel("Products for selected job:"))
        self.products_table = QTableWidget(0, 4)
        self.products_table.setHorizontalHeaderLabels(
            ["Product name", "Product UUID", "Acquisition", "CDSE online"]
        )
        self.products_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.products_table.setSelectionMode(QTableWidget.SingleSelection)
        self.products_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.products_table.verticalHeader().hide()
        hp = self.products_table.horizontalHeader()
        hp.setSectionResizeMode(0, QHeaderView.Stretch)
        hp.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hp.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hp.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.products_table)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(100)
        layout.addWidget(self.log_text)

        self.jobs_table.itemSelectionChanged.connect(self._on_job_selection_changed)
        self.setWidget(w)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_list()

    def _append_log(self, msg):
        self.log_text.append(msg)

    def refresh_list(self):
        from ..core import job_store
        waiting = [
            j
            for j in job_store.load_jobs()
            if j.get("status") == job_store.STATUS_WAITING_ORDERS
            and j.get("backend_id") == "local"
            and j.get("offline_product_ids")
            and (j.get("data_source") or "cdse") == "cdse"
        ]
        self.jobs_table.setRowCount(len(waiting))
        for row, job in enumerate(waiting):
            jid = job["id"]
            c0 = QTableWidgetItem(jid)
            c0.setData(Qt.UserRole, jid)
            self.jobs_table.setItem(row, 0, c0)
            ym = f"{job['war_start'][:7]} \u2192 {job['inference_start'][:7]}"
            self.jobs_table.setItem(row, 1, QTableWidgetItem(ym))
            n = len(job.get("offline_product_ids") or [])
            self.jobs_table.setItem(row, 2, QTableWidgetItem(str(n)))
            od = job.get("output_dir") or ""
            disp = od
            if len(disp) > 52:
                disp = "\u2026" + disp[-49:]
            od_item = QTableWidgetItem(disp)
            od_item.setToolTip(od)
            self.jobs_table.setItem(row, 3, od_item)
        nwait = len(waiting)
        self.status_label.setText(
            f"{nwait} job(s) waiting" if nwait else "No jobs waiting on GRD"
        )
        self._on_job_selection_changed()

    def _selected_pwtt_job_id(self):
        row = self.jobs_table.currentRow()
        if row < 0:
            return None
        item = self.jobs_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_job_selection_changed(self):
        from ..core import job_store
        jid = self._selected_pwtt_job_id()
        self.resume_btn.setEnabled(bool(jid))
        self.focus_jobs_btn.setEnabled(bool(jid))
        self.check_btn.setEnabled(bool(jid) and not self._check_running)
        if not jid:
            self.products_table.setRowCount(0)
            return
        job = job_store.get_job(jid)
        if not job:
            self.products_table.setRowCount(0)
            return
        rows = offline_grd_catalog_rows(job)
        self.products_table.setRowCount(len(rows))
        gray = QColor("#666")
        for i, r in enumerate(rows):
            nm = r.get("name") or "\u2014"
            ni = QTableWidgetItem(nm)
            ni.setToolTip(nm if nm != "\u2014" else r["id"])
            self.products_table.setItem(i, 0, ni)
            pid = r["id"]
            short = pid[:10] + "\u2026" if len(pid) > 14 else pid
            pi = QTableWidgetItem(short)
            pi.setToolTip(pid)
            pi.setData(Qt.UserRole, pid)
            self.products_table.setItem(i, 1, pi)
            raw_date = r.get("date") or ""
            disp_date = format_iso_datetime_display(raw_date) if raw_date else ""
            self.products_table.setItem(i, 2, QTableWidgetItem(disp_date))
            st = QTableWidgetItem("\u2014")
            st.setForeground(gray)
            self.products_table.setItem(i, 3, st)

    def _check_selected_job(self):
        if self._check_running:
            self._append_log("A CDSE check is already running.")
            return
        jid = self._selected_pwtt_job_id()
        if not jid:
            return
        from ..core import job_store
        job = job_store.get_job(jid)
        if not job:
            return
        if (job.get("data_source") or "cdse") != "cdse":
            self._append_log(
                "CDSE offline check applies only to jobs that used Copernicus Data Space."
            )
            return
        pids = list(job.get("offline_product_ids") or [])
        if not pids:
            return
        s = QgsSettings()
        s.beginGroup("PWTT")
        username = s.value("cdse_username", "")
        password = s.value("cdse_password", "")
        s.endGroup()
        if not username or not password:
            self._append_log(
                "CDSE username/password not set. Enter credentials in Damage Detection panel."
            )
            QMessageBox.warning(
                self,
                "PWTT",
                "Set CDSE username and password in PWTT \u2014 Damage Detection, then try again.",
            )
            return
        self._check_running = True
        self.check_btn.setEnabled(False)
        self._append_log(f"Checking {len(pids)} product(s) for job {jid}\u2026")

        def _worker():
            try:
                from ..core.downloader import get_token, _is_product_online
                token = get_token(username, password)
                results = [(pid, _is_product_online(token, pid)) for pid in pids]
                self._check_done.emit(jid, results)
            except Exception as e:
                self._check_log.emit(f"CDSE check failed: {e}")
                self._check_done.emit("", [])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_done(self, job_id, results):
        self._check_running = False
        self._on_job_selection_changed()
        jid = self._selected_pwtt_job_id()
        if job_id and results and jid == job_id:
            green = QColor("#2E7D32")
            red = QColor("#C62828")
            for row in range(self.products_table.rowCount()):
                item = self.products_table.item(row, 1)
                if not item:
                    continue
                pid = item.data(Qt.UserRole)
                online = None
                for p, o in results:
                    if p == pid:
                        online = o
                        break
                st_item = self.products_table.item(row, 3)
                if online is None or st_item is None:
                    continue
                if online:
                    st_item.setText("Yes")
                    st_item.setForeground(green)
                else:
                    st_item.setText("No")
                    st_item.setForeground(red)
            online_n = sum(1 for _, o in results if o)
            self._append_log(
                f"Job {job_id}: {online_n}/{len(results)} product(s) online on CDSE."
            )
        elif job_id or results:
            self._append_log("CDSE check finished (job selection changed).")

    def _resume_selected_job(self):
        jid = self._selected_pwtt_job_id()
        if not jid or not self.jobs_dock:
            return
        self.jobs_dock.resume_job_by_id(jid)

    def _focus_in_jobs(self):
        jid = self._selected_pwtt_job_id()
        if not jid or not self.jobs_dock:
            return
        self.jobs_dock.focus_job(jid)

