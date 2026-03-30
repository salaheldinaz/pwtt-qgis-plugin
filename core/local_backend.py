# -*- coding: utf-8 -*-
"""Full-local backend: GRD from CDSE / ASF / Planetary Computer, Lee filter, t-test, rasterio output."""

import math
import os
import threading
from datetime import datetime
import numpy as np
from typing import Optional, Tuple

from qgis.core import QgsSettings

from .base_backend import PWTTBackend
from .downloader import get_token, search_s1_grd, download_product, find_vv_vh_in_safe
from .local_numpy_ops import (
    convolve2d_edge,
    gaussian_filter2d_edge,
    two_sided_normal_p_value,
    uniform_filter2d_edge,
)
from .utils import wkt_to_bbox

LOCAL_SOURCE_CDSE = "cdse"
LOCAL_SOURCE_ASF = "asf"
LOCAL_SOURCE_PC = "pc"

# Maximum number of Sentinel-1 scenes to download per period (pre/post).
# More scenes improve t-test accuracy but increase download time and memory.
MAX_SCENES_PER_PERIOD = 3

# Default ground-range pixel spacing for AOI warp (~Sentinel-1 GRD IW).
_AOI_RESOLUTION_M = 10.0
# Expand AOI in projected metres so edge pixels are not clipped by reprojection.
_AOI_MARGIN_M = 40.0

# Keep progress lines readable in the Jobs dock (full text still in exceptions / job log).
_PROGRESS_ERR_MAX_LEN = 200


def _is_identity_pixel_transform(t) -> bool:
    """True when *t* is the default 1×1 pixel grid (no real geotransform)."""
    return (
        abs(t.a - 1.0) < 1e-9
        and abs(t.e - 1.0) < 1e-9
        and abs(t.b) < 1e-12
        and abs(t.d) < 1e-12
        and abs(t.c) < 1e-12
        and abs(t.f) < 1e-12
    )


def _aoi_utm_grid(
    west: float,
    south: float,
    east: float,
    north: float,
    resolution_m: float = _AOI_RESOLUTION_M,
    margin_m: float = _AOI_MARGIN_M,
) -> Tuple[object, object, int, int]:
    """Build a UTM raster grid covering the WGS84 bounding box.

    Returns ``(dst_crs, dst_transform, height, width)`` suitable for warping all scenes
    to a common pixel grid (avoids full-frame loads and fixes GCP-only products).
    """
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds

    lon_c = (west + east) / 2.0
    lat_c = (south + north) / 2.0
    zone = int((lon_c + 180.0) // 6) + 1
    epsg = (32600 if lat_c >= 0 else 32700) + zone
    dst_crs = CRS.from_epsg(epsg)
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", dst_crs, west, south, east, north, densify_pts=21
    )
    left -= margin_m
    right += margin_m
    bottom -= margin_m
    top += margin_m
    width = max(1, int(math.ceil((right - left) / resolution_m)))
    height = max(1, int(math.ceil((top - bottom) / resolution_m)))
    dst_transform = from_bounds(left, bottom, right, top, width, height)
    return dst_crs, dst_transform, height, width


def _effective_src_geo(src) -> Tuple[object, object]:
    """Affine + CRS for ``rasterio.warp.reproject`` (handles GCP-only GRD COGs)."""
    from rasterio.transform import from_gcps

    if src.crs is not None and not _is_identity_pixel_transform(src.transform):
        return src.transform, src.crs
    gcps, gcrs = src.gcps
    if gcps and gcrs is not None:
        return from_gcps(gcps), gcrs
    if src.crs is not None:
        return src.transform, src.crs
    raise ValueError("Raster has no usable georeferencing (affine+CRS or GCPs).")


def _warp_band_to_aoi_grid(
    path: str,
    dst_crs,
    dst_transform,
    dst_height: int,
    dst_width: int,
    resampling,
) -> np.ndarray:
    """Warp one band onto the AOI UTM grid as ``float32`` (COG-friendly when georef is affine)."""
    import rasterio
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import reproject

    with rasterio.open(path) as src:
        if src.crs is not None and not _is_identity_pixel_transform(src.transform):
            with WarpedVRT(
                src,
                crs=dst_crs,
                transform=dst_transform,
                width=dst_width,
                height=dst_height,
                resampling=resampling,
            ) as vrt:
                return vrt.read(1).astype(np.float32, copy=False)
        src_transform, src_crs = _effective_src_geo(src)
        arr = src.read(1).astype(np.float32, copy=False)
    out = np.empty((dst_height, dst_width), dtype=np.float32)
    reproject(
        arr,
        out,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=resampling,
    )
    return out


