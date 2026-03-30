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

_macos_bundle_site_injected = False


def _inject_macos_bundle_python_site_packages():
    """Once: append site-packages dirs reported by ``Contents/MacOS/python``.

    The QGIS GUI process often omits paths that ``MacOS/python -m pip`` uses, so
    packages installed from the terminal look fine but ``import`` fails inside the
    plugin and the panel keeps showing "Missing".
    """
    global _macos_bundle_site_injected
    if _macos_bundle_site_injected:
        return
    if sys.platform != "darwin":
        _macos_bundle_site_injected = True
        return
    ex = getattr(sys, "executable", "") or ""
    marker = "/Contents/MacOS/"
    if marker not in ex:
        _macos_bundle_site_injected = True
        return
    app_root = os.path.normpath(ex.split(marker)[0] + "/Contents")
    mac_py = None
    for name in ("python", "python3"):
        p = os.path.join(app_root, "MacOS", name)
        if os.path.isfile(p):
            mac_py = p
            break
    if not mac_py:
        _macos_bundle_site_injected = True
        return
    try:
        out = subprocess.check_output(
            [
                mac_py,
                "-c",
                "import os, site\n"
                "paths = list(site.getsitepackages())\n"
                "u = site.getusersitepackages()\n"
                "if u:\n"
                "    paths.append(u)\n"
                "for p in paths:\n"
                "    if p and os.path.isdir(p):\n"
                "        print(p)",
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        _macos_bundle_site_injected = True
        return
    app_prefix = app_root + os.sep
    for line in out.splitlines():
        p = line.strip()
        if not p or p in sys.path:
            continue
        try:
            norm = os.path.normpath(p) + os.sep
        except Exception:
            continue
        if not norm.startswith(app_prefix):
            continue
        sys.path.append(p.rstrip(os.sep))
    _macos_bundle_site_injected = True


def ensure_on_path():
    """Append the deps directory to *sys.path* (idempotent).

    **Appended** — never prepended — so QGIS's own packages always win.
    This means even if *uv* pulls in numpy as a transitive dep, QGIS's
    numpy is found first.
    """
    _inject_macos_bundle_python_site_packages()
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
        "import": ["numpy", "rasterio", "requests"],
        "pip":    [],  # all QGIS-provided; scipy not required (numpy-only ops)
    },
}

# Extra imports per local GRD source (import_name -> pip package for install dialog)
LOCAL_SOURCE_EXTRA_IMPORTS = {
    "cdse": [],
    "asf": ["asf_search"],  # PyPI: asf-search
    "pc": ["planetary_computer", "pystac_client"],
}

LOCAL_SOURCE_PIP_NAMES = {
    "asf_search": "asf-search",
    "planetary_computer": "planetary-computer",
    "pystac_client": "pystac-client",
}

FOOTPRINT_DEPS = {
    "import": ["geopandas", "rasterstats"],
    "pip":    ["rasterstats"],  # geopandas is QGIS-provided
}


# ── Queries ──────────────────────────────────────────────────────────────────

def _purge_rasterstats_modules():
    """Remove all cached ``rasterstats*`` entries from ``sys.modules``."""
    for key in list(sys.modules):
        if key == "rasterstats" or key.startswith("rasterstats."):
            del sys.modules[key]


def _try_import_zonal_stats():
    """Return True if ``from rasterstats import zonal_stats`` yields a callable."""
    from rasterstats import zonal_stats
    if not callable(zonal_stats):
        raise ImportError("rasterstats.zonal_stats is not callable")
    return True


def _find_real_rasterstats_dir():
    """Find a ``site-packages`` directory on ``sys.path`` that contains the
    *real* PyPI ``rasterstats`` (i.e. has ``zonal_stats``).

    A QGIS plugin named ``rasterstats`` (under ``.../python/plugins/``) can
    shadow the real package.  We scan ``sys.path`` for ``rasterstats``
    directories that live in ``site-packages`` and check for ``zonal_stats.py``
    or ``zonal_stats`` in the package.
    """
    for entry in sys.path:
        if "site-packages" not in entry:
            continue
        candidate = os.path.join(entry, "rasterstats")
        if not os.path.isdir(candidate):
            continue
        # The real package has a zonal_stats sub-module
        if (os.path.isfile(os.path.join(candidate, "zonal_stats.py"))
                or os.path.isfile(os.path.join(candidate, "_zonal_stats.py"))
                or os.path.isfile(os.path.join(candidate, "main.py"))):
            return entry
    return None


