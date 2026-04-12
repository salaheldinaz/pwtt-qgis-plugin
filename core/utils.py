# -*- coding: utf-8 -*-
"""Shared helpers: AOI conversion, output dir."""

import os
import re
from datetime import datetime
from typing import Optional, Tuple

# English month abbreviations — fixed across devices (avoid locale-dependent strftime %b).
_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _parse_iso_datetime(iso_str: str) -> Optional[datetime]:
    if not iso_str or not isinstance(iso_str, str):
        return None
    s = iso_str.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def format_ymd_display(year: int, month: int, day: int) -> str:
    """Calendar date as 'Mar 13, 2025' (English, locale-independent)."""
    return f"{_MONTH_ABBR[month - 1]} {day}, {year}"


def format_iso_date_display(iso_str: str) -> str:
    """ISO date or datetime string → 'Mar 13, 2025'."""
    dt = _parse_iso_datetime(iso_str)
    if dt is None:
        if not iso_str:
            return ""
        return iso_str[:10] if len(iso_str) >= 10 else iso_str
    return f"{_MONTH_ABBR[dt.month - 1]} {dt.day}, {dt.year}"


def format_iso_datetime_display(iso_str: str) -> str:
    """ISO datetime → 'Mar 13, 2025, 14:30' (24h time); date-only input → date only."""
    if not iso_str:
        return ""
    s = iso_str.strip()
    dt = _parse_iso_datetime(s)
    if dt is None:
        return s[:16].replace("T", " ") if "T" in s else s[:19]
    base = f"{_MONTH_ABBR[dt.month - 1]} {dt.day}, {dt.year}"
    if len(s) <= 10:
        return base
    if "T" not in s.upper():
        return base
    return f"{base}, {dt.hour:02d}:{dt.minute:02d}"


def wkt_to_bbox(wkt: str) -> Optional[Tuple[float, float, float, float]]:
    """Extract (west, south, east, north) from WKT polygon (EPSG:4326). Returns None if invalid."""
    # Prefer shapely for robust parsing of complex geometries
    try:
        from shapely import wkt as shapely_wkt
        geom = shapely_wkt.loads(wkt)
        west, south, east, north = geom.bounds
        return west, south, east, north
    except Exception:
        pass
    # Fallback: regex extraction for simple polygons (e.g. QGIS bbox output)
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