def _short_progress_error(msg: str) -> str:
    if not msg:
        return "unknown error"
    one_line = " ".join(str(msg).split())
    if len(one_line) <= _PROGRESS_ERR_MAX_LEN:
        return one_line
    return one_line[: _PROGRESS_ERR_MAX_LEN - 1] + "…"


def _read_warp_vv_vh_pair(
    vv_path: str,
    vh_path: str,
    dst_crs,
    dst_transform,
    dst_height: int,
    dst_width: int,
    resampling,
    log,
) -> Optional[Tuple[np.ndarray, np.ndarray, dict, object, object]]:
    """Warp VV/VH to the AOI grid. Log errors and return ``None`` on failure."""
    try:
        vv = _warp_band_to_aoi_grid(
            vv_path, dst_crs, dst_transform, dst_height, dst_width, resampling
        )
        vh = _warp_band_to_aoi_grid(
            vh_path, dst_crs, dst_transform, dst_height, dst_width, resampling
        )
    except Exception as e:
        if log:
            log(f"warp VV/VH failed — {_short_progress_error(repr(e))}")
        return None
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": dst_width,
        "height": dst_height,
        "count": 1,
        "crs": dst_crs,
        "transform": dst_transform,
    }
    return vv, vh, profile, dst_transform, dst_crs


