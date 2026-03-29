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
        try:
            import ee
            return True, ""
        except ImportError:
            return False, "GEE backend requires the 'earthengine-api' package. Install with: pip install earthengine-api"

    def authenticate(self, credentials: dict) -> bool:
        import ee
        project = (credentials.get("project") or "").strip()
        try:
            if not getattr(ee.data, "_credentials", None):
                ee.Authenticate(auth_mode="localhost")
            ee.Initialize(project=project if project else None)
            return True
        except Exception as e:
            raise RuntimeError(f"GEE authentication failed: {e}") from e

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
            self._viz_threshold = damage_threshold

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
