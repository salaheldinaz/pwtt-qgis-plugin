# -*- coding: utf-8 -*-
"""openEO / CDSE backend: server-side PWTT, download result GeoTIFF."""

from datetime import datetime
from typing import Optional
from .base_backend import PWTTBackend
from .utils import wkt_to_bbox


def _add_months(d: datetime, months: int) -> datetime:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return datetime(y, m, min(d.day, 28))


class OpenEOBackend(PWTTBackend):
    @property
    def name(self):
        return "openEO (recommended)"

    @property
    def id(self):
        return "openeo"

    def check_dependencies(self):
        try:
            import openeo
            return True, ""
        except ImportError:
            return False, "openEO backend requires the 'openeo' package. Install with: pip install openeo"

    def authenticate(self, credentials: dict) -> bool:
        try:
            import openeo
            self._conn = openeo.connect("https://openeo.dataspace.copernicus.eu")
            client_id = credentials.get("client_id")
            client_secret = credentials.get("client_secret")
            if client_id and client_secret:
                self._conn.authenticate_oidc(client_id=client_id, client_secret=client_secret)
            else:
                self._conn.authenticate_oidc()
            return True
        except Exception:
            return False

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
    ) -> str:
        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
        spatial_extent = {"west": west, "south": south, "east": east, "north": north, "crs": "EPSG:4326"}

        war_d = datetime.strptime(war_start, "%Y-%m-%d")
        inf_d = datetime.strptime(inference_start, "%Y-%m-%d")
        pre_start = _add_months(war_d, -pre_interval).strftime("%Y-%m-%d")
        post_end = _add_months(inf_d, post_interval).strftime("%Y-%m-%d")

        if progress_callback:
            progress_callback(5, "Loading pre-war collection…")
        pre = (
            self._conn.load_collection(
                "SENTINEL1_GRD",
                temporal_extent=[pre_start, war_start],
                spatial_extent=spatial_extent,
                bands=["VV", "VH"],
            )
            .sar_backscatter(coefficient="sigma0-ellipsoid")
            .reduce_dimension(dimension="t", reducer="mean")
        )
        if progress_callback:
            progress_callback(15, "Loading post-war collection…")
        post = (
            self._conn.load_collection(
                "SENTINEL1_GRD",
                temporal_extent=[inference_start, post_end],
                spatial_extent=spatial_extent,
                bands=["VV", "VH"],
            )
            .sar_backscatter(coefficient="sigma0-ellipsoid")
            .reduce_dimension(dimension="t", reducer="mean")
        )
        if progress_callback:
            progress_callback(25, "Computing change…")
        diff = (post - pre).abs()
        result = diff.reduce_dimension(dimension="bands", reducer="max")

        if progress_callback:
            progress_callback(50, "Creating batch job…")
        job = result.execute_batch(
            outputfile=output_path,
            format="GTiff",
            job_options={"driver-memory": "2G"},
        )
        if progress_callback:
            progress_callback(70, "Downloading result…")
        job.get_results().download_file(output_path)
        if progress_callback:
            progress_callback(95, "Done.")
        return output_path
