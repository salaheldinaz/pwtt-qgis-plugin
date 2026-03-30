# -*- coding: utf-8 -*-
"""Dependency management for PWTT plugin.

Installs plugin-specific packages (openeo, rasterstats, …) into an isolated
directory at ``~/.qgis3/PWTT/deps/`` using the standalone *uv* binary
(auto-downloaded) or the system *pip*.  The directory is **appended** to
``sys.path`` so QGIS-bundled packages (numpy, scipy, rasterio, …) always
take priority.

Inspired by opengeos/geoai plugin dependency management (MIT).
"""

import hashlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


# ── Paths ────────────────────────────────────────────────────────────────────

def _base_dir():
    from qgis.core import QgsApplication
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")


def _deps_dir():
    return os.path.join(_base_dir(), "deps")


# ── Logging ──────────────────────────────────────────────────────────────────

def _log(message, level=None):
    try:
        from qgis.core import Qgis, QgsMessageLog
        if level is None:
            level = Qgis.Info
        QgsMessageLog.logMessage(str(message), "PWTT", level=level)
    except Exception:
        pass


def _log_warn(message):
    try:
        from qgis.core import Qgis
        _log(message, Qgis.Warning)
    except Exception:
        _log(message)


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


# ── Deps hash tracking ──────────────────────────────────────────────────────

# Bump when install logic changes significantly to force a re-check.
_INSTALL_LOGIC_VERSION = "2"


def _deps_hash_file():
    return os.path.join(_deps_dir(), ".deps_hash.txt")


