# -*- coding: utf-8 -*-
"""Main plugin class: toolbar, menu, dock panels."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis

import os
from . import resources_rc  # noqa: F401  compiled Qt resources


class PWTTPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        # Make plugin-local deps importable
        from .core import deps
        deps.ensure_on_path()
        self.actions = []
        self.menu = "PWTT"
        self.toolbar = self.iface.addToolBar("PWTT")
        self.toolbar.setObjectName("PWTT")
        self.controls_dock = None
        self.jobs_dock = None
        self.openeo_dock = None
        self.grd_dock = None
        self._action_controls = None
        self._action_jobs = None
        self._action_openeo = None
        self._action_grd = None

    def add_action(self, icon, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True, checkable=False):
        if isinstance(icon, str):
            icon = QIcon(icon)
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setCheckable(checkable)
        if add_to_toolbar:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        self._action_controls = self.add_action(
            QIcon(":/pwtt/icon_main.svg"),
            "PWTT \u2014 Damage Detection",
            self._toggle_controls,
            checkable=True,
        )
        self._action_jobs = self.add_action(
            QIcon(":/pwtt/icon_run.svg"),
            "PWTT \u2014 Jobs",
            self._toggle_jobs,
            checkable=True,
        )
        self._action_openeo = self.add_action(
            QIcon(":/pwtt/icon_openeo.svg"),
            "PWTT \u2014 openEO Jobs",
            self._toggle_openeo,
            checkable=True,
        )
        self._action_grd = self.add_action(
            QIcon(":/pwtt/icon_grd.svg"),
            "PWTT \u2014 GRD staging",
            self._toggle_grd,
            checkable=True,
        )

    def _ensure_docks(self):
        if self.controls_dock is not None:
            return
        from .ui.main_dialog import (
            PWTTJobsDock,
            PWTTControlsDock,
            PWTTOpenEOJobsDock,
            PWTTGrdStagingDock,
        )
        mw = self.iface.mainWindow()

        self.jobs_dock = PWTTJobsDock(mw, self.plugin_dir)
        mw.addDockWidget(Qt.BottomDockWidgetArea, self.jobs_dock)
        self.jobs_dock.hide()

        self.controls_dock = PWTTControlsDock(self.iface, self.plugin_dir, self.jobs_dock, mw)
        mw.addDockWidget(Qt.RightDockWidgetArea, self.controls_dock)
        self.controls_dock.hide()

        self.openeo_dock = PWTTOpenEOJobsDock(mw, self.plugin_dir)
        mw.addDockWidget(Qt.BottomDockWidgetArea, self.openeo_dock)
        self.openeo_dock.hide()

        self.grd_dock = PWTTGrdStagingDock(mw, self.plugin_dir)
        mw.addDockWidget(Qt.BottomDockWidgetArea, self.grd_dock)
        self.grd_dock.hide()

        # Back-reference so jobs dock can load parameters into controls
        self.jobs_dock.controls_dock = self.controls_dock
        self.controls_dock.openeo_dock = self.openeo_dock
        self.openeo_dock.controls_dock = self.controls_dock
        self.grd_dock.jobs_dock = self.jobs_dock
        self.grd_dock.controls_dock = self.controls_dock

        self.jobs_dock.jobs_changed.connect(self.grd_dock.refresh_list)

        # Keep toolbar button check state in sync with dock visibility
        self.controls_dock.visibilityChanged.connect(self._action_controls.setChecked)
        self.jobs_dock.visibilityChanged.connect(self._action_jobs.setChecked)
        self.openeo_dock.visibilityChanged.connect(self._action_openeo.setChecked)
        self.grd_dock.visibilityChanged.connect(self._action_grd.setChecked)

    def _toggle_controls(self, checked=False):
        self._ensure_docks()
        if checked:
            self.controls_dock.show()
            self.controls_dock.raise_()
        else:
            self.controls_dock.hide()

    def _toggle_jobs(self, checked=False):
        self._ensure_docks()
        if checked:
            self.jobs_dock.show()
            self.jobs_dock.raise_()
        else:
            self.jobs_dock.hide()

    def _toggle_openeo(self, checked=False):
        self._ensure_docks()
        if checked:
            self.openeo_dock.show()
            self.openeo_dock.raise_()
        else:
            self.openeo_dock.hide()

    def _toggle_grd(self, checked=False):
        self._ensure_docks()
        if checked:
            self.grd_dock.show()
            self.grd_dock.raise_()
        else:
            self.grd_dock.hide()

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        try:
            self.iface.mainWindow().removeToolBar(self.toolbar)
        except Exception:
            pass
        del self.toolbar
        mw = self.iface.mainWindow()
        if self.jobs_dock and self.grd_dock:
            try:
                self.jobs_dock.jobs_changed.disconnect(self.grd_dock.refresh_list)
            except Exception:
                pass
        if self.controls_dock:
            self.controls_dock.cleanup_map_canvas()
            mw.removeDockWidget(self.controls_dock)
            self.controls_dock.deleteLater()
            self.controls_dock = None
        if self.jobs_dock:
            self.jobs_dock.cleanup()
            mw.removeDockWidget(self.jobs_dock)
            self.jobs_dock.deleteLater()
            self.jobs_dock = None
        if self.openeo_dock:
            mw.removeDockWidget(self.openeo_dock)
            self.openeo_dock.deleteLater()
            self.openeo_dock = None
        if self.grd_dock:
            mw.removeDockWidget(self.grd_dock)
            self.grd_dock.deleteLater()
            self.grd_dock = None

    def run(self):
        """Show controls dock (called from menu or legacy entry points)."""
        self._ensure_docks()
        self.controls_dock.show()
        self.controls_dock.raise_()
