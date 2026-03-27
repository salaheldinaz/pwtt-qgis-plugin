# -*- coding: utf-8 -*-
"""Full-local backend: CDSE download, Lee filter (scipy), t-test (numpy), post-process, rasterio output."""

import os
import numpy as np
from typing import Optional
from .base_backend import PWTTBackend
from .downloader import get_token, search_s1_grd, download_product, find_vv_vh_in_safe
from .utils import wkt_to_bbox


def _add_months(y, m, months):
    m -= 1
    m += months
    y += m // 12
    m = m % 12 + 1
    return y, m


def _lee_filter(band: np.ndarray, kernel_radius: int = 1, enl: float = 5.0) -> np.ndarray:
    """Lee speckle filter (MMSE). band: 2D float. Returns filtered 2D array."""
    from scipy.ndimage import uniform_filter
    eta = 1.0 / np.sqrt(enl)
    one = np.ones_like(band)
    mean = uniform_filter(band, size=2 * kernel_radius + 1, mode="nearest")
    mean_sq = uniform_filter(band ** 2, size=2 * kernel_radius + 1, mode="nearest")
    var = mean_sq - mean ** 2
    var = np.maximum(var, 1e-12)
    varx = (var - (mean ** 2) * (eta ** 2)) / (1 + eta ** 2)
    b = np.clip(varx / var, 0, 1)
    return (1 - b) * np.abs(mean) + b * band


def _focal_median_gaussian(data: np.ndarray, sigma_m: float, pixel_size: float) -> np.ndarray:
    """Gaussian smoothing approximating focal median in meters. sigma_m in meters."""
    from scipy.ndimage import gaussian_filter
    sigma_px = max(1.0, sigma_m / pixel_size)
    return gaussian_filter(data, sigma=sigma_px, mode="nearest")


def _circle_kernel(radius_m: float, pixel_size: float) -> np.ndarray:
    """Binary circle kernel in pixels."""
    r_px = int(np.ceil(radius_m / pixel_size))
    y, x = np.ogrid[-r_px : r_px + 1, -r_px : r_px + 1]
    return ((x * x + y * y) <= (radius_m / pixel_size) ** 2).astype(np.float64)


