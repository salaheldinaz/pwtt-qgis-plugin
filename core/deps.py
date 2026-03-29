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

def _try_import_zonal_stats():
    """Return True if ``from rasterstats import zonal_stats`` yields a callable."""
    from rasterstats import zonal_stats
    if not callable(zonal_stats):
        raise ImportError("rasterstats.zonal_stats is not callable")
    return True


def _rasterstats_probe():
    """Return ``(ok, detail)``.  *detail* is non-empty on failure (for error dialogs).

    Mirrors ``footprints.compute_footprints``: prefer ``from rasterstats import zonal_stats``,
    then force-load from ``_deps_dir()`` if a QGIS plugin shadows the name.
    """
    # 1) Try the normal import path
    try:
        _try_import_zonal_stats()
        return True, ""
    except Exception as e:
        first = str(e)

    # 2) Try from deps dir with path priority + module cache purge
    ensure_on_path()
    d = _deps_dir()
    if not os.path.isdir(d):
        return False, f"{first}\n(Deps folder does not exist yet: {d})"

    _saved_path = sys.path[:]
    _saved_mods = {k: sys.modules[k] for k in list(sys.modules)
                   if k == "rasterstats" or k.startswith("rasterstats.")}
    try:
        sys.path.insert(0, d)
        for key in list(_saved_mods):
            del sys.modules[key]
        importlib.invalidate_caches()
        _try_import_zonal_stats()
        return True, ""
    except ImportError as e:
        return False, (
            f"{first}\n"
            f"Loading from PWTT deps dir also failed: {e}\n"
            f"Often this means packages were built for a different Python than QGIS uses. "
            f"The plugin now installs with QGIS's interpreter first — try Install again, "
            f"or run:\n  \"{sys.executable}\" -m pip install --target \"{d}\" rasterstats"
        )
    except Exception as e:
        return False, f"{first}\nFrom deps dir: {type(e).__name__}: {e}"
    finally:
        sys.path[:] = _saved_path
        # Restore original module state so we don't leave half-loaded modules
        for key in list(sys.modules):
            if key == "rasterstats" or key.startswith("rasterstats."):
                del sys.modules[key]
        sys.modules.update(_saved_mods)


def _rasterstats_ok():
    ok, _ = _rasterstats_probe()
    return ok


def rasterstats_failure_detail():
    """Short diagnostic when footprints need rasterstats but it is not usable."""
    ok, detail = _rasterstats_probe()
    return "" if ok else detail


def find_missing(import_names):
    """Return the subset of *import_names* that cannot be imported.

    For ``rasterstats`` we additionally verify `zonal_stats` is importable,
    because a QGIS plugin of the same name can shadow the real package.
    """
    missing = []
    for name in import_names:
        if name == "rasterstats":
            if not _rasterstats_ok():
                missing.append(name)
            continue
        try:
            __import__(name)
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

