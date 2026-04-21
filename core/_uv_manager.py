# -*- coding: utf-8 -*-
"""Standalone uv binary manager for PWTT QGIS Plugin.

Downloads and manages the uv package installer binary so installs work
even when the user has no ``uv`` or ``pip`` on PATH.  Adapted from
opengeos/geoai (MIT).
"""

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


UV_VERSION = "0.10.6"


def _cache_dir():
    """Plugin-level cache dir (same parent as deps)."""
    from qgis.core import QgsApplication
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")


def _uv_dir():
    return os.path.join(_cache_dir(), "uv")


def get_uv_path():
    if sys.platform == "win32":
        return os.path.join(_uv_dir(), "uv.exe")
    return os.path.join(_uv_dir(), "uv")


def uv_exists():
    return os.path.isfile(get_uv_path())


def _platform_info():
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        arch = "aarch64-apple-darwin" if machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
        return arch, ".tar.gz"
    elif sys.platform == "win32":
        return "x86_64-pc-windows-msvc", ".zip"
    else:
        arch = "aarch64-unknown-linux-gnu" if machine in ("arm64", "aarch64") else "x86_64-unknown-linux-gnu"
        return arch, ".tar.gz"


def _download_url():
    plat, ext = _platform_info()
    return f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{plat}{ext}"


def _find_file(directory, filename):
    for root, _dirs, files in os.walk(directory):
        if filename in files:
            return os.path.join(root, filename)
    return None


def _safe_extract_tar(tar, dest):
    dest = os.path.realpath(dest)
    use_filter = sys.version_info >= (3, 12)
    for member in tar.getmembers():
        member_path = os.path.realpath(os.path.join(dest, member.name))
        if not member_path.startswith(dest + os.sep) and member_path != dest:
            raise ValueError(f"Path traversal in tar: {member.name}")
        if not use_filter and (member.issym() or member.islnk()):
            raise ValueError(f"Refusing symlink/hardlink in tar: {member.name}")
        if use_filter:
            tar.extract(member, dest, filter="data")
        else:
            tar.extract(member, dest)


def _safe_extract_zip(zf, dest):
    dest = os.path.realpath(dest)
    for name in zf.namelist():
        member_path = os.path.realpath(os.path.join(dest, name))
        if not member_path.startswith(dest + os.sep) and member_path != dest:
            raise ValueError(f"Path traversal in zip: {name}")
        zf.extract(name, dest)


def download_uv(progress_callback=None, use_qgis_network=True):
    """Download uv binary.  Returns ``(success, message)``."""
    if uv_exists():
        return True, "uv already installed"

    url = _download_url()
    _, ext = _platform_info()

    if progress_callback:
        progress_callback(0, f"Downloading uv {UV_VERSION}…")

    fd, tmp = tempfile.mkstemp(suffix=ext)
    os.close(fd)

    try:
        if use_qgis_network:
            ok, msg = _download_via_qgis(url, tmp, progress_callback)
        else:
            ok, msg = _download_via_urllib(url, tmp, progress_callback)

        if not ok:
            return False, msg

        if progress_callback:
            progress_callback(60, "Extracting uv…")

        uv_dir = _uv_dir()
        if os.path.exists(uv_dir):
            shutil.rmtree(uv_dir)
        os.makedirs(uv_dir, exist_ok=True)

        extract_dir = tempfile.mkdtemp()
        try:
            if tmp.endswith(".zip"):
                with zipfile.ZipFile(tmp, "r") as zf:
                    _safe_extract_zip(zf, extract_dir)
            else:
                with tarfile.open(tmp, "r:gz") as tar:
                    _safe_extract_tar(tar, extract_dir)

            binary_name = "uv.exe" if sys.platform == "win32" else "uv"
            src = _find_file(extract_dir, binary_name)
            if src is None:
                return False, "uv binary not found in archive"

            dest = get_uv_path()
            shutil.copy2(src, dest)
            if sys.platform != "win32":
                os.chmod(dest, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)

        if progress_callback:
            progress_callback(80, "Verifying uv…")

        ok, vmsg = verify_uv()
        if ok:
            if progress_callback:
                progress_callback(100, f"uv {UV_VERSION} ready")
            return True, f"uv {UV_VERSION} installed"
        shutil.rmtree(uv_dir, ignore_errors=True)
        return False, f"Verification failed: {vmsg}"

    except Exception as e:
        shutil.rmtree(_uv_dir(), ignore_errors=True)
        return False, f"uv install failed: {e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _download_via_qgis(url, dest_path, progress_callback):
    """Download using QgsBlockingNetworkRequest (respects QGIS proxy)."""
    try:
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
    except ImportError:
        return _download_via_urllib(url, dest_path, progress_callback)

    if progress_callback:
        progress_callback(5, "Connecting…")

    request = QgsBlockingNetworkRequest()
    err = request.get(QNetworkRequest(QUrl(url)))

    if err != QgsBlockingNetworkRequest.NoError:
        emsg = request.errorMessage()
        return False, f"Download failed: {emsg}"

    content = request.reply().content()
    if progress_callback:
        mb = len(content) / (1024 * 1024)
        progress_callback(50, f"Downloaded {mb:.1f} MB")

    with open(dest_path, "wb") as f:
        f.write(content.data())
    return True, ""


def _download_via_urllib(url, dest_path, progress_callback):
    """Fallback download with stdlib urllib."""
    import urllib.request
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        return False, f"Download failed: unsupported URL scheme '{scheme}'"

    if progress_callback:
        progress_callback(5, "Downloading (urllib)…")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as f:  # nosec B310  # noqa: S310
            shutil.copyfileobj(resp, f)
    except Exception as e:
        return False, f"Download failed: {e}"
    return True, ""


def verify_uv():
    p = get_uv_path()
    if not os.path.isfile(p):
        return False, f"uv not found at {p}"
    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        kwargs = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
        r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=30, env=env, **kwargs)
        if r.returncode == 0:
            return True, r.stdout.strip()
        return False, r.stderr[:100] if r.stderr else "unknown error"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:100]


def remove_uv():
    d = _uv_dir()
    if not os.path.exists(d):
        return True, "uv not installed"
    try:
        shutil.rmtree(d)
        return True, "uv removed"
    except Exception as e:
        return False, f"removal failed: {e}"