def _compute_deps_hash(pip_names):
    """Hash of requested package names + logic version."""
    data = repr(sorted(pip_names)).encode("utf-8")
    data += _INSTALL_LOGIC_VERSION.encode("utf-8")
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _read_deps_hash():
    try:
        with open(_deps_hash_file(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def _write_deps_hash(pip_names):
    try:
        hf = _deps_hash_file()
        os.makedirs(os.path.dirname(hf), exist_ok=True)
        with open(hf, "w", encoding="utf-8") as f:
            f.write(_compute_deps_hash(pip_names))
    except (OSError, IOError) as e:
        _log_warn(f"Failed to write deps hash: {e}")


def deps_are_stale(pip_names):
    """Return True if the currently installed deps don't match *pip_names*."""
    stored = _read_deps_hash()
    if stored is None:
        return True
    return stored != _compute_deps_hash(pip_names)


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


# ── Error classification ─────────────────────────────────────────────────────

_SSL_PATTERNS = [
    "ssl", "certificate verify failed", "CERTIFICATE_VERIFY_FAILED",
    "SSLError", "SSLCertVerificationError", "tlsv1 alert",
    "unable to get local issuer certificate",
    "self signed certificate in certificate chain",
]

_NETWORK_PATTERNS = [
    "connectionreseterror", "connection aborted", "remotedisconnected",
    "connectionerror", "newconnectionerror", "maxretryerror",
    "protocolerror", "readtimeouterror", "connecttimeouterror",
    "network is unreachable", "temporary failure in name resolution",
    "name or service not known",
]


def _is_ssl_error(output):
    low = output.lower()
    return any(p.lower() in low for p in _SSL_PATTERNS)


def _is_network_error(output):
    low = output.lower()
    if _is_ssl_error(output):
        return False
    return any(p in low for p in _NETWORK_PATTERNS)


def _is_hash_mismatch(output):
    low = output.lower()
    return "do not match the hashes" in low or "hash mismatch" in low


def _friendly_error(output, pip_names):
    """Return a user-friendly error message based on pip/uv output."""
    if _is_ssl_error(output):
        return (
            f"SSL/certificate error installing {', '.join(pip_names)}.\n\n"
            "This is common behind corporate proxies with custom CA certificates.\n"
            "Check QGIS → Settings → Options → Network → Proxy settings.\n\n"
            f"Details: {output[-300:]}"
        )
    if _is_network_error(output):
        return (
            f"Network error installing {', '.join(pip_names)}.\n\n"
            "Check your internet connection and proxy settings.\n\n"
            f"Details: {output[-300:]}"
        )
    if _is_hash_mismatch(output):
        return (
            f"Hash mismatch installing {', '.join(pip_names)}.\n\n"
            "A cached download may be corrupted. Try clearing pip's cache:\n"
            "  pip cache purge\n\n"
            f"Details: {output[-300:]}"
        )
    return None


# ── Subprocess helpers ───────────────────────────────────────────────────────

def _get_clean_env():
    """Return env dict stripped of QGIS/venv variables that confuse pip/uv."""
    env = os.environ.copy()
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                "QGIS_PREFIX_PATH", "QGIS_PLUGINPATH"):
        env.pop(var, None)
    env["PYTHONIOENCODING"] = "utf-8"

    proxy = _get_qgis_proxy()
    if proxy:
        env.setdefault("HTTP_PROXY", proxy)
        env.setdefault("HTTPS_PROXY", proxy)
    return env


def _get_subprocess_kwargs():
    """Platform kwargs: hide Windows console, set safe cwd."""
    base_dir = _base_dir()
    os.makedirs(base_dir, exist_ok=True)
    kwargs = {"cwd": base_dir}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


def _get_qgis_proxy():
    """Read proxy URL from QGIS settings, or None."""
    try:
        from qgis.core import QgsSettings
        from urllib.parse import quote as url_quote

        s = QgsSettings()
        if not s.value("proxy/proxyEnabled", False, type=bool):
            return None
        host = s.value("proxy/proxyHost", "", type=str)
        if not host:
            return None
        port = s.value("proxy/proxyPort", "", type=str)
        user = s.value("proxy/proxyUser", "", type=str)
        password = s.value("proxy/proxyPassword", "", type=str)

        url = "http://"
        if user:
            url += url_quote(user, safe="")
            if password:
                url += ":" + url_quote(password, safe="")
            url += "@"
        url += host
        if port:
            url += f":{port}"
        return url
    except Exception:
        return None


def _pip_ssl_flags():
    return [
        "--trusted-host", "pypi.org",
        "--trusted-host", "pypi.python.org",
        "--trusted-host", "files.pythonhosted.org",
    ]


def _uv_ssl_flags():
    return [
        "--allow-insecure-host", "pypi.org",
        "--allow-insecure-host", "files.pythonhosted.org",
    ]


def _pip_proxy_args():
    proxy = _get_qgis_proxy()
    if proxy:
        return ["--proxy", proxy]
    return []


# ── Python interpreter discovery ─────────────────────────────────────────────

def _find_python_candidates():
    """Return list of Python interpreter paths bundled with this QGIS."""
    py_candidates = []
    pfx = getattr(sys, "prefix", "") or ""
    if pfx:
        for name in ("bin/python3", "bin/python"):
            p = os.path.join(pfx, name)
            if os.path.isfile(p):
                py_candidates.append(p)
        if sys.platform == "win32":
            vi = sys.version_info
            for rel in (
                ("apps", f"Python{vi.major}{vi.minor}", "python.exe"),
                ("apps", f"Python{vi.major}.{vi.minor}", "python.exe"),
            ):
                p = os.path.join(pfx, *rel)
                if os.path.isfile(p) and p not in py_candidates:
                    py_candidates.append(p)

    ex = getattr(sys, "executable", "") or ""
    if sys.platform == "darwin":
        marker = "/Contents/MacOS/"
        if marker in ex:
            app_root = ex.split(marker)[0] + "/Contents"
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

    if ex and os.path.isfile(ex) and ex not in py_candidates:
        basename = os.path.basename(ex).lower()
        if "python" in basename:
            py_candidates.append(ex)

    return py_candidates


# ── Installation (core) ──────────────────────────────────────────────────────

def _ensure_uv():
    """Ensure the standalone uv binary exists; download if needed.  Returns path or None."""
    from ._uv_manager import uv_exists, get_uv_path, download_uv

    if uv_exists():
        return get_uv_path()

    # Also check PATH as final fallback
    system_uv = shutil.which("uv")
    if system_uv:
        return system_uv

    _log("Downloading standalone uv binary…")
    ok, msg = download_uv()
    if ok:
        _log(f"uv ready: {msg}")
        return get_uv_path()

    _log_warn(f"uv download failed: {msg}")
    return None


def _run_install_command(cmd, timeout, label, progress_start, progress_end,
                         progress_callback=None, cancel_check=None):
    """Run an install command with progress polling.  Returns (returncode, stdout, stderr)."""
    env = _get_clean_env()
    kwargs = _get_subprocess_kwargs()

    stdout_fd, stdout_path = tempfile.mkstemp(suffix="_stdout.txt", prefix="pwtt_pip_")
    stderr_fd, stderr_path = tempfile.mkstemp(suffix="_stderr.txt", prefix="pwtt_pip_")

    try:
        stdout_file = os.fdopen(stdout_fd, "w", encoding="utf-8")
        stderr_file = os.fdopen(stderr_fd, "w", encoding="utf-8")
    except Exception:
        try:
            os.close(stdout_fd)
        except Exception:
            pass
        try:
            os.close(stderr_fd)
        except Exception:
            pass
        raise

    process = None
    poll_interval = 2
    try:
        process = subprocess.Popen(
            cmd, stdout=stdout_file, stderr=stderr_file,
            text=True, env=env, **kwargs,
        )
        start = time.monotonic()

        while True:
            try:
                process.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                pass

            elapsed = int(time.monotonic() - start)

            if cancel_check and cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return -1, "", "Cancelled"

            if elapsed >= timeout:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return -2, "", f"Timed out after {timeout}s"

            if progress_callback:
                elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"

                # Read last line of stdout for download info
                dl_status = ""
                try:
                    with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 4096))
                        tail = f.read()
                        for line in reversed(tail.strip().split("\n")):
                            m = re.search(r"Downloading\s+(\S+)\s+\(([^)]+)\)", line)
                            if m:
                                pkg = m.group(1).rsplit("/", 1)[-1]
                                nm = re.match(r"([A-Za-z][A-Za-z0-9_]*)", pkg)
                                dl_status = f"Downloading {nm.group(1) if nm else pkg} ({m.group(2)})"
                                break
                except Exception:
                    pass

                msg = f"{dl_status}… {elapsed_str}" if dl_status else f"Installing {label}… {elapsed_str}"
                progress_range = progress_end - progress_start
                fraction = min(elapsed / max(timeout, 1), 0.9)
                pct = progress_start + int(progress_range * fraction)
                pct = min(pct, progress_end - 1)
                progress_callback(pct, msg)

        stdout_file.close()
        stderr_file.close()
        stdout_file = None
        stderr_file = None

        try:
            with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                full_stdout = f.read()
        except Exception:
            full_stdout = ""
        try:
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
                full_stderr = f.read()
        except Exception:
            full_stderr = ""

        return process.returncode, full_stdout, full_stderr

    except Exception:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
        raise
    finally:
        if stdout_file is not None:
            try:
                stdout_file.close()
            except Exception:
                pass
        if stderr_file is not None:
            try:
                stderr_file.close()
            except Exception:
                pass
        try:
            os.unlink(stdout_path)
        except Exception:
            pass
        try:
            os.unlink(stderr_path)
        except Exception:
            pass