def _install_into_target(pip_names, target_dir):
    """Run pip/uv only (no QGIS/Qt). Safe to call from a ``QThread``."""
    pip_names = list(pip_names)
    if not pip_names:
        raise RuntimeError("No packages specified for install.")

    os.makedirs(target_dir, exist_ok=True)

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    uv = shutil.which("uv")

    attempts = []

    # 1) Interpreters bundled with this QGIS (correct wheels). ``sys.executable``
    #    is sometimes the QGIS binary on macOS — also try ``sys.prefix/bin/python*``.
    py_candidates = []
    pfx = getattr(sys, "prefix", "") or ""
    if pfx:
        for name in ("bin/python3", "bin/python"):
            p = os.path.join(pfx, name)
            if os.path.isfile(p):
                py_candidates.append(p)
    ex = getattr(sys, "executable", "") or ""
    if ex and os.path.isfile(ex) and ex not in py_candidates:
        py_candidates.append(ex)

    pip_tail = ["-m", "pip", "install", "--upgrade", "--target", target_dir] + list(pip_names)
    for py in py_candidates:
        attempts.append([py] + pip_tail)

    if uv:
        attempts.append(
            [uv, "pip", "install", "--upgrade",
             "--target", target_dir,
             "--python-version", py_ver]
            + list(pip_names)
        )

    attempts.append(
        ["python3", "-m", "pip", "install", "--upgrade", "--target", target_dir]
        + list(pip_names)
    )

    last_out = b""
    last_exc = None
    for cmd in attempts:
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=300)
            return
        except FileNotFoundError as e:
            last_exc = e
            continue
        except subprocess.CalledProcessError as e:
            last_out = e.output or b""
            last_exc = e
            continue

    tail = last_out.decode(errors="replace") if last_out else ""
    if isinstance(last_exc, subprocess.CalledProcessError):
        raise RuntimeError(
            f"Failed to install {', '.join(pip_names)} (tried QGIS python, uv, then python3):\n"
            f"{tail}"
        ) from last_exc
    hint_py = py_candidates[0] if py_candidates else (ex or "python3")
    raise RuntimeError(
        "Cannot run pip (no working QGIS/python interpreter, uv, or python3). Install manually:\n"
        f"  \"{hint_py}\" -m pip install --target \"{target_dir}\" {' '.join(pip_names)}"
    ) from last_exc


def _finalize_install(pip_names):
    """Update ``sys.path`` and drop stale *rasterstats* imports (main thread only)."""
    ensure_on_path()
    importlib.invalidate_caches()
    pip_names = list(pip_names)
    if "rasterstats" in pip_names:
        for key in list(sys.modules):
            if key == "rasterstats" or key.startswith("rasterstats."):
                del sys.modules[key]
        importlib.invalidate_caches()


def install(pip_names):
    """Install *pip_names* into the plugin deps directory.

    Uses ``uv pip install --target`` when *uv* is on PATH (fast, resolves for
    the correct Python version).  Falls back to ``python3 -m pip install
    --target``.  Raises `RuntimeError` on failure.

    Must run on the **Qt main thread** (uses ``QgsApplication`` for paths).
    """
    d = _deps_dir()
    _install_into_target(pip_names, d)
    _finalize_install(pip_names)


def install_with_dialog(pip_names, parent=None):
    """Install packages with a progress dialog.  Returns True on success."""
    from qgis.PyQt.QtCore import Qt, QThread
    from qgis.PyQt.QtWidgets import QApplication, QProgressDialog, QMessageBox

    if not pip_names:
        QMessageBox.warning(
            parent,
            "PWTT",
            "No installable packages were requested (e.g. only QGIS-bundled "
            "dependencies are missing). Install or enable them in QGIS, then try again.",
        )
        return False

    # Resolve install dir on the Qt main thread — never call QgsApplication from QThread.
    target_dir = _deps_dir()
    os.makedirs(target_dir, exist_ok=True)

    class _Worker(QThread):
        def __init__(self, names, tdir):
            super().__init__()
            self.names = names
            self.tdir = tdir
            self.error = None

        def run(self):
            try:
                _install_into_target(self.names, self.tdir)
            except Exception as e:
                self.error = str(e)

    dlg = QProgressDialog(
        f"Installing {', '.join(pip_names)}\u2026", "Cancel", 0, 0, parent
    )
    dlg.setWindowTitle("PWTT \u2014 Installing Dependencies")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)

    worker = _Worker(pip_names, target_dir)

    def _on_worker_finished():
        if dlg.wasCanceled():
            return
        if worker.error:
            dlg.reject()
        else:
            dlg.accept()

    worker.finished.connect(_on_worker_finished, Qt.QueuedConnection)
    worker.start()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    QApplication.processEvents()
    dlg.exec_()

    worker.wait(600_000)

    if dlg.wasCanceled():
        return False

    if worker.error:
        QMessageBox.critical(parent, "PWTT", worker.error)
        return False

    _finalize_install(pip_names)
    return True
