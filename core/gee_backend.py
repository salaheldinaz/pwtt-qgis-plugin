# -*- coding: utf-8 -*-
"""Google Earth Engine backend: uses bundled gee_pwtt (detect_damage), downloads via getDownloadURL (streamed)."""

from __future__ import annotations

import math
import requests
from typing import Optional, Tuple
from .base_backend import PWTTBackend
from .utils import wkt_to_bbox

# Earth Engine getDownloadURL / thumbnail pipeline cap (bytes), per API error text.
GEE_GETDOWNLOAD_MAX_BYTES = 50331648
# EE's "Total request size" is often ~10–15% above a naive WGS84 bbox × scale pixel
# count (projection, alignment). All client-side fit checks use this effective budget.
GEE_GETDOWNLOAD_SIZE_HEADROOM = 1.15
GEE_GETDOWNLOAD_EFFECTIVE_MAX_BYTES = int(
    GEE_GETDOWNLOAD_MAX_BYTES / GEE_GETDOWNLOAD_SIZE_HEADROOM
)
# Must match getDownloadURL params in run().
_GEE_DOWNLOAD_SCALE_M = 10
_GEE_DOWNLOAD_BANDS = 3
_GEE_DOWNLOAD_BYTES_PER_BAND = 4  # Float32


def estimate_gee_getdownload_request_bytes(
    west: float,
    south: float,
    east: float,
    north: float,
    scale_m: float = _GEE_DOWNLOAD_SCALE_M,
    num_bands: int = _GEE_DOWNLOAD_BANDS,
    bytes_per_band: int = _GEE_DOWNLOAD_BYTES_PER_BAND,
) -> int:
    """Approximate uncompressed raster size for our GEE export (bbox rectangle, EPSG:4326 → m)."""
    lat_mid = (south + north) / 2.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat_mid))
    m_per_deg_lat = 111_320.0
    width_m = max(0.0, (east - west)) * m_per_deg_lon
    height_m = max(0.0, (north - south)) * m_per_deg_lat
    cols = max(1, math.ceil(width_m / scale_m))
    rows = max(1, math.ceil(height_m / scale_m))
    return cols * rows * num_bands * bytes_per_band


def gee_precheck_getdownload_url(aoi_wkt: str) -> Tuple[bool, str]:
    """Return (True, "") if the AOI bbox is within EE direct-download limits, else (False, user message)."""
    bbox = wkt_to_bbox((aoi_wkt or "").strip())
    if not bbox:
        return True, ""
    west, south, east, north = bbox
    est = estimate_gee_getdownload_request_bytes(west, south, east, north)
    if est <= GEE_GETDOWNLOAD_EFFECTIVE_MAX_BYTES:
        return True, ""
    lim_mb = GEE_GETDOWNLOAD_MAX_BYTES / (1024 * 1024)
    est_mb = est / (1024 * 1024)
    msg = (
        f"The area of interest is too large for Google Earth Engine's direct GeoTIFF "
        f"download (about {est_mb:.0f} MiB estimated vs {lim_mb:.0f} MiB max).\n\n"
        f"GEE exports this result at {_GEE_DOWNLOAD_SCALE_M} m with {_GEE_DOWNLOAD_BANDS} float bands. "
        f"Shrink the AOI or use another backend (e.g. Local or openEO)."
    )
    return False, msg


