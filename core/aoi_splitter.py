# -*- coding: utf-8 -*-
"""AOI tiling: detect oversized AOIs, split into uniform tile grids, estimate costs."""

from __future__ import annotations

import math
from typing import List, Tuple

# openEO/CDSE: tested to ~100×100 km; conservative for free-tier 10,000 PU/month
_OPENEO_MAX_DEG: float = 0.5
# Local: no hard limit; large sensible default
_LOCAL_MAX_DEG: float = 1.0

# GEE constants mirrored from gee_backend.py
_GEE_SCALE_M: int = 10
_GEE_BANDS: int = 3
_GEE_BYTES_PER_BAND: int = 4  # float32

# Re-export so callers can read it without importing gee_backend
GEE_GETDOWNLOAD_MAX_BYTES: int = 50_331_648  # 48 MiB


def _m_per_deg_lon(mid_lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(mid_lat))


def _m_per_deg_lat() -> float:
    return 111_320.0


def _max_tile_deg(backend_id: str, mid_lat: float) -> float:
    if backend_id == "gee":
        # Back-calculate max square tile that fits under GEE_GETDOWNLOAD_MAX_BYTES
        mpd_lon = _m_per_deg_lon(mid_lat)
        mpd_lat = _m_per_deg_lat()
        max_pixels = GEE_GETDOWNLOAD_MAX_BYTES / (_GEE_BANDS * _GEE_BYTES_PER_BAND)
        side_m = math.sqrt(max_pixels) * _GEE_SCALE_M
        return min(side_m / mpd_lon, side_m / mpd_lat)
    if backend_id == "openeo":
        return _OPENEO_MAX_DEG
    return _LOCAL_MAX_DEG


def needs_split(bbox: List[float], backend_id: str) -> bool:
    """True if bbox exceeds the backend's per-job size limit."""
    west, south, east, north = bbox
    mid_lat = (south + north) / 2.0

    if backend_id == "gee":
        from .gee_backend import estimate_gee_getdownload_request_bytes
        est = estimate_gee_getdownload_request_bytes(west, south, east, north)
        return est > GEE_GETDOWNLOAD_MAX_BYTES

    max_deg = _max_tile_deg(backend_id, mid_lat)
    return (east - west) > max_deg or (north - south) > max_deg


def tile_grid_dims(bbox: List[float], backend_id: str) -> Tuple[int, int]:
    """Return (cols, rows) for the split grid — no overlap applied."""
    west, south, east, north = bbox
    mid_lat = (south + north) / 2.0
    max_deg = _max_tile_deg(backend_id, mid_lat)
    cols = max(1, math.ceil((east - west) / max_deg))
    rows = max(1, math.ceil((north - south) / max_deg))
    return cols, rows


def split_bbox(
    bbox: List[float],
    backend_id: str,
    overlap_deg: float = 0.01,
) -> List[List[float]]:
    """Return list of [west, south, east, north] tile bboxes covering bbox.

    Tiles are uniform (bbox divided evenly). Each tile is expanded outward by
    overlap_deg on all sides. Tiles ordered left-to-right, top-to-bottom.
    """
    west, south, east, north = bbox
    cols, rows = tile_grid_dims(bbox, backend_id)
    cell_w = (east - west) / cols
    cell_h = (north - south) / rows
    tiles = []
    for r in range(rows - 1, -1, -1):   # top-to-bottom (north first)
        for c in range(cols):            # left-to-right
            tiles.append([
                west  + c * cell_w - overlap_deg,
                south + r * cell_h - overlap_deg,
                west  + (c + 1) * cell_w + overlap_deg,
                south + (r + 1) * cell_h + overlap_deg,
            ])
    return tiles


def estimate_gee_bytes(bbox: List[float]) -> int:
    """Estimated uncompressed GEE download bytes for this bbox."""
    from .gee_backend import estimate_gee_getdownload_request_bytes
    west, south, east, north = bbox
    return estimate_gee_getdownload_request_bytes(west, south, east, north)


# openEO PU formula: (px / 512²) × bands × float32-multiplier
_OPENEO_SCALE_M: int = 10
_OPENEO_BANDS: int = 3
_OPENEO_FLOAT32_MULT: float = 2.0
_OPENEO_BASELINE_PX: int = 512 * 512


def estimate_openeo_pu(bbox: List[float]) -> float:
    """Estimated PU for an openEO batch job (S1/S2, 10 m, 3 bands, float32)."""
    west, south, east, north = bbox
    mid_lat = (south + north) / 2.0
    width_m  = (east - west)   * _m_per_deg_lon(mid_lat)
    height_m = (north - south) * _m_per_deg_lat()
    width_px  = math.ceil(width_m  / _OPENEO_SCALE_M)
    height_px = math.ceil(height_m / _OPENEO_SCALE_M)
    px_factor = (width_px * height_px) / _OPENEO_BASELINE_PX
    return px_factor * _OPENEO_BANDS * _OPENEO_FLOAT32_MULT