def _rasterstats_probe():
    """Return ``(ok, detail)``.  *detail* is non-empty on failure (for error dialogs).

    Strategy (mirrors ``footprints.compute_footprints``):
      1. Normal ``from rasterstats import zonal_stats``.
      2. If shadowed by a QGIS plugin, find the real package in site-packages,
         temporarily prioritise that path, purge cached modules, retry.
      3. Try from ``_deps_dir()`` as last resort.
    """
    # 1) Normal import
    try:
        _try_import_zonal_stats()
        return True, ""
    except Exception as e:
        first = str(e)

    # 2) Find the real rasterstats in site-packages (bypasses QGIS plugin shadow)
    real_dir = _find_real_rasterstats_dir()
    extra_dirs = [d for d in [real_dir, _deps_dir()] if d and os.path.isdir(d)]

    if not extra_dirs:
        ensure_on_path()
        d = _deps_dir()
        extra_dirs = [d] if os.path.isdir(d) else []

    if not extra_dirs:
        return False, (
            f"{first}\n"
            f"No site-packages rasterstats found and deps folder does not exist yet."
        )

    _saved_path = sys.path[:]
    _saved_mods = {k: sys.modules[k] for k in list(sys.modules)
                   if k == "rasterstats" or k.startswith("rasterstats.")}
    errors = [first]
    try:
        for d in extra_dirs:
            _purge_rasterstats_modules()
            importlib.invalidate_caches()
            sys.path[:] = [d] + [p for p in _saved_path if p != d]
            try:
                _try_import_zonal_stats()
                return True, ""
            except Exception as e:
                errors.append(f"from {d}: {e}")
                continue

        return False, "\n".join(errors)
    finally:
        sys.path[:] = _saved_path
        _purge_rasterstats_modules()
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


def local_backend_missing(local_data_source: str = "cdse"):
    """Return ``(missing_import_names, pip_names_to_install)`` for Local Processing.

    *local_data_source* is ``cdse``, ``asf``, or ``pc``.
    """
    src = (local_data_source or "cdse").strip().lower()
    if src not in ("cdse", "asf", "pc"):
        src = "cdse"
    base = BACKEND_DEPS["local"]["import"]
    extra = LOCAL_SOURCE_EXTRA_IMPORTS.get(src, [])
    names = list(base) + list(extra)
    m = find_missing(names)
    if not m:
        return [], []
    pip_out = []
    for imp in m:
        pip_out.append(LOCAL_SOURCE_PIP_NAMES.get(imp, imp))
    # De-dup while preserving order
    seen = set()
    pip_unique = []
    for p in pip_out:
        if p not in seen:
            seen.add(p)
            pip_unique.append(p)
    return m, pip_unique


def backend_missing(backend_id, local_data_source=None):
    """Return ``(missing_import_names, pip_names_to_install)``."""
    if backend_id == "local":
        return local_backend_missing(local_data_source or "cdse")
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

    # 1) Interpreters bundled with this QGIS (correct wheels).
    #    On macOS ``sys.executable`` is often the QGIS binary itself
    #    (e.g. /Applications/QGIS.app/Contents/MacOS/QGIS) — running it
    #    with ``-m pip`` launches a *new* QGIS instance.  We look for the
    #    actual Python under ``sys.prefix`` and the Frameworks tree first.
    py_candidates = []
    pfx = getattr(sys, "prefix", "") or ""
    if pfx:
        for name in ("bin/python3", "bin/python"):
            p = os.path.join(pfx, name)
            if os.path.isfile(p):
                py_candidates.append(p)
        # Windows (OSGeo4W / standalone installer): python is under apps\Python312\, not bin\python3
        if sys.platform == "win32":
            vi = sys.version_info
            for rel in (
                ("apps", f"Python{vi.major}{vi.minor}", "python.exe"),
                ("apps", f"Python{vi.major}.{vi.minor}", "python.exe"),
            ):
                p = os.path.join(pfx, *rel)
                if os.path.isfile(p) and p not in py_candidates:
                    py_candidates.append(p)
    # On macOS the bundle may be QGIS.app, QGIS-LTR.app, etc. — not only "QGIS.app"
    ex = getattr(sys, "executable", "") or ""
    if sys.platform == "darwin":
        marker = "/Contents/MacOS/"
        if marker in ex:
            app_root = ex.split(marker)[0] + "/Contents"
            # Prefer MacOS/python first: same binary users run for ``-m pip`` in Terminal.
            for candidate in (
                "MacOS/python",
                "MacOS/python3",
                f"Frameworks/Python.framework/Versions/{sys.version_info.major}.{sys.version_info.minor}/bin/python3",
                f"Frameworks/bin/python{sys.version_info.major}.{sys.version_info.minor}",
                "Frameworks/bin/python3",
                "MacOS/bin/python3",
            ):
                p = os.path.join(app_root, candidate)
                if os.path.isfile(p) and p not in py_candidates:
                    py_candidates.append(p)
    # Only add sys.executable if it's actually a Python interpreter, not the QGIS binary
    if ex and os.path.isfile(ex) and ex not in py_candidates:
        basename = os.path.basename(ex).lower()
        if "python" in basename:
            py_candidates.append(ex)

    pip_tail = ["-m", "pip", "install", "--upgrade", "--target", target_dir] + list(pip_names)
    for py in py_candidates:
        attempts.append([py] + pip_tail)

    if uv:
        # Prefer the same interpreter QGIS uses so wheels match; else uv's standalone Python.
        uv_base = [
            uv, "pip", "install", "--upgrade", "--target", target_dir,
        ]
        if py_candidates:
            attempts.append(uv_base + ["--python", py_candidates[0]] + list(pip_names))
        else:
            attempts.append(
                uv_base + ["--python-version", py_ver] + list(pip_names)
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