class GEEBackend(PWTTBackend):
    @property
    def name(self):
        return "Google Earth Engine"

    @property
    def id(self):
        return "gee"

    def check_dependencies(self):
        from . import deps
        deps.ensure_on_path()
        missing, pip = deps.backend_missing("gee")
        if missing:
            return False, f"GEE backend requires: pip install {' '.join(pip)}"
        return True, ""

    @staticmethod
    def _resolve_project(project: str) -> str:
        """Return an explicit project ID, falling back to the EE CLI default."""
        if project:
            return project
        try:
            import json
            from pathlib import Path
            props_path = Path.home() / ".config" / "earthengine" / "properties"
            if props_path.is_file():
                with open(props_path) as f:
                    props = json.load(f)
                saved = (props.get("project") or "").strip()
                if saved:
                    return saved
        except Exception:
            pass
        return ""

    @staticmethod
    def _gee_saved_oauth_matches_client(client_id: str) -> bool:
        """True if ~/.config/earthengine/credentials has a refresh token for this client."""
        import json
        from pathlib import Path
        from ee import oauth as _ee_oauth

        path = Path(_ee_oauth.get_credentials_path())
        if not path.is_file():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        if not (data.get("refresh_token") or "").strip():
            return False
        stored = (data.get("client_id") or "").strip()
        return bool(stored) and stored == client_id.strip()

    @staticmethod
    def _gee_needs_interactive_authenticate(ee) -> bool:
        """earthengine-api no longer exposes ee.data._credentials; use the public API."""
        try:
            ee.data.get_persistent_credentials()
            return False
        except Exception:
            return True

    def authenticate(self, credentials: dict) -> bool:
        import builtins
        import socket as _socket
        import ee
        project = self._resolve_project(
            (credentials.get("project") or "").strip()
        )
        client_id = (credentials.get("client_id") or "").strip()
        client_secret = (credentials.get("client_secret") or "").strip()
        try:
            # ── Preferred: OAuth 2.0 with user's own GCP client credentials ──
            if client_id and client_secret:
                if (
                    self._gee_saved_oauth_matches_client(client_id)
                    and not self._gee_needs_interactive_authenticate(ee)
                ):
                    self._ee_init(ee, project)
                    return True
                self._oauth_with_client_credentials(
                    project, client_id, client_secret
                )
                self._ee_init(ee, project)
                return True

            # ── Fallback: EE default browser OAuth ──
            if self._gee_needs_interactive_authenticate(ee):
                _orig_input = builtins.input
                # Some QGIS plugins call socket.setdefaulttimeout(), which
                # causes the OAuth callback HTTP server to time out before the
                # user can approve the request.  Reset to None (blocking) for
                # the duration of the auth flow.
                _saved_timeout = _socket.getdefaulttimeout()
                _socket.setdefaulttimeout(None)
                builtins.input = lambda *a, **kw: (_ for _ in ()).throw(
                    RuntimeError(
                        "Browser authentication did not complete. "
                        "Ensure your browser opened and you approved the request."
                    )
                )
                try:
                    ee.Authenticate(auth_mode="localhost")
                except OSError as _oe:
                    if _oe.errno == 48 or "address already in use" in str(_oe).lower():
                        raise RuntimeError(
                            "The local OAuth callback port is still in use from a "
                            "previous attempt. Please wait a few seconds and try again."
                        ) from _oe
                    raise
                finally:
                    builtins.input = _orig_input
                    _socket.setdefaulttimeout(_saved_timeout)
            self._ee_init(ee, project)
            return True
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"GEE authentication failed: {e}") from e

    @staticmethod
    def _ee_init(ee, project, **kwargs):
        """Wrapper around ee.Initialize that gives a clear error when project is missing."""
        try:
            ee.Initialize(project=project if project else None, **kwargs)
        except Exception as e:
            if "no project found" in str(e).lower():
                raise RuntimeError(
                    "A Google Cloud project is required.\n\n"
                    "Set the 'Project' field in the GEE credentials panel "
                    "(e.g. 'my-gcp-project'), or run:\n"
                    "  earthengine set_project YOUR_PROJECT\n\n"
                    "Your project must have the Earth Engine API enabled:\n"
                    "https://console.cloud.google.com/apis/library/"
                    "earthengine.googleapis.com"
                ) from e
            raise

    def _oauth_with_client_credentials(
        self, project: str, client_id: str, client_secret: str
    ) -> None:
        """Run the OAuth 2.0 installed-app flow with the user's own GCP client
        credentials, saving the resulting refresh token so ee.Initialize() can
        use it on this and future runs.

        Credentials are created at:
        https://console.cloud.google.com/apis/credentials
        (Create credentials → OAuth client ID → Desktop app)
        """
        import base64
        import hashlib
        import os
        import socket as _socket
        import urllib.parse
        import webbrowser
        from ee import oauth as _ee_oauth

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

        code_verifier = _b64(os.urandom(32))
        code_challenge = _b64(hashlib.sha256(code_verifier.encode()).digest())

        # Reset any global socket timeout so the callback server blocks
        # properly while waiting for the browser redirect.
        _saved_timeout = _socket.getdefaulttimeout()
        _socket.setdefaulttimeout(None)
        try:
            # Try the default port first; if it's still in use from a previous
            # attempt, find the next available port.
            port = _ee_oauth.DEFAULT_LOCAL_PORT
            for _attempt in range(10):
                try:
                    server = _ee_oauth._start_server(port)
                    break
                except OSError:
                    port += 1
            else:
                raise RuntimeError(
                    "Could not bind to a local port for OAuth callback. "
                    "Please wait a moment and try again."
                )
            auth_url = (
                "https://accounts.google.com/o/oauth2/auth?"
                + urllib.parse.urlencode({
                    "client_id": client_id,
                    "scope": " ".join(_ee_oauth.SCOPES),
                    "redirect_uri": server.url,
                    "response_type": "code",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                })
            )
            # webbrowser.open is intercepted by the auth dialog to show the URL
            # to the user (with Copy / Open in Browser buttons).
            webbrowser.open(auth_url)
            auth_code = server.fetch_code()
        finally:
            _socket.setdefaulttimeout(_saved_timeout)

        if not auth_code:
            raise RuntimeError(
                "Browser authentication did not complete. "
                "Ensure your browser opened and you approved the request."
            )
        refresh_token = _ee_oauth.request_token(
            auth_code, code_verifier,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=server.url,
        )
        _ee_oauth.write_private_json(
            _ee_oauth.get_credentials_path(),
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": list(_ee_oauth.SCOPES),
            },
        )

    def run(
        self,
        aoi_wkt: str,
        war_start: str,
        inference_start: str,
        pre_interval: int,
        post_interval: int,
        output_path: str,
        progress_callback=None,
        include_footprints: bool = False,
        footprints_path: Optional[str] = None,
        remote_job_id: Optional[str] = None,
        damage_threshold: float = 3.3,
        gee_viz: bool = False,
        method: str = 'stouffer',
        ttest_type: str = 'welch',
        smoothing: str = 'default',
        mask_before_smooth: bool = True,
        lee_mode: str = 'per_image',
        save_timeseries: bool = True,
        job_id: Optional[str] = None,
    ) -> str:
        import ee

        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
        est_bytes = estimate_gee_getdownload_request_bytes(west, south, east, north)
        if est_bytes > GEE_GETDOWNLOAD_EFFECTIVE_MAX_BYTES:
            lim_mb = GEE_GETDOWNLOAD_MAX_BYTES / (1024 * 1024)
            est_mb = est_bytes / (1024 * 1024)
            raise RuntimeError(
                f"AOI too large for GEE direct download (~{est_mb:.0f} MiB estimated, "
                f"limit ~{lim_mb:.0f} MiB). Shrink the AOI or use Local/openEO."
            )
        aoi_geom = ee.Geometry.Rectangle([west, south, east, north])
        aoi = ee.FeatureCollection([ee.Feature(aoi_geom)])

        from . import gee_pwtt

        if progress_callback:
            progress_callback(20, "Running PWTT on Earth Engine…")
        image = gee_pwtt.detect_damage(
            aoi,
            inference_start=inference_start,
            war_start=war_start,
            pre_interval=pre_interval,
            post_interval=post_interval,
            viz=False,
            export=False,
            damage_threshold=damage_threshold,
            method=method,
            ttest_type=ttest_type,
            smoothing=smoothing,
            mask_before_smooth=mask_before_smooth,
            lee_mode=lee_mode,
        )

        # gee_viz is handled by PWTTRunTask.finished() on the main thread
        # (webbrowser.open from a worker thread fails silently on macOS).
        # Store the ee objects so the task can call open_geemap_preview later.
        if gee_viz:
            self._viz_aoi = aoi
            self._viz_image = image

        if progress_callback:
            progress_callback(60, "Requesting download URL…")
        try:
            url = image.getDownloadURL(
                {
                    "region": aoi_geom,
                    "scale": 10,
                    "format": "GEO_TIFF",
                    "bands": ["T_statistic", "damage", "p_value"],
                }
            )
        except Exception as e:
            raise RuntimeError(f"GEE getDownloadURL failed (AOI may be too large): {e}") from e

        if progress_callback:
            progress_callback(80, "Downloading…")
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        if save_timeseries:
            if progress_callback:
                progress_callback(92, "Computing per-image time series…")
            try:
                series = gee_pwtt.compute_orbit_normalized_timeseries(
                    aoi,
                    war_start=war_start,
                    inference_start=inference_start,
                    pre_interval=pre_interval,
                    post_interval=post_interval,
                    lee_mode=lee_mode,
                )
                if series:
                    from . import timeseries_sidecar
                    payload = timeseries_sidecar.build_sidecar(
                        job_id=job_id or "",
                        backend=self.id,
                        aoi_wkt=aoi_wkt,
                        war_start=war_start,
                        inference_start=inference_start,
                        pre_interval_months=pre_interval,
                        post_interval_months=post_interval,
                        normalization="per-orbit z-score vs pre-war baseline (mean/std, log-backscatter)",
                        series=series,
                    )
                    timeseries_sidecar.write_sidecars(output_path, payload)
            except Exception as ts_err:
                if progress_callback:
                    progress_callback(93, f"Time series skipped: {ts_err}")

        if progress_callback:
            progress_callback(95, "Done.")
        return output_path
