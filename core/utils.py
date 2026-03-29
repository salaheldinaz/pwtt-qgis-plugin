# -*- coding: utf-8 -*-
"""Shared helpers: AOI conversion, output dir."""

import os
import re
from typing import Optional, Tuple


def wkt_to_bbox(wkt: str) -> Optional[Tuple[float, float, float, float]]:
    """Extract (west, south, east, north) from WKT polygon (EPSG:4326). Returns None if invalid."""
    numbers = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", wkt)
    if len(numbers) < 4:
        return None
    floats = [float(x) for x in numbers]
    xs = floats[0::2]
    ys = floats[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def ensure_output_dir(path: str) -> str:
    """Create parent directory of path if needed. Return path."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return path


def raster_bounds_to_aoi_wkt(raster_path: str) -> Optional[str]:
    """Axis-aligned EPSG:4326 polygon WKT from a raster's geographic extent."""
    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError:
        return None
    try:
        with rasterio.open(raster_path) as src:
            if src.crs is None:
                return None
            west, south, east, north = transform_bounds(
                src.crs, "EPSG:4326", *src.bounds, densify_pts=21
            )
    except Exception:
        return None
    return (
        f"POLYGON(({west} {south}, {east} {south}, {east} {north}, "
        f"{west} {north}, {west} {south}))"
    )
