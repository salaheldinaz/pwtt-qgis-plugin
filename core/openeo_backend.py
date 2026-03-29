# -*- coding: utf-8 -*-
"""openEO / CDSE backend: server-side PWTT, download result GeoTIFF."""

import os
import shutil
from datetime import datetime
from typing import Any, Optional
from .base_backend import PWTTBackend
from .utils import wkt_to_bbox


def download_job_geotiff(results: Any, out_path: str, scratch_dir: str) -> str:
    """
    Download GeoTIFF batch-job output to ``out_path``.

    The openeo client's ``download_file()`` raises when several STAC assets are
    published (e.g. GeoTIFF plus JSON metadata). We select image/tiff assets
    explicitly and, if there are several rasters, keep the largest file.
    """
    assets = results.get_assets()

    def _is_geotiff(a) -> bool:
        t = (a.metadata.get("type") or "").lower()
        if t.startswith("image/tiff"):
            return True
        n = a.name.lower()
        return n.endswith(".tif") or n.endswith(".tiff")

    gt = [a for a in assets if _is_geotiff(a)]
    if not gt:
        names = [a.name for a in assets]
        raise RuntimeError(f"No GeoTIFF in job results. Assets: {names}")

    if len(gt) == 1:
        gt[0].download(out_path)
        return out_path

    os.makedirs(scratch_dir, exist_ok=True)
    paths = [a.download(scratch_dir) for a in gt]
    chosen = max(paths, key=lambda p: p.stat().st_size)
    shutil.copy2(chosen, out_path)
    return out_path


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

            verify_ssl = credentials.get("verify_ssl", True)
            if verify_ssl is None:
                verify_ssl = True
            session = None
            if not verify_ssl:
                import urllib3
                import requests

                session = requests.Session()
                session.verify = False
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            connect_kw = {"session": session} if session is not None else {}
            self._conn = openeo.connect(
                "https://openeo.dataspace.copernicus.eu",
                **connect_kw,
            )
            client_id = credentials.get("client_id")
            client_secret = credentials.get("client_secret")
            if client_id and client_secret:
                self._conn.authenticate_oidc_client_credentials(
                    client_id=client_id, client_secret=client_secret
                )
            else:
                self._conn.authenticate_oidc()
            return True
        except Exception as e:
            raise RuntimeError(f"openEO authentication failed: {e}") from e

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
        # If we already have an openEO job id, resume polling it instead of
        # creating a brand-new batch job.
        if remote_job_id:
            return self._poll_and_download(
                remote_job_id, output_path, progress_callback
            )

        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
        spatial_extent = {"west": west, "south": south, "east": east, "north": north, "crs": "EPSG:4326"}

        war_d = datetime.strptime(war_start, "%Y-%m-%d")
        inf_d = datetime.strptime(inference_start, "%Y-%m-%d")
        pre_start = _add_months(war_d, -pre_interval).strftime("%Y-%m-%d")
        post_end = _add_months(inf_d, post_interval).strftime("%Y-%m-%d")

        # Initialise run_metadata
        self.run_metadata = {
            "collection": "SENTINEL1_GRD",
            "bands": ["VV", "VH"],
            "sar_backscatter": "sigma0-ellipsoid",
            "pre_period": {"start": pre_start, "end": war_start},
            "post_period": {"start": inference_start, "end": post_end},
            "bbox": [west, south, east, north],
            "processing": "openEO CDSE server-side",
            "job_logs": [],
        }

        if progress_callback:
            progress_callback(5, f"Loading pre-war collection ({pre_start} to {war_start})…")
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
            progress_callback(15, f"Loading post-war collection ({inference_start} to {post_end})…")
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
            progress_callback(25, "Computing change detection (abs diff, band max)…")
        diff = (post - pre).apply(lambda x: x.absolute())
        result = diff.reduce_dimension(dimension="bands", reducer="max")

        if progress_callback:
            progress_callback(30, "Creating batch job on openEO…")
        job = result.create_job(out_format="GTiff", job_options={"driver-memory": "2G"})

        # Store the remote job id so the caller can persist it
        self.remote_job_id = job.job_id

        if progress_callback:
            progress_callback(32, f"Batch job created: {job.job_id}")

        # Log initial job metadata
        self._log_job_describe(job, progress_callback)

        if progress_callback:
            progress_callback(35, f"Starting batch job {job.job_id}…")
        job.start()

        return self._poll_and_download(job.job_id, output_path, progress_callback)

    def _poll_and_download(self, job_id, output_path, progress_callback=None):
        """Poll an existing openEO batch job until it finishes, then download."""
        import time

        self.remote_job_id = job_id
        if self.run_metadata is None:
            self.run_metadata = {"processing": "openEO CDSE server-side (resumed)"}
        job = self._conn.job(job_id)

        # Get full job metadata via describe()
        self._log_job_describe(job, progress_callback)

        status = job.status()
        if progress_callback:
            progress_callback(36, f"Current status: {status}")

        if status == "finished":
            if progress_callback:
                progress_callback(80, f"Batch job {job_id} already finished. Downloading…")
            return self._download_results(job, output_path, progress_callback)

        if status == "created":
            if progress_callback:
                progress_callback(35, f"Starting batch job {job_id}…")
            job.start()

        if status == "error":
            self._log_job_errors(job, progress_callback)
            raise RuntimeError(self._job_error_msg(job) or "openEO batch job failed.")
        if status in ("canceled", "cancelled"):
            raise RuntimeError("openEO batch job was cancelled.")

        # Poll until the server-side job completes
        poll_wait = 10
        poll_count = 0
        while True:
            time.sleep(poll_wait)
            poll_count += 1

            try:
                info = job.describe()
            except Exception as e:
                if progress_callback:
                    progress_callback(40, f"Connection error (will retry): {e}")
                poll_wait = min(poll_wait + 5, 30)
                continue

            status = info.get("status", "unknown")
            progress = info.get("progress")

            # Build a detailed status line
            parts = [f"Batch job {job_id}: {status}"]
            if progress is not None:
                parts.append(f"progress {progress}%")
            msg = " — ".join(parts)

            if status == "finished":
                if progress_callback:
                    progress_callback(75, msg)
                break
            if status == "error":
                if progress_callback:
                    progress_callback(40, msg)
                self._log_job_errors(job, progress_callback)
                raise RuntimeError(self._job_error_msg(job) or "openEO batch job failed.")
            if status in ("canceled", "cancelled"):
                raise RuntimeError("openEO batch job was cancelled.")

            if progress_callback:
                # Map openEO progress to our 35-75 range
                if progress is not None:
                    mapped = 35 + int(float(progress) * 0.4)
                else:
                    mapped = 40
                progress_callback(mapped, msg)

            # Periodically fetch and show server-side logs
            if poll_count % 3 == 0:
                self._log_recent(job, progress_callback)

            poll_wait = min(poll_wait + 5, 30)

        return self._download_results(job, output_path, progress_callback)

    def _download_results(self, job, output_path, progress_callback=None):
        """Download results and log metadata."""
        job_id = job.job_id

        if progress_callback:
            progress_callback(78, "Fetching result metadata…")

        result_meta = {}
        try:
            results = job.get_results()
            meta = results.get_metadata()
            result_meta = meta
            bbox = meta.get("bbox")
            assets = meta.get("assets", {})
            asset_names = list(assets.keys())
            if progress_callback:
                parts = [f"Result: {len(asset_names)} asset(s)"]
                if bbox:
                    parts.append(f"bbox={bbox}")
                if asset_names:
                    parts.append(f"files: {', '.join(asset_names)}")
                progress_callback(80, " — ".join(parts))
        except Exception as e:
            if progress_callback:
                progress_callback(80, f"Could not fetch result metadata: {e}")
            results = job.get_results()

        if progress_callback:
            progress_callback(82, f"Downloading result to {output_path}…")
        scratch = os.path.dirname(output_path) or "."
        download_job_geotiff(results, output_path, scratch)

        size_mb = os.path.getsize(output_path) / (1024 * 1024) if os.path.isfile(output_path) else 0
        if progress_callback:
            progress_callback(95, f"Download complete ({size_mb:.1f} MB). Job {job_id} done.")

        # Collect run_metadata from job describe, logs, and result metadata
        self._collect_run_metadata(job, result_meta, output_path)

        return output_path

    def _collect_run_metadata(self, job, result_meta, output_path):
        """Gather processing details into run_metadata after job completion."""
        import os
        if self.run_metadata is None:
            self.run_metadata = {}

        # Job-level info from describe()
        try:
            info = job.describe()
            self.run_metadata["remote_job_id"] = info.get("id")
            self.run_metadata["job_status"] = info.get("status")
            self.run_metadata["job_created"] = info.get("created")
            self.run_metadata["job_updated"] = info.get("updated")
            if info.get("usage"):
                self.run_metadata["usage"] = info["usage"]
            if info.get("costs") is not None:
                self.run_metadata["costs"] = info["costs"]
        except Exception:
            pass

        # Result metadata (bbox, assets)
        if result_meta:
            if result_meta.get("bbox"):
                self.run_metadata["result_bbox"] = result_meta["bbox"]
            assets = result_meta.get("assets", {})
            if assets:
                self.run_metadata["result_assets"] = {
                    name: {
                        "type": a.get("type", ""),
                        "href": a.get("href", ""),
                    }
                    for name, a in assets.items()
                }

        # Output file size
        try:
            if os.path.isfile(output_path):
                self.run_metadata["output_size_bytes"] = os.path.getsize(output_path)
        except OSError:
            pass

        # Extract scene/data info from server logs
        try:
            logs = job.logs()
            entries = list(logs) if logs else []
            log_messages = []
            for entry in entries:
                if isinstance(entry, dict):
                    msg = entry.get("message", "")
                    lvl = entry.get("level", "info")
                else:
                    msg = str(entry)
                    lvl = "info"
                if msg:
                    log_messages.append({"level": lvl, "message": msg})
            self.run_metadata["job_logs"] = log_messages
        except Exception:
            pass

    def _log_job_describe(self, job, progress_callback=None):
        """Fetch and log full job metadata from describe()."""
        if not progress_callback:
            return
        try:
            info = job.describe()
            parts = []
            for key in ("id", "status", "created", "updated", "title", "progress"):
                val = info.get(key)
                if val is not None and val != "":
                    parts.append(f"{key}={val}")
            # Show usage/costs if available
            usage = info.get("usage")
            if usage:
                for k, v in usage.items():
                    parts.append(f"{k}={v}")
            costs = info.get("costs")
            if costs is not None:
                parts.append(f"costs={costs}")
            if parts:
                progress_callback(0, f"Job info: {', '.join(parts)}")
        except Exception:
            pass

    def _log_job_errors(self, job, progress_callback=None):
        """Fetch and log error-level entries from job logs."""
        if not progress_callback:
            return
        try:
            logs = job.logs(level="error")
            for entry in logs:
                msg = entry.get("message", "") if isinstance(entry, dict) else str(entry)
                if msg:
                    progress_callback(0, f"[openEO error] {msg}")
        except Exception:
            pass

    def _log_recent(self, job, progress_callback=None):
        """Fetch recent info/warning/error log entries from the server."""
        if not progress_callback:
            return
        try:
            logs = job.logs(level="info")
            # Show last few entries
            entries = list(logs)[-5:] if logs else []
            for entry in entries:
                if isinstance(entry, dict):
                    lvl = entry.get("level", "info")
                    msg = entry.get("message", "")
                else:
                    lvl = "info"
                    msg = str(entry)
                if msg:
                    progress_callback(0, f"[openEO {lvl}] {msg}")
        except Exception:
            pass

    @staticmethod
    def _job_error_msg(job):
        try:
            logs = job.logs(level="error")
            messages = []
            for e in (list(logs) or [])[-5:]:
                msg = e.get("message", "") if isinstance(e, dict) else str(e)
                if msg:
                    messages.append(msg)
            return "; ".join(messages)
        except Exception:
            return ""