class LocalBackend(PWTTBackend):
    @property
    def name(self):
        return "Local Processing"

    @property
    def id(self):
        return "local"

    def check_dependencies(self):
        for pkg in ("numpy", "scipy", "rasterio", "requests"):
            try:
                __import__(pkg)
            except ImportError:
                return False, f"Local backend requires: pip install numpy scipy rasterio requests"
        return True, ""

    def authenticate(self, credentials: dict) -> bool:
        user = (credentials.get("username") or "").strip()
        password = credentials.get("password") or ""
        if not user or not password:
            return False
        try:
            self._token = get_token(user, password)
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
        if not getattr(self, "_token", None):
            raise ValueError("Not authenticated. Call authenticate() first.")
        import rasterio
        from rasterio.warp import reproject, Resampling
        from scipy.ndimage import convolve

        bbox = wkt_to_bbox(aoi_wkt)
        if not bbox:
            raise ValueError("Invalid AOI WKT")
        west, south, east, north = bbox
        war_y, war_m = int(war_start[:4]), int(war_start[5:7])
        inf_y, inf_m = int(inference_start[:4]), int(inference_start[5:7])
        pre_start_y, pre_start_m = _add_months(war_y, war_m, -pre_interval)
        post_end_y, post_end_m = _add_months(inf_y, inf_m, post_interval)
        pre_start = f"{pre_start_y}-{pre_start_m:02d}-01"
        post_end = f"{post_end_y}-{post_end_m:02d}-01"

        cache_dir = os.path.join(os.path.dirname(output_path), ".pwtt_cache")
        os.makedirs(cache_dir, exist_ok=True)

        if progress_callback:
            progress_callback(5, "Searching pre-war products…")
        pre_products = search_s1_grd(self._token, aoi_wkt, pre_start, war_start, max_results=20)
        if progress_callback:
            progress_callback(10, "Searching post-war products…")
        post_products = search_s1_grd(self._token, aoi_wkt, inference_start, post_end, max_results=20)
        if not pre_products or not post_products:
            raise RuntimeError("No Sentinel-1 GRD products found for the given AOI and dates.")

        # Download and load up to 3 pre and 3 post (to limit disk/time)
        max_per_period = 3
        pre_arrays = []  # list of (vv, vh, profile)
        post_arrays = []

        def load_products(products, arrays_list, label):
            loaded = 0
            for i, prod in enumerate(products):
                if loaded >= max_per_period:
                    break
                if progress_callback:
                    progress_callback(0, f"{label} {loaded+1}/{max_per_period}…")
                pid, name = prod["Id"], prod["Name"]
                # Try download; skip offline products that aren't immediately available
                safe_dir = download_product(self._token, pid, name, cache_dir, wait_for_offline=False)
                if safe_dir is None:
                    if progress_callback:
                        progress_callback(0, f"{label}: {name} is offline, skipping…")
                    continue
                vv_path, vh_path = find_vv_vh_in_safe(safe_dir)
                if not vv_path or not vh_path:
                    continue
                with rasterio.open(vv_path) as src:
                    vv = src.read(1)
                    profile = src.profile.copy()
                    transform = src.transform
                    crs = src.crs
                with rasterio.open(vh_path) as src:
                    vh = src.read(1)
                arrays_list.append((vv.astype(np.float32), vh.astype(np.float32), profile, transform, crs))
                loaded += 1

        if progress_callback:
            progress_callback(12, "Downloading pre-war scenes…")
        load_products(pre_products, pre_arrays, "Pre")
        if progress_callback:
            progress_callback(35, "Downloading post-war scenes…")
        load_products(post_products, post_arrays, "Post")

        if not pre_arrays or not post_arrays:
            raise RuntimeError(
                "Could not load VV/VH data from any product. "
                "All available products may be in offline/cold storage. "
                "Try a more recent date range or try again later."
            )

        # Use first pre image as reference grid; reproject others into it
        ref_vv, ref_vh, ref_profile, ref_transform, ref_crs = pre_arrays[0]
        height, width = ref_vv.shape
        pixel_size = abs(ref_transform.a)

        def to_ref(vv, vh, profile, transform, crs):
            if crs == ref_crs and transform == ref_transform:
                return vv, vh
            out_vv = np.empty((height, width), dtype=np.float32)
            out_vh = np.empty((height, width), dtype=np.float32)
            reproject(vv, out_vv, src_transform=transform, src_crs=crs, dst_transform=ref_transform, dst_crs=ref_crs, resampling=Resampling.bilinear)
            reproject(vh, out_vh, src_transform=transform, src_crs=crs, dst_transform=ref_transform, dst_crs=ref_crs, resampling=Resampling.bilinear)
            return out_vv, out_vh

        pre_vv_list = []
        pre_vh_list = []
        for vv, vh, prof, tr, crs in pre_arrays:
            vv_r, vh_r = to_ref(vv, vh, prof, tr, crs)
            vv_f = _lee_filter(vv_r)
            vh_f = _lee_filter(vh_r)
            pre_vv_list.append(np.log(np.maximum(vv_f, 1e-12)))
            pre_vh_list.append(np.log(np.maximum(vh_f, 1e-12)))
        post_vv_list = []
        post_vh_list = []
        for vv, vh, prof, tr, crs in post_arrays:
            vv_r, vh_r = to_ref(vv, vh, prof, tr, crs)
            vv_f = _lee_filter(vv_r)
            vh_f = _lee_filter(vh_r)
            post_vv_list.append(np.log(np.maximum(vv_f, 1e-12)))
            post_vh_list.append(np.log(np.maximum(vh_f, 1e-12)))

        if progress_callback:
            progress_callback(55, "Computing t-test…")
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
        pooled_vv = np.sqrt(
            (pre_sd_vv ** 2 * (pre_n - 1) + post_sd_vv ** 2 * (post_n - 1)) / (pre_n + post_n - 2) + eps
        )
        pooled_vh = np.sqrt(
            (pre_sd_vh ** 2 * (pre_n - 1) + post_sd_vh ** 2 * (post_n - 1)) / (pre_n + post_n - 2) + eps
        )
        denom_vv = pooled_vv * np.sqrt(1.0 / pre_n + 1.0 / post_n) + eps
        denom_vh = pooled_vh * np.sqrt(1.0 / pre_n + 1.0 / post_n) + eps
        t_vv = np.abs(post_mean_vv - pre_mean_vv) / denom_vv
        t_vh = np.abs(post_mean_vh - pre_mean_vh) / denom_vh
        max_change = np.maximum(t_vv, t_vh)

        if progress_callback:
            progress_callback(70, "Post-processing…")
        max_change = _focal_median_gaussian(max_change, 10.0, pixel_size)
        def _mean_kernel(radius_m):
            k = _circle_kernel(radius_m, pixel_size)
            k = k / (k.sum() + 1e-12)
            return k
        k50 = convolve(max_change, _mean_kernel(50), mode="nearest")
        k100 = convolve(max_change, _mean_kernel(100), mode="nearest")
        k150 = convolve(max_change, _mean_kernel(150), mode="nearest")
        t_statistic = (max_change + k50 + k100 + k150) / 4.0
        damage = (t_statistic > 3).astype(np.float32)

        if progress_callback:
            progress_callback(90, "Writing GeoTIFF…")
        out_profile = ref_profile.copy()
        out_profile.update(
            dtype=rasterio.float32,
            count=2,
            compress="lzw",
            nodata=-9999,
        )
        with rasterio.open(output_path, "w", **out_profile) as dst:
            dst.write(t_statistic.astype(np.float32), 1)
            dst.write(damage, 2)
        if progress_callback:
            progress_callback(95, "Done.")
        return output_path