def _add_months_dt(d: datetime, months: int) -> datetime:
    """Calendar add (same rule as openEO backend: clamp day to 28)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return datetime(y, m, min(d.day, 28))


def _lee_filter(band: np.ndarray, kernel_radius: int = 1, enl: float = 5.0) -> np.ndarray:
    """Lee speckle filter (MMSE). band: 2D float. Returns filtered 2D array."""
    ksz = 2 * kernel_radius + 1
    eta = 1.0 / np.sqrt(enl)
    mean = uniform_filter2d_edge(band, ksz)
    mean_sq = uniform_filter2d_edge(band ** 2, ksz)
    var = mean_sq - mean ** 2
    var = np.maximum(var, 1e-12)
    varx = (var - (mean ** 2) * (eta ** 2)) / (1 + eta ** 2)
    b = np.clip(varx / var, 0, 1)
    return (1 - b) * np.abs(mean) + b * band


def _focal_median_gaussian(data: np.ndarray, sigma_m: float, pixel_size: float) -> np.ndarray:
    """Gaussian smoothing approximating focal median in meters. sigma_m in meters."""
    sigma_px = max(1.0, sigma_m / pixel_size)
    return gaussian_filter2d_edge(data, sigma_px)


def _circle_kernel(radius_m: float, pixel_size: float) -> np.ndarray:
    """Binary circle kernel in pixels."""
    r_px = int(np.ceil(radius_m / pixel_size))
    y, x = np.ogrid[-r_px : r_px + 1, -r_px : r_px + 1]
    return ((x * x + y * y) <= (radius_m / pixel_size) ** 2).astype(np.float64)


def _settings_local_source() -> str:
    s = QgsSettings()
    s.beginGroup("PWTT")
    src = (s.value("local_data_source", LOCAL_SOURCE_CDSE) or LOCAL_SOURCE_CDSE).strip().lower()
    s.endGroup()
    if src not in (LOCAL_SOURCE_CDSE, LOCAL_SOURCE_ASF, LOCAL_SOURCE_PC):
        return LOCAL_SOURCE_CDSE
    return src


class LocalBackend(PWTTBackend):
    @property
    def name(self):
        return "Local Processing (Experimental)"

    @property
    def id(self):
        return "local"

    def check_dependencies(self):
        from . import deps

        src = _settings_local_source()
        missing, pip = deps.local_backend_missing(src)
        if missing:
            if pip:
                return False, f"Local backend ({src}) requires: pip install {' '.join(pip)}"
            return False, f"Local backend missing: {', '.join(missing)}"
        return True, ""

    def authenticate(self, credentials: dict) -> bool:
        source = (credentials.get("source") or LOCAL_SOURCE_CDSE).strip().lower()
        if source not in (LOCAL_SOURCE_CDSE, LOCAL_SOURCE_ASF, LOCAL_SOURCE_PC):
            source = LOCAL_SOURCE_CDSE
        self._data_source = source

        if source == LOCAL_SOURCE_CDSE:
            user = (credentials.get("username") or "").strip()
            password = credentials.get("password") or ""
            if not user:
                raise ValueError("CDSE username is required.")
            if not password:
                raise ValueError("CDSE password is required.")
            try:
                self._token = get_token(user, password)
            except Exception as e:
                raise RuntimeError(f"CDSE authentication failed: {e}") from e
            self._asf_session = None
            self._pc_client = None
            return True

        if source == LOCAL_SOURCE_ASF:
            from .asf_downloader import authenticate_asf

            user = (credentials.get("earthdata_username") or credentials.get("username") or "").strip()
            password = credentials.get("earthdata_password") or credentials.get("password") or ""
            if not user:
                raise ValueError("Earthdata username is required for ASF.")
            if not password:
                raise ValueError("Earthdata password is required for ASF.")
            try:
                self._asf_session = authenticate_asf(user, password)
            except Exception as e:
                raise RuntimeError(
                    "ASF / Earthdata authentication failed. "
                    "Check Earthdata credentials and account access for ASF downloads. "
                    f"Details: {e}"
                ) from e
            self._token = None
            self._pc_client = None
            return True

        if source == LOCAL_SOURCE_PC:
            from .pc_downloader import authenticate_pc

            key = (credentials.get("pc_subscription_key") or "").strip() or None
            try:
                self._pc_client = authenticate_pc(key)
            except Exception as e:
                raise RuntimeError(
                    "Planetary Computer client initialization failed. "
                    "Check network access and subscription key (if provided). "
                    f"Details: {e}"
                ) from e
            self._token = None
            self._asf_session = None
            return True

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
        remote_job_id: Optional[str] = None,
        damage_threshold: float = 3.3,
        gee_viz: bool = False,
    ) -> str:
        source = getattr(self, "_data_source", LOCAL_SOURCE_CDSE)
        if source == LOCAL_SOURCE_CDSE and not getattr(self, "_token", None):
            raise ValueError("Not authenticated. Call authenticate() first.")
        if source == LOCAL_SOURCE_ASF and not getattr(self, "_asf_session", None):
            raise ValueError("Not authenticated. Call authenticate() first.")
        if source == LOCAL_SOURCE_PC and not getattr(self, "_pc_client", None):
            raise ValueError("Not authenticated. Call authenticate() first.")

        import rasterio
        from rasterio.warp import Resampling

        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
        dst_crs, dst_transform, dst_height, dst_width = _aoi_utm_grid(
            west, south, east, north
        )
        war_d = datetime.strptime(war_start[:10], "%Y-%m-%d")
        inf_d = datetime.strptime(inference_start[:10], "%Y-%m-%d")
        pre_start = _add_months_dt(war_d, -pre_interval).strftime("%Y-%m-%d")
        post_end = _add_months_dt(inf_d, post_interval).strftime("%Y-%m-%d")

        cache_dir = os.path.join(os.path.dirname(output_path), ".pwtt_cache")
        os.makedirs(cache_dir, exist_ok=True)

        # QgsTask progress must not be reset to 0 on every log line (search/download are noisy).
        _progress = [0]

        def emit(pct: int, msg: str = ""):
            pct = max(0, min(100, int(pct)))
            _progress[0] = max(_progress[0], pct)
            if progress_callback:
                progress_callback(_progress[0], msg)

        def job_log(msg: str):
            if progress_callback:
                progress_callback(_progress[0], msg)

        job_log(
            f"Local processing: source={source}, cache_dir={cache_dir}, "
            f"output_tif={output_path}"
        )
        job_log(
            f"Local: AOI WGS84 bbox west={west:.5f} south={south:.5f} "
            f"east={east:.5f} north={north:.5f}"
        )
        job_log(
            f"Local: AOI warp ~{_AOI_RESOLUTION_M} m UTM, {dst_width}×{dst_height} px, "
            f"CRS {dst_crs} (per-scene warp; pre/post loaded sequentially)"
        )
        job_log(
            f"Local: pre window {pre_start} … {war_start[:10]}, "
            f"post window {inference_start[:10]} … {post_end}"
        )

        self.run_metadata = {
            "collection": "SENTINEL-1 IW_GRDH_1S",
            "data_source": source,
            "pre_period": {"start": pre_start, "end": war_start},
            "post_period": {"start": inference_start, "end": post_end},
            "bbox": [west, south, east, north],
            "pre_scenes_found": [],
            "post_scenes_found": [],
            "pre_scenes_used": [],
            "post_scenes_used": [],
            "offline_scenes": [],
        }

        emit(5, "Searching pre-war products…")

        if source == LOCAL_SOURCE_CDSE:
            pre_products = search_s1_grd(
                self._token,
                aoi_wkt,
                pre_start,
                war_start,
                max_results=20,
                log=job_log,
            )
            emit(10, "Searching post-war products…")
            post_products = search_s1_grd(
                self._token,
                aoi_wkt,
                inference_start,
                post_end,
                max_results=20,
                log=job_log,
            )
        elif source == LOCAL_SOURCE_ASF:
            from .asf_downloader import search_s1_grd_asf

            emit(5, "Searching pre-war products…")
            pre_products = search_s1_grd_asf(
                self._asf_session,
                aoi_wkt,
                pre_start,
                war_start,
                max_results=20,
                log=job_log,
            )
            emit(10, "Searching post-war products…")
            post_products = search_s1_grd_asf(
                self._asf_session,
                aoi_wkt,
                inference_start,
                post_end,
                max_results=20,
                log=job_log,
            )
        else:
            from .pc_downloader import search_s1_grd_pc

            emit(5, "Searching pre-war products…")
            pre_products = search_s1_grd_pc(
                self._pc_client,
                aoi_wkt,
                pre_start,
                war_start,
                max_results=20,
                log=job_log,
            )
            emit(10, "Searching post-war products…")
            post_products = search_s1_grd_pc(
                self._pc_client,
                aoi_wkt,
                inference_start,
                post_end,
                max_results=20,
                log=job_log,
            )

        if not pre_products or not post_products:
            if source == LOCAL_SOURCE_CDSE:
                raise RuntimeError(
                    "No Sentinel-1 GRD products found on CDSE for this AOI/date range. "
                    "Try a wider date range or switch local source to ASF/Planetary Computer."
                )
            if source == LOCAL_SOURCE_ASF:
                raise RuntimeError(
                    "No Sentinel-1 GRD products found on ASF for this AOI/date range. "
                    "Confirm Earthdata access and try a wider date range."
                )
            raise RuntimeError(
                "No Sentinel-1 GRD products found on Planetary Computer for this AOI/date range. "
                "Try a wider date range or use CDSE/ASF source."
            )

        def _scene_summary(prod):
            cd = prod.get("ContentDate") or {}
            return {
                "id": prod.get("Id", ""),
                "name": prod.get("Name", ""),
                "date": (cd.get("Start") or "")[:19],
                "online": prod.get("Online", True),
            }

        self.run_metadata["pre_scenes_found"] = [_scene_summary(p) for p in pre_products]
        self.run_metadata["post_scenes_found"] = [_scene_summary(p) for p in post_products]

        def _preview_names(products, n=6):
            parts = []
            for p in products[:n]:
                parts.append(str(p.get("Name") or p.get("Id") or "?"))
            tail = f" (+{len(products) - n} more)" if len(products) > n else ""
            return ", ".join(parts) + tail

        job_log(f"Local: pre search — {_preview_names(pre_products)}")
        job_log(f"Local: post search — {_preview_names(post_products)}")
        job_log(
            f"Local: will try up to {MAX_SCENES_PER_PERIOD} pre + "
            f"{MAX_SCENES_PER_PERIOD} post scenes (pre period, then post; AOI warp per scene)"
        )

        max_per_period = MAX_SCENES_PER_PERIOD
        pre_arrays = []
        post_arrays = []
        triggered_orders = [0]  # mutable container for thread-safe increment
        offline_product_ids = []
        download_failures = []
        _lock = threading.Lock()

        def load_products_safe(products, arrays_list, label, used_key):
            loaded = 0
            for prod in products:
                if loaded >= max_per_period:
                    break
                emit(
                    min(54, 12 + int(42 * (loaded + 1) / max_per_period)),
                    f"{label} {loaded + 1}/{max_per_period}…",
                )
                pid, name = prod["Id"], prod["Name"]
                cd = prod.get("ContentDate") or {}
                scene_date = (cd.get("Start") or "")[:19]

                if source == LOCAL_SOURCE_CDSE:
                    safe_dir = download_product(
                        self._token,
                        pid,
                        name,
                        cache_dir,
                        wait_for_offline=False,
                        log=job_log,
                    )
                    if safe_dir is None:
                        with _lock:
                            triggered_orders[0] += 1
                            offline_product_ids.append(pid)
                            self.run_metadata["offline_scenes"].append(
                                {"id": pid, "name": name, "date": scene_date}
                            )
                        job_log(f"{label}: {name} is offline, staging order triggered…")
                        continue
                else:
                    from .asf_downloader import download_product_asf

                    err_msg = None
                    try:
                        safe_dir = download_product_asf(
                            self._asf_session, prod, cache_dir, log=job_log
                        )
                    except Exception as dl_err:
                        safe_dir = None
                        err_msg = str(dl_err)
                        with _lock:
                            download_failures.append({"name": name, "error": err_msg})
                    if not safe_dir:
                        if err_msg is None:
                            err_msg = "no SAFE/ZIP in cache after download (unexpected)"
                            with _lock:
                                download_failures.append({"name": name, "error": err_msg})
                        detail = _short_progress_error(err_msg)
                        job_log(f"{label}: skip {name} — {detail}")
                        continue

                vv_path, vh_path = find_vv_vh_in_safe(safe_dir)
                if not vv_path or not vh_path:
                    job_log(
                        f"{label}: no VV/VH tifs under measurement/ in {safe_dir} "
                        f"(skip «{name}»)"
                    )
                    continue
                job_log(
                    f"{label}: VV/VH rasters — {vv_path} | {vh_path}"
                )
                result = _read_warp_vv_vh_pair(
                    vv_path,
                    vh_path,
                    dst_crs,
                    dst_transform,
                    dst_height,
                    dst_width,
                    Resampling.bilinear,
                    job_log,
                )
                if result is None:
                    job_log(f"{label}: skip {name} (AOI warp failed)…")
                    continue
                if source == LOCAL_SOURCE_CDSE:
                    from .downloader import remove_product_zip

                    remove_product_zip(name, cache_dir, log=job_log)
                elif source == LOCAL_SOURCE_ASF:
                    from .asf_downloader import remove_zips_for_extracted_safe

                    remove_zips_for_extracted_safe(safe_dir, log=job_log)
                arrays_list.append(result)
                with _lock:
                    self.run_metadata[used_key].append({"id": pid, "name": name, "date": scene_date})
                loaded += 1
                job_log(
                    f"{label}: accepted scene «{name}» ({scene_date}) "
                    f"[{loaded}/{max_per_period}]"
                )

        def load_products_pc(products, arrays_list, label, used_key):
            from .pc_downloader import download_pc_vv_vh

            loaded = 0
            for prod in products:
                if loaded >= max_per_period:
                    break
                emit(
                    min(54, 12 + int(42 * (loaded + 1) / max_per_period)),
                    f"{label} {loaded + 1}/{max_per_period}…",
                )
                pid, name = prod["Id"], prod["Name"]
                cd = prod.get("ContentDate") or {}
                scene_date = (cd.get("Start") or "")[:19]
                subdir = os.path.join(cache_dir, "pc_" + "".join(c if c.isalnum() else "_" for c in pid)[:120])
                dl_err = None
                try:
                    vv_path, vh_path = download_pc_vv_vh(prod, subdir, log=job_log)
                except Exception as e:
                    dl_err = e
                    vv_path, vh_path = None, None
                    with _lock:
                        download_failures.append({"name": name, "error": str(dl_err)})
                if not vv_path or not vh_path:
                    if dl_err is not None:
                        detail = _short_progress_error(str(dl_err))
                        job_log(f"{label}: skip {name} — {detail}")
                    else:
                        job_log(f"{label}: skip {name} (no VV/VH assets)…")
                    continue
                job_log(f"{label}: PC COGs ready — {vv_path} | {vh_path}")
                result = _read_warp_vv_vh_pair(
                    vv_path,
                    vh_path,
                    dst_crs,
                    dst_transform,
                    dst_height,
                    dst_width,
                    Resampling.bilinear,
                    job_log,
                )
                if result is None:
                    job_log(f"{label}: skip {name} (AOI warp failed)…")
                    continue
                arrays_list.append(result)
                with _lock:
                    self.run_metadata[used_key].append({"id": pid, "name": name, "date": scene_date})
                loaded += 1
                job_log(
                    f"{label}: accepted PC scene «{name}» ({scene_date}) "
                    f"[{loaded}/{max_per_period}]"
                )

        emit(12, "Downloading pre- and post-war scenes…")

        loader = load_products_pc if source == LOCAL_SOURCE_PC else load_products_safe

        # Serial pre/post: each scene warp peaks at ~1–2 full GRD bands + small AOI;
        # parallel load previously paired with MemoryError / GDAL thread issues in QGIS.
        loader(pre_products, pre_arrays, "Pre", "pre_scenes_used")
        loader(post_products, post_arrays, "Post", "post_scenes_used")

        job_log(
            f"Local: downloads finished — pre stacks={len(pre_arrays)}, "
            f"post stacks={len(post_arrays)}"
        )

        if not pre_arrays or not post_arrays:
            if source == LOCAL_SOURCE_CDSE and triggered_orders[0] > 0:
                from .base_backend import ProductsOfflineError

                raise ProductsOfflineError(
                    f"All available products are in cold storage. "
                    f"Staging orders have been triggered for {triggered_orders[0]} product(s). "
                    f"Will auto-check and resume when products become available.",
                    product_ids=offline_product_ids,
                    offline_scenes=list(self.run_metadata.get("offline_scenes", [])),
                )
            if source == LOCAL_SOURCE_ASF:
                detail = ""
                if download_failures:
                    detail = " Errors: " + "; ".join(
                        f"{f['name']}: {f['error']}" for f in download_failures[:3]
                    )
                raise RuntimeError(
                    "Could not load VV/VH data from ASF products. "
                    "Try a different date range or verify Earthdata/ASF access."
                    + detail
                )
            if source == LOCAL_SOURCE_PC:
                detail = ""
                if download_failures:
                    detail = " Errors: " + "; ".join(
                        f"{f['name']}: {f['error']}" for f in download_failures[:3]
                    )
                raise RuntimeError(
                    "Could not load VV/VH assets from Planetary Computer STAC items. "
                    "Try a different date range or switch source."
                    + detail
                )
            raise RuntimeError(
                "Could not load VV/VH data from any CDSE product. "
                "All products may be offline; try again later or switch source."
            )

        ref_vv, ref_vh, ref_profile, ref_transform, ref_crs = pre_arrays[0]
        height, width = ref_vv.shape
        pixel_size = abs(ref_transform.a)
        job_log(
            f"Local: reference grid {width}×{height} px, "
            f"~{pixel_size:.2f} m, CRS {ref_crs}"
        )

        pre_vv_list = []
        pre_vh_list = []
        for vv, vh, prof, tr, crs in pre_arrays:
            vv_f = _lee_filter(vv)
            vh_f = _lee_filter(vh)
            pre_vv_list.append(np.log(np.maximum(vv_f, 1e-12)))
            pre_vh_list.append(np.log(np.maximum(vh_f, 1e-12)))
        post_vv_list = []
        post_vh_list = []
        for vv, vh, prof, tr, crs in post_arrays:
            vv_f = _lee_filter(vv)
            vh_f = _lee_filter(vh)
            post_vv_list.append(np.log(np.maximum(vv_f, 1e-12)))
            post_vh_list.append(np.log(np.maximum(vh_f, 1e-12)))

        job_log(
            "Local: Lee filter + log-amplitude — "
            f"pre {len(pre_vv_list)} layers, post {len(post_vh_list)} layers"
        )
        emit(55, "Computing t-test…")
        pre_vv = np.stack(pre_vv_list, axis=0)
        pre_vh = np.stack(pre_vh_list, axis=0)
        post_vv = np.stack(post_vv_list, axis=0)
        post_vh = np.stack(post_vh_list, axis=0)
        pre_n, post_n = pre_vv.shape[0], post_vv.shape[0]
        pre_mean_vv = np.nanmean(pre_vv, axis=0)
        pre_sd_vv = np.nanstd(pre_vv, axis=0)
        pre_mean_vh = np.nanmean(pre_vh, axis=0)
        pre_sd_vh = np.nanstd(pre_vh, axis=0)
        post_mean_vv = np.nanmean(post_vv, axis=0)
        post_sd_vv = np.nanstd(post_vv, axis=0)
        post_mean_vh = np.nanmean(post_vh, axis=0)
        post_sd_vh = np.nanstd(post_vh, axis=0)
        eps = 1e-12
        # Degrees of freedom; eps prevents division by zero when pre_n + post_n == 2
        df = max(pre_n + post_n - 2, 1)
        pooled_vv = np.sqrt(
            (pre_sd_vv ** 2 * (pre_n - 1) + post_sd_vv ** 2 * (post_n - 1)) / df + eps
        )
        pooled_vh = np.sqrt(
            (pre_sd_vh ** 2 * (pre_n - 1) + post_sd_vh ** 2 * (post_n - 1)) / df + eps
        )
        denom_vv = pooled_vv * np.sqrt(1.0 / pre_n + 1.0 / post_n) + eps
        denom_vh = pooled_vh * np.sqrt(1.0 / pre_n + 1.0 / post_n) + eps
        t_vv = np.abs(post_mean_vv - pre_mean_vv) / denom_vv
        t_vh = np.abs(post_mean_vh - pre_mean_vh) / denom_vh
        max_change = np.maximum(t_vv, t_vh)
        job_log(
            f"Local: Welch-style t on means — pre_n={pre_n} post_n={post_n}, "
            f"df={df}, damage |t| threshold={damage_threshold}"
        )

        p_vv = two_sided_normal_p_value(t_vv)
        p_vh = two_sided_normal_p_value(t_vh)
        p_value = np.minimum(p_vv, p_vh)
        p_value = np.clip(p_value, 1e-10, 1.0)

        emit(70, "Post-processing…")
        job_log("Local: 10 m Gaussian on max |t|, then 50/100/150 m ring means (equal weight)")
        max_change = _focal_median_gaussian(max_change, 10.0, pixel_size)

        def _mean_kernel(radius_m):
            k = _circle_kernel(radius_m, pixel_size)
            k = k / (k.sum() + 1e-12)
            return k

        k50 = convolve2d_edge(max_change, _mean_kernel(50))
        k100 = convolve2d_edge(max_change, _mean_kernel(100))
        k150 = convolve2d_edge(max_change, _mean_kernel(150))
        t_statistic = (max_change + k50 + k100 + k150) / 4.0
        damage = (t_statistic > float(damage_threshold)).astype(np.float32)

        emit(90, "Writing GeoTIFF…")
        job_log(
            f"Local: write GeoTIFF bands (1=t, 2=damage mask, 3=p-value) LZW → {output_path}"
        )
        out_profile = ref_profile.copy()
        out_profile.update(
            dtype=rasterio.float32,
            count=3,
            compress="lzw",
            nodata=-9999,
        )
        with rasterio.open(output_path, "w", **out_profile) as dst:
            dst.write(t_statistic.astype(np.float32), 1)
            dst.write(damage, 2)
            dst.write(p_value.astype(np.float32), 3)

        self.run_metadata["output_size_bytes"] = os.path.getsize(output_path)
        self.run_metadata["output_crs"] = str(ref_crs)
        self.run_metadata["output_pixel_size_m"] = round(pixel_size, 2)
        self.run_metadata["output_shape"] = [height, width]
        self.run_metadata["pre_scenes_count"] = len(pre_arrays)
        self.run_metadata["post_scenes_count"] = len(post_arrays)

        emit(95, "Done.")
        return output_path
