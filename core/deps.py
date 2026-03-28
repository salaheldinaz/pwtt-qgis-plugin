# -*- coding: utf-8 -*-
"""Dependency management for PWTT plugin.

Installs plugin-specific packages (openeo, rasterstats, …) into an isolated
directory at ``~/.qgis3/PWTT/deps/`` using *uv* (preferred) or the system
*pip*.  The directory is **appended** to ``sys.path`` so QGIS-bundled packages
(numpy, scipy, rasterio, …) always take priority.

Note: QGIS's Python cannot be invoked outside the QGIS process (it has
hard-coded build paths), so we cannot create a venv from it.  ``--target``
achieves the same isolation without needing a functional standalone Python.
"""

import importlib
import os
import shutil
import subprocess
import sys


# ── Paths ────────────────────────────────────────────────────────────────────

def _base_dir():
    from qgis.core import QgsApplication
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")


def _deps_dir():
    return os.path.join(_base_dir(), "deps")


# ── sys.path management ─────────────────────────────────────────────────────

def ensure_on_path():
    """Append the deps directory to *sys.path* (idempotent).

    **Appended** — never prepended — so QGIS's own packages always win.
    This means even if *uv* pulls in numpy as a transitive dep, QGIS's
    numpy is found first.
    """
    d = _deps_dir()
    if os.path.isdir(d) and d not in sys.path:
        sys.path.append(d)


# ── Package / backend mapping ────────────────────────────────────────────────

# Packages shipped inside QGIS — we only *check* these; we never install them.
QGIS_PROVIDED = {"numpy", "scipy", "rasterio", "requests", "geopandas"}

BACKEND_DEPS = {
    "openeo": {"import": ["openeo"], "pip": ["openeo"]},
    "gee":    {"import": ["ee"],     "pip": ["earthengine-api"]},
    "local":  {
        "import": ["numpy", "scipy", "rasterio", "requests"],
        "pip":    [],  # all QGIS-provided
    },
}

FOOTPRINT_DEPS = {
    "import": ["geopandas", "rasterstats"],
    "pip":    ["rasterstats"],  # geopandas is QGIS-provided
}


# ── Queries ──────────────────────────────────────────────────────────────────

def find_missing(import_names):
    """Return the subset of *import_names* that cannot be imported.

    For ``rasterstats`` we additionally verify `zonal_stats` is importable,
    because a QGIS plugin of the same name can shadow the real package.
    """
    missing = []
    for name in import_names:
        try:
            mod = __import__(name)
            # Guard against QGIS plugin "rasterstats" shadowing the real one
            if name == "rasterstats" and not hasattr(mod, "zonal_stats"):
                missing.append(name)
        except ImportError:
            missing.append(name)
    return missing


def backend_missing(backend_id):
    """Return ``(missing_import_names, pip_names_to_install)``."""
    info = BACKEND_DEPS.get(backend_id)
    if not info:
        return [], []
    m = find_missing(info["import"])
    if not m:
        return [], []
    return m, list(info["pip"])


def footprint_missing():
    """Return ``(missing_import_names, pip_names_to_install)``."""
    m = find_missing(FOOTPRINT_DEPS["import"])
    if not m:
        return [], []
    pip = [p for p in FOOTPRINT_DEPS["pip"] if p in m]
    return m, pip


# ── Installation ─────────────────────────────────────────────────────────────

def install(pip_names):
    """Install *pip_names* into the plugin deps directory.

    Uses ``uv pip install --target`` when *uv* is on PATH (fast, resolves for
    the correct Python version).  Falls back to ``python3 -m pip install
    --target``.  Raises `RuntimeError` on failure.
    """
    d = _deps_dir()
    os.makedirs(d, exist_ok=True)

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    uv = shutil.which("uv")

    if uv:
        cmd = [uv, "pip", "install",
               "--target", d,
               "--python-version", py_ver] + list(pip_names)
    else:
        # System python3 (not QGIS's — that one can't run standalone)
        cmd = ["python3", "-m", "pip", "install",
               "--target", d] + list(pip_names)

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=300)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to install {', '.join(pip_names)}:\n"
            f"{e.output.decode(errors='replace')}"
        ) from e
    except FileNotFoundError:
        raise RuntimeError(
            "Cannot find uv or pip.  Install packages manually:\n"
            f"  uv pip install --target \"{d}\" {' '.join(pip_names)}"
        )

    ensure_on_path()
    importlib.invalidate_caches()


def install_with_dialog(pip_names, parent=None):
    """Install packages with a progress dialog.  Returns True on success."""
    from qgis.PyQt.QtCore import Qt, QThread
    from qgis.PyQt.QtWidgets import QProgressDialog, QMessageBox

    class _Worker(QThread):
        def __init__(self, names):
            super().__init__()
            self.names = names
            self.error = None

        def run(self):
            try:
                install(self.names)
            except Exception as e:
                self.error = str(e)

    dlg = QProgressDialog(
        f"Installing {', '.join(pip_names)}\u2026", "Cancel", 0, 0, parent
    )
    dlg.setWindowTitle("PWTT \u2014 Installing Dependencies")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)

    worker = _Worker(pip_names)
    worker.finished.connect(dlg.close)
    worker.start()
    dlg.exec_()

    if dlg.wasCanceled():
        worker.wait(5000)
        return False

    worker.wait()
    if worker.error:
        QMessageBox.critical(parent, "PWTT", worker.error)
        return False
    return True
