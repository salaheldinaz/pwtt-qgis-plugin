import math
import sys
import types
import importlib
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stub qgis
qgis_mod = types.ModuleType("qgis")
qgis_core = types.ModuleType("qgis.core")
sys.modules.setdefault("qgis", qgis_mod)
sys.modules["qgis.core"] = qgis_core

# Stub gee_backend with known values
gee_backend_mod = types.ModuleType("core.gee_backend")
GEE_MAX = 50_331_648  # 48 MiB
gee_backend_mod.GEE_GETDOWNLOAD_MAX_BYTES = GEE_MAX
gee_backend_mod.estimate_gee_getdownload_request_bytes = (
    lambda w, s, e, n: 10_000_000  # ~10 MiB stub
)
sys.modules["core.gee_backend"] = gee_backend_mod

import core.aoi_splitter as splitter
importlib.reload(splitter)


# ── needs_split ───────────────────────────────────────────────────────────────

def test_needs_split_openeo_large():
    assert splitter.needs_split([0.0, 0.0, 1.0, 1.0], "openeo") is True

def test_needs_split_openeo_small():
    assert splitter.needs_split([0.0, 0.0, 0.3, 0.3], "openeo") is False

def test_needs_split_local_large():
    assert splitter.needs_split([0.0, 0.0, 2.0, 2.0], "local") is True

def test_needs_split_local_small():
    assert splitter.needs_split([0.0, 0.0, 0.5, 0.5], "local") is False

def test_needs_split_gee_small():
    # stub returns 10 MiB which is below 48 MiB cap
    assert splitter.needs_split([0.0, 0.0, 0.1, 0.1], "gee") is False

def test_needs_split_gee_large(monkeypatch):
    monkeypatch.setattr(
        gee_backend_mod,
        "estimate_gee_getdownload_request_bytes",
        lambda w, s, e, n: GEE_MAX + 1,
    )
    importlib.reload(splitter)
    assert splitter.needs_split([0.0, 0.0, 1.0, 1.0], "gee") is True
    monkeypatch.setattr(
        gee_backend_mod,
        "estimate_gee_getdownload_request_bytes",
        lambda w, s, e, n: 10_000_000,
    )
    importlib.reload(splitter)


# ── tile_grid_dims ────────────────────────────────────────────────────────────

def test_tile_grid_dims_openeo_2x2():
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 1.0, 1.0], "openeo")
    assert cols == 2
    assert rows == 2

def test_tile_grid_dims_openeo_exact():
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 0.5, 0.5], "openeo")
    assert cols == 1
    assert rows == 1

def test_tile_grid_dims_openeo_3x2():
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 1.4, 0.9], "openeo")
    assert cols == 3
    assert rows == 2

def test_tile_grid_dims_local():
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 2.5, 1.5], "local")
    assert cols == 3
    assert rows == 2

def test_tile_grid_dims_minimum_1():
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 0.01, 0.01], "openeo")
    assert cols >= 1
    assert rows >= 1


# ── split_bbox ────────────────────────────────────────────────────────────────

def test_split_bbox_count():
    # 1° × 1° openEO → 2×2 = 4 tiles
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    assert len(tiles) == 4


def test_split_bbox_no_overlap_union():
    # With no overlap, tiles exactly tile the bbox (no gaps, no duplicates)
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    wests  = [t[0] for t in tiles]
    easts  = [t[2] for t in tiles]
    souths = [t[1] for t in tiles]
    norths = [t[3] for t in tiles]
    assert min(wests)  == pytest.approx(0.0)
    assert max(easts)  == pytest.approx(1.0)
    assert min(souths) == pytest.approx(0.0)
    assert max(norths) == pytest.approx(1.0)


def test_split_bbox_overlap_expands_tiles():
    tiles    = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.05)
    no_ov    = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    for t, b in zip(tiles, no_ov):
        assert (t[2] - t[0]) > (b[2] - b[0])


def test_split_bbox_order_top_to_bottom_left_to_right():
    # 2×2 grid: tiles[0]=top-left, tiles[1]=top-right, tiles[2]=bottom-left, tiles[3]=bottom-right
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    assert len(tiles) == 4
    assert tiles[0][1] > tiles[2][1]   # top-left south > bottom-left south
    assert tiles[0][0] < tiles[1][0]   # top-left west < top-right west


def test_split_bbox_uniform_cells():
    tiles   = splitter.split_bbox([0.0, 0.0, 1.5, 1.0], "openeo", overlap_deg=0.0)
    widths  = [round(t[2] - t[0], 8) for t in tiles]
    heights = [round(t[3] - t[1], 8) for t in tiles]
    assert len(set(widths))  == 1
    assert len(set(heights)) == 1


def test_split_bbox_clamped_near_antimeridian():
    # AOI near east edge — overlap must not push past 180
    tiles = splitter.split_bbox([179.6, 0.0, 180.0, 0.5], "openeo", overlap_deg=0.05)
    for t in tiles:
        assert t[2] <= 180.0


def test_split_bbox_clamped_near_pole():
    # AOI near north pole — overlap must not push past 90
    tiles = splitter.split_bbox([0.0, 89.6, 1.0, 90.0], "openeo", overlap_deg=0.05)
    for t in tiles:
        assert t[3] <= 90.0