def _install_into_target(pip_names, target_dir,
                         progress_callback=None, cancel_check=None):
    """Install packages into *target_dir* using uv (preferred) or pip.

    Safe to call from a ``QThread`` — no QGIS/Qt calls.
    """
    pip_names = list(pip_names)
    if not pip_names:
        raise RuntimeError("No packages specified for install.")

    os.makedirs(target_dir, exist_ok=True)

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    py_candidates = _find_python_candidates()
    uv_path = _ensure_uv()

    # --- Build ordered list of install commands to try ---
    attempts = []

    # 1) uv with QGIS python (best: fast + correct wheels)
    if uv_path and py_candidates:
        attempts.append((
            [uv_path, "pip", "install", "--upgrade", "--target", target_dir,
             "--python", py_candidates[0]] + _uv_ssl_flags() + list(pip_names),
            f"uv + {os.path.basename(py_candidates[0])}"
        ))

    # 2) uv with auto python version (if no QGIS python found)
    if uv_path and not py_candidates:
        attempts.append((
            [uv_path, "pip", "install", "--upgrade", "--target", target_dir,
             "--python-version", py_ver] + _uv_ssl_flags() + list(pip_names),
            "uv (auto python)"
        ))

    # 3) QGIS python + pip
    pip_tail = ["-m", "pip", "install", "--upgrade", "--target", target_dir,
                "--prefer-binary", "--no-warn-script-location",
                "--disable-pip-version-check"] + _pip_proxy_args() + list(pip_names)
    for py in py_candidates:
        attempts.append(([py] + pip_tail, f"pip via {os.path.basename(py)}"))

    # 4) System python3 + pip (last resort)
    attempts.append((
        ["python3", "-m", "pip", "install", "--upgrade", "--target", target_dir,
         "--prefer-binary", "--no-warn-script-location",
         "--disable-pip-version-check"] + _pip_proxy_args() + list(pip_names),
        "system python3"
    ))

    label = ", ".join(pip_names)
    n = len(attempts)
    last_output = ""
    last_exc = None
    ssl_retry_done = False

    for i, (cmd, desc) in enumerate(attempts):
        _log(f"Install attempt {i + 1}/{n}: {desc}")

        # Progress: split the 15–90 range across attempts
        p_start = 15 + int(75 * i / n)
        p_end = 15 + int(75 * (i + 1) / n)

        try:
            rc, stdout, stderr = _run_install_command(
                cmd, timeout=300, label=label,
                progress_start=p_start, progress_end=p_end,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

            if rc == 0:
                _log(f"Install succeeded via {desc}")
                return

            combined = (stdout + "\n" + stderr).strip()
            last_output = combined
            _log_warn(f"Attempt {desc} failed (rc={rc}): {combined[-500:]}")

            # SSL error → retry same command with SSL bypass flags (once)
            if _is_ssl_error(combined) and not ssl_retry_done:
                ssl_retry_done = True
                _log("Retrying with SSL bypass flags…")
                if uv_path and cmd[0] == uv_path:
                    retry_cmd = cmd + _uv_ssl_flags()
                else:
                    retry_cmd = cmd + _pip_ssl_flags()
                rc2, stdout2, stderr2 = _run_install_command(
                    retry_cmd, timeout=300, label=label,
                    progress_start=p_start, progress_end=p_end,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
                if rc2 == 0:
                    _log("SSL retry succeeded")
                    return
                last_output = (stdout2 + "\n" + stderr2).strip()

            if rc == -1:
                raise RuntimeError("Installation cancelled.")

        except FileNotFoundError:
            last_exc = True
            _log_warn(f"Attempt {desc}: interpreter not found")
            continue
        except subprocess.TimeoutExpired:
            last_output = f"Timed out (300s) during {desc}"
            _log_warn(last_output)
            continue

    # All attempts failed
    friendly = _friendly_error(last_output, pip_names)
    if friendly:
        raise RuntimeError(friendly)

    hint_py = py_candidates[0] if py_candidates else "python3"
    raise RuntimeError(
        f"Failed to install {', '.join(pip_names)} "
        f"(tried uv, QGIS python, system python3).\n\n"
        f"Manual install:\n"
        f'  "{hint_py}" -m pip install --target "{target_dir}" {" ".join(pip_names)}\n\n'
        f"Last output:\n{last_output[-500:]}"
    )


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

    Prefers the standalone uv binary (auto-downloaded).  Falls back to
    QGIS python + pip, then system python3.  Raises `RuntimeError` on failure.

    Must run on the **Qt main thread** (uses ``QgsApplication`` for paths).
    """
    d = _deps_dir()
    _install_into_target(pip_names, d)
    _write_deps_hash(pip_names)
    _finalize_install(pip_names)


def install_with_dialog(pip_names, parent=None):
    """Install packages with a progress dialog.  Returns True on success."""
    from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
    from qgis.PyQt.QtWidgets import QApplication, QProgressDialog, QMessageBox

    if not pip_names:
        QMessageBox.warning(
            parent,
            "PWTT",
            "No installable packages were requested (e.g. only QGIS-bundled "
            "dependencies are missing). Install or enable them in QGIS, then try again.",
        )
        return False

    target_dir = _deps_dir()
    os.makedirs(target_dir, exist_ok=True)

    class _Worker(QThread):
        progress = pyqtSignal(int, str)

        def __init__(self, names, tdir):
            super().__init__()
            self.names = names
            self.tdir = tdir
            self.error = None
            self._cancelled = False

        def cancel(self):
            self._cancelled = True

        def run(self):
            try:
                _install_into_target(
                    self.names, self.tdir,
                    progress_callback=lambda pct, msg: self.progress.emit(pct, msg),
                    cancel_check=lambda: self._cancelled,
                )
            except Exception as e:
                self.error = str(e)

    dlg = QProgressDialog(
        f"Installing {', '.join(pip_names)}\u2026", "Cancel", 0, 100, parent
    )
    dlg.setWindowTitle("PWTT \u2014 Installing Dependencies")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setValue(0)

    worker = _Worker(pip_names, target_dir)

    def _on_progress(pct, msg):
        if not dlg.wasCanceled():
            dlg.setValue(pct)
            dlg.setLabelText(msg)

    def _on_worker_finished():
        if dlg.wasCanceled():
            return
        if worker.error:
            dlg.reject()
        else:
            dlg.setValue(100)
            dlg.accept()

    def _on_cancel():
        worker.cancel()

    worker.progress.connect(_on_progress, Qt.QueuedConnection)
    worker.finished.connect(_on_worker_finished, Qt.QueuedConnection)
    dlg.canceled.connect(_on_cancel)

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

    _write_deps_hash(pip_names)
    _finalize_install(pip_names)
    return True
