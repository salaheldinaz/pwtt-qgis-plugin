# -*- coding: utf-8 -*-
"""Google Earth Engine backend: uses bundled gee_pwtt (detect_damage), downloads via getDownloadURL (streamed)."""

import requests
from typing import Optional
from .base_backend import PWTTBackend
from .utils import wkt_to_bbox


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

    def authenticate(self, credentials: dict) -> bool:
        import builtins
        import socket as _socket
        import ee
        project = (credentials.get("project") or "").strip()
        client_id = (credentials.get("client_id") or "").strip()
        client_secret = (credentials.get("client_secret") or "").strip()
        api_key = (credentials.get("api_key") or "").strip()
        try:
            # ── Preferred: OAuth 2.0 with user's own GCP client credentials ──
            if client_id and client_secret:
                self._oauth_with_client_credentials(
                    project, client_id, client_secret
                )
                ee.Initialize(project=project if project else None)
                return True

            # ── Optional: Cloud API key (no browser required) ──
            if api_key:
                # credentials=None skips the 'persistent' file load so the
                # key alone authenticates requests.
                ee.Initialize(
                    credentials=None,
                    project=project if project else None,
                    cloud_api_key=api_key,
                )
                return True

            # ── Fallback: EE default browser OAuth ──
            if not getattr(ee.data, "_credentials", None):
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
                finally:
                    builtins.input = _orig_input
                    _socket.setdefaulttimeout(_saved_timeout)
            ee.Initialize(project=project if project else None)
            return True
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"GEE authentication failed: {e}") from e

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
            server = _ee_oauth._start_server(_ee_oauth.DEFAULT_LOCAL_PORT)
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
    ) -> str:
        import ee

        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
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
        if progress_callback:
            progress_callback(95, "Done.")
        return output_path
