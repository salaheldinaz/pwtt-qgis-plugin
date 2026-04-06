# -*- coding: utf-8 -*-
"""Backend/auth helper functions extracted from main_dialog."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsSettings

import threading
import time

AUTH_TIMEOUT_SEC = 300  # 5 min — GEE uses a browser OIDC flow; openEO uses client credentials


def save_openeo_credentials_to_settings(client_id, client_secret, verify_ssl):
    """Persist openEO OAuth client-credentials to QgsSettings (PWTT group)."""
    s = QgsSettings()
    s.beginGroup("PWTT")
    cid = (client_id or "").strip() if isinstance(client_id, str) else ""
    csec = (client_secret or "").strip() if isinstance(client_secret, str) else ""
    s.setValue("openeo_client_id", cid)
    s.setValue("openeo_client_secret", csec)
    if verify_ssl is None:
        verify_ssl = True
    s.setValue("openeo_verify_ssl", bool(verify_ssl))
    s.endGroup()


def _run_with_timeout(fn, timeout_sec, cancel_event=None):
    """Run *fn()* in a daemon thread with timeout and cancellation.

    Returns the result of fn(). Raises RuntimeError on timeout or cancel.
    Re-raises any exception thrown by fn().
    """
    result = [None]
    error = [None]

    def _target():
        try:
            result[0] = fn()
        except Exception as exc:
            error[0] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    deadline = time.monotonic() + timeout_sec
    while t.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Authentication timed out after {timeout_sec}s. "
                "The server may be unreachable or the operation took too long."
            )
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Authentication cancelled.")
        t.join(timeout=min(1.0, remaining))

    if error[0] is not None:
        raise error[0]
    return result[0]


def is_message_box_yes(reply):
    """Reliable Yes detection across PyQt5/6."""
    try:
        return (int(reply) & int(QMessageBox.Yes)) != 0
    except (TypeError, ValueError):
        return reply == QMessageBox.Yes


def confirm_local_processing_storage(parent):
    """Disk-space warning before a local (download-heavy) run. True = user chose Continue."""
    reply = QMessageBox.question(
        parent,
        "PWTT",
        "Local processing downloads Sentinel-1 GRD products to your computer. "
        "That uses a lot of disk space for raw downloads, cache, and outputs "
        "(often multiple GB per run, depending on AOI and date ranges).\n\n"
        "Continue?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return is_message_box_yes(reply)


def ensure_footprint_dependencies(parent):
    """Prompt to install footprint packages if needed. Return True if ready."""
    from ..core import deps

    fp_missing, fp_pip = deps.footprint_missing()
    if not fp_missing:
        return True
    if fp_pip:
        reply = QMessageBox.question(
            parent,
            "PWTT",
            f"Building footprints require: {', '.join(fp_pip)}\n\nInstall now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if is_message_box_yes(reply):
            if not deps.install_with_dialog(fp_pip, parent=parent):
                return False
            fp_missing, fp_pip = deps.footprint_missing()
    if fp_missing:
        detail = ""
        if "rasterstats" in fp_missing:
            detail = deps.rasterstats_failure_detail()
        qgis_only = [n for n in fp_missing if n not in (fp_pip or [])]
        msg = f"Cannot compute footprints: missing {', '.join(fp_missing)}."
        if qgis_only:
            msg += (
                f"\n{', '.join(qgis_only)} should be provided by QGIS — "
                f"check your QGIS installation."
            )
        else:
            msg += "\nInstall the packages or skip this step."
        if detail:
            msg += f"\n\n{detail}"
        QMessageBox.warning(parent, "PWTT", msg)
        return False
    return True


def get_backend_class(backend_id):
    try:
        if backend_id == "openeo":
            from ..core.openeo_backend import OpenEOBackend

            return OpenEOBackend
        if backend_id == "gee":
            from ..core.gee_backend import GEEBackend

            return GEEBackend
        if backend_id == "local":
            from ..core.local_backend import LocalBackend

            return LocalBackend
    except Exception:
        return None
    return None


def auth_with_progress(backend, credentials, backend_id, parent=None):
    """Run backend.authenticate() in QThread + dialog, raise on failure/cancel."""
    import webbrowser as _wb
    from qgis.PyQt.QtCore import QThread, pyqtSignal
    from qgis.PyQt.QtWidgets import (
        QDialog,
        QVBoxLayout,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QProgressDialog,
        QApplication,
    )

    is_oidc = (backend_id == "gee")

    class _Worker(QThread):
        auth_url_ready = pyqtSignal(str)

        def __init__(self, b, c):
            super().__init__()
            self.b = b
            self.c = c
            self.ok = False
            self.error_msg = ""
            self._cancel_event = threading.Event()

        def cancel(self):
            self._cancel_event.set()

        def run(self):
            try:
                self.ok = _run_with_timeout(
                    lambda: self.b.authenticate(self.c),
                    AUTH_TIMEOUT_SEC,
                    self._cancel_event,
                )
                if not self.ok:
                    self.error_msg = "Authentication failed. Check your credentials."
            except Exception as e:
                self.ok = False
                self.error_msg = str(e)

    worker = _Worker(backend, credentials)
    canceled = [False]

    if is_oidc and parent is not None:
        _backend_label = {
            "gee": ("Google Earth Engine Sign In", "Connecting to Google Earth Engine…"),
        }
        _title, _connecting = _backend_label.get(backend_id, ("Sign In", "Connecting…"))
        dlg = QDialog(parent)
        dlg.setWindowTitle(f"PWTT — {_title}")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumWidth(440)
        layout = QVBoxLayout(dlg)

        status_lbl = QLabel(_connecting)
        status_lbl.setWordWrap(True)
        layout.addWidget(status_lbl)

        url_lbl = QLabel()
        url_lbl.setWordWrap(True)
        url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_lbl.hide()
        layout.addWidget(url_lbl)

        url_btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy URL")
        copy_btn.setEnabled(False)
        open_btn = QPushButton("Open in Browser")
        open_btn.setEnabled(False)
        url_btn_row.addWidget(copy_btn)
        url_btn_row.addWidget(open_btn)
        layout.addLayout(url_btn_row)

        cancel_btn = QPushButton("Cancel")
        layout.addWidget(cancel_btn)

        _orig_open = _wb.open
        _orig_open_new = _wb.open_new
        _orig_open_tab = _wb.open_new_tab
        _orig_get = _wb.get

        detected_url = [None]

        def _on_url_ready(url):
            detected_url[0] = url
            url_lbl.setText(url)
            url_lbl.show()
            copy_btn.setEnabled(True)
            open_btn.setEnabled(True)
            status_lbl.setText("Visit the URL below and approve sign-in, then wait here:")
            dlg.adjustSize()

        def _on_copy():
            if detected_url[0]:
                QApplication.clipboard().setText(detected_url[0])

        def _on_open():
            if detected_url[0]:
                _orig_open(detected_url[0])

        def _on_cancel():
            canceled[0] = True
            worker.cancel()
            try:
                worker.finished.disconnect()
            except Exception:
                pass
            dlg.reject()

        worker.auth_url_ready.connect(_on_url_ready)
        worker.finished.connect(dlg.accept, Qt.QueuedConnection)
        copy_btn.clicked.connect(_on_copy)
        open_btn.clicked.connect(_on_open)
        cancel_btn.clicked.connect(_on_cancel)

        def _intercept(url, *a, **kw):
            if url:
                worker.auth_url_ready.emit(url)
            return True

        class _DummyBrowser:
            name = "pwtt-interceptor"

        _wb.open = _intercept
        _wb.open_new = _intercept
        _wb.open_new_tab = _intercept
        _wb.get = lambda *a, **kw: _DummyBrowser()
        try:
            worker.start()
            dlg.exec_()
        finally:
            # Restore each function individually so a failure in one
            # doesn't prevent the others from being restored.
            try:
                _wb.open = _orig_open
            except Exception:
                pass
            try:
                _wb.open_new = _orig_open_new
            except Exception:
                pass
            try:
                _wb.open_new_tab = _orig_open_tab
            except Exception:
                pass
            try:
                _wb.get = _orig_get
            except Exception:
                pass

        if canceled[0]:
            worker.wait(5000)
            raise RuntimeError("Authentication cancelled.")
        worker.wait()
    else:
        dlg = QProgressDialog("Authenticating…", "Cancel", 0, 0, parent)
        dlg.setWindowTitle("PWTT")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        prog_cancel_clicked = [False]

        def _on_progress_dialog_cancel():
            prog_cancel_clicked[0] = True
            worker.cancel()

        dlg.canceled.connect(_on_progress_dialog_cancel)

        def _dismiss_auth_progress():
            try:
                dlg.canceled.disconnect()
            except Exception:
                pass
            dlg.close()

        worker.finished.connect(_dismiss_auth_progress, Qt.QueuedConnection)
        worker.start()
        dlg.exec_()

        if prog_cancel_clicked[0]:
            worker.wait(5000)
            raise RuntimeError("Authentication cancelled.")
        worker.wait()

    if not worker.ok:
        raise RuntimeError(worker.error_msg or "Authentication failed. Check your credentials.")


def merge_openeo_creds_from_controls_dock(creds, controls_dock):
    if controls_dock is None or not hasattr(controls_dock, "_get_credentials"):
        return creds
    try:
        ui = controls_dock._get_credentials("openeo")
    except Exception:
        return creds
    if ui.get("client_id") and ui.get("client_secret"):
        out = dict(creds)
        out["client_id"] = ui["client_id"]
        out["client_secret"] = ui["client_secret"]
        if "verify_ssl" in ui:
            out["verify_ssl"] = ui["verify_ssl"]
        return out
    return creds


def merge_local_creds_from_controls_dock(creds, controls_dock):
    if controls_dock is None or not hasattr(controls_dock, "_get_credentials"):
        return creds
    try:
        ui = controls_dock._get_credentials("local")
    except Exception:
        return creds
    out = dict(creds)
    if ui.get("source"):
        out["source"] = ui["source"]
    u = (ui.get("username") or "").strip()
    p = ui.get("password") or ""
    if u:
        out["username"] = u
    if p:
        out["password"] = p
    eu = (ui.get("earthdata_username") or "").strip()
    ep = ui.get("earthdata_password") or ""
    if eu:
        out["earthdata_username"] = eu
    if ep:
        out["earthdata_password"] = ep
    pk = (ui.get("pc_subscription_key") or "").strip()
    if pk:
        out["pc_subscription_key"] = pk
    return out


def create_and_auth_backend(
    backend_id,
    parent=None,
    controls_dock=None,
    local_data_source=None,
):
    BackendClass = get_backend_class(backend_id)
    if not BackendClass:
        raise RuntimeError(f"Backend '{backend_id}' is not available.")
    backend = BackendClass()

    if backend_id == "local":
        from ..core import deps

        s0 = QgsSettings()
        s0.beginGroup("PWTT")
        src0 = (s0.value("local_data_source", "cdse") or "cdse").strip().lower()
        s0.endGroup()
        eff_src = local_data_source if local_data_source in ("cdse", "asf", "pc") else src0
        miss, pip_hint = deps.local_backend_missing(eff_src)
        if miss:
            pip_msg = f" pip install {' '.join(pip_hint)}" if pip_hint else ""
            raise RuntimeError(
                f"Local backend ({eff_src}) missing: {', '.join(miss)}."
                f"{pip_msg} (or use Install dependencies in the panel.)"
            )
    else:
        ok, msg = backend.check_dependencies()
        if not ok:
            raise RuntimeError(msg)

    s = QgsSettings()
    s.beginGroup("PWTT")
    if backend_id == "openeo":
        creds = {
            "client_id": s.value("openeo_client_id", "") or None,
            "client_secret": s.value("openeo_client_secret", "") or None,
            "verify_ssl": s.value("openeo_verify_ssl", True, type=bool),
        }
    elif backend_id == "gee":
        creds = {"project": s.value("gee_project", "")}
    elif backend_id == "local":
        creds = {
            "source": (s.value("local_data_source", "cdse") or "cdse"),
            "username": s.value("cdse_username", ""),
            "password": s.value("cdse_password", ""),
            "earthdata_username": s.value("earthdata_username", ""),
            "earthdata_password": s.value("earthdata_password", ""),
            "pc_subscription_key": s.value("pc_subscription_key", ""),
        }
    else:
        creds = {}
    s.endGroup()

    if backend_id == "openeo":
        creds = merge_openeo_creds_from_controls_dock(creds, controls_dock)
        if not (creds.get("client_id") and creds.get("client_secret")):
            raise RuntimeError(
                "Client ID and Client Secret are required for openEO.\n"
                "Create OAuth2 credentials at the Copernicus Data Space dashboard:\n"
                "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings"
            )
        # SSL bypass: require explicit user confirmation before proceeding
        if not creds.get("verify_ssl", True):
            reply = QMessageBox.warning(
                parent,
                "PWTT — Security Warning",
                "TLS certificate verification is DISABLED.\n\n"
                "This makes your connection vulnerable to interception. "
                "Only disable this if you understand the risk (e.g. a corporate proxy "
                "with a custom CA certificate).\n\n"
                "Proceed without TLS verification?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if not is_message_box_yes(reply):
                raise RuntimeError("Authentication cancelled.")
            creds["_ssl_bypass_confirmed"] = True
    elif backend_id == "local":
        creds = merge_local_creds_from_controls_dock(creds, controls_dock)
        if local_data_source in ("cdse", "asf", "pc"):
            creds["source"] = local_data_source

    if parent:
        auth_with_progress(backend, creds, backend_id, parent)
    else:
        try:
            if not backend.authenticate(creds):
                raise RuntimeError("Authentication failed. Check your credentials.")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(str(e)) from e
    if backend_id == "openeo":
        save_openeo_credentials_to_settings(
            creds.get("client_id") or "",
            creds.get("client_secret") or "",
            creds.get("verify_ssl", True),
        )
        if controls_dock is not None and hasattr(
            controls_dock, "_sync_openeo_widgets_from_settings"
        ):
            controls_dock._sync_openeo_widgets_from_settings()
    return backend
