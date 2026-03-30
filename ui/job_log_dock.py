# -*- coding: utf-8 -*-
"""Dock panel for the selected job's activity log (fed by PWTTJobsDock)."""

from qgis.PyQt.QtWidgets import QDockWidget, QVBoxLayout, QTextEdit, QWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QIcon

from .dock_common import dock_title


class PWTTJobLogDock(QDockWidget):
    """Read-only activity log for the job row selected in PWTT — Jobs."""

    def __init__(self, parent=None, plugin_dir=None):
        super().__init__(dock_title("PWTT \u2014 Job log", plugin_dir), parent)
        self.setObjectName("PWTTJobLogDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.setWindowIcon(QIcon(":/pwtt/icon_job_log.svg"))

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.WidgetWidth)
        self.log_text.setMinimumHeight(120)
        lf = QFont(self.font())
        lf.setStyleHint(QFont.Monospace)
        lf.setFixedPitch(True)
        pt = lf.pointSize()
        if pt <= 0:
            pt = 10
        lf.setPointSize(max(pt, 10))
        self.log_text.setFont(lf)
        layout.addWidget(self.log_text)

        self.setWidget(w)
