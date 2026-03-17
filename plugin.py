# -*- coding: utf-8 -*-
"""Main plugin class: toolbar, menu, dialog launch."""

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
        self.actions = []
        self.menu = "PWTT"
        self.toolbar = self.iface.addToolBar("PWTT")
        self.toolbar.setObjectName("PWTT")
        self.dialog = None

    def add_action(self, icon, text, callback, enabled_flag=True, add_to_menu=True, add_to_toolbar=True):
        if isinstance(icon, str):
            icon = QIcon(icon)
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if add_to_toolbar:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        self.add_action(
            QIcon(":/pwtt/icon_main.svg"),
            "PWTT - Battle Damage Detection",
            self.run,
        )

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        try:
            self.iface.mainWindow().removeToolBar(self.toolbar)
        except Exception:
            pass
        del self.toolbar
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def run(self):
        from .ui.main_dialog import PWTTMainDialog
        if self.dialog is None:
            self.dialog = PWTTMainDialog(self.iface, self.plugin_dir, parent=self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
