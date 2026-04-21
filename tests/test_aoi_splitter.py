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
gee_backend_mod.GEE_GETDOWNLOAD_EFFECTIVE_MAX_BYTES = int(GEE_MAX / 1.15)
gee_backend_mod.estimate_gee_getdownload_request_bytes = (
    lambda w, s, e, n: 10_000_000  # ~10 MiB stub
)
sys.modules["core.gee_backend"] = gee_backend_mod

import core.aoi_splitter as splitter  # noqa: E402
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
    wests = [t[0] for t in tiles]
    easts = [t[2] for t in tiles]
    souths = [t[1] for t in tiles]
    norths = [t[3] for t in tiles]
    assert min(wests) == pytest.approx(0.0)
    assert max(easts) == pytest.approx(1.0)
    assert min(souths) == pytest.approx(0.0)
    assert max(norths) == pytest.approx(1.0)


def test_split_bbox_overlap_expands_tiles():
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.05)
    no_ov = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    for t, b in zip(tiles, no_ov):
        assert (t[2] - t[0]) > (b[2] - b[0])


def test_split_bbox_order_top_to_bottom_left_to_right():
    # 2×2 grid: tiles[0]=top-left, tiles[1]=top-right, tiles[2]=bottom-left, tiles[3]=bottom-right
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    assert len(tiles) == 4
    assert tiles[0][1] > tiles[2][1]   # top-left south > bottom-left south
    assert tiles[0][0] < tiles[1][0]   # top-left west < top-right west


def test_split_bbox_uniform_cells():
    tiles = splitter.split_bbox([0.0, 0.0, 1.5, 1.0], "openeo", overlap_deg=0.0)
    widths = [t[2] - t[0] for t in tiles]
    heights = [t[3] - t[1] for t in tiles]
    for w in widths[1:]:
        assert w == pytest.approx(widths[0])
    for h in heights[1:]:
        assert h == pytest.approx(heights[0])


def test_split_bbox_clamped_near_antimeridian():
    # AOI at east edge — overlap would push east past 180; must be clamped to exactly 180
    tiles = splitter.split_bbox([179.6, 0.0, 180.0, 0.5], "openeo", overlap_deg=0.05)
    east_edges = [t[2] for t in tiles]
    # The rightmost tiles must be clamped to exactly 180.0
    assert max(east_edges) == pytest.approx(180.0)
    # Without clamping it would be 180.05 — verify clamping reduced it
    assert max(east_edges) < 180.05


def test_split_bbox_clamped_near_pole():
    # AOI at north pole — overlap would push north past 90; must be clamped to exactly 90
    tiles = splitter.split_bbox([0.0, 89.6, 1.0, 90.0], "openeo", overlap_deg=0.05)
    north_edges = [t[3] for t in tiles]
    assert max(north_edges) == pytest.approx(90.0)
    assert max(north_edges) < 90.05


# ── estimate_gee_bytes ────────────────────────────────────────────────────────


def test_estimate_gee_bytes_returns_int():
    result = splitter.estimate_gee_bytes([0.0, 0.0, 0.2, 0.2])
    assert isinstance(result, int)
    assert result > 0


def test_estimate_gee_bytes_delegates_to_backend():
    # The stub returns a fixed value; verify delegation works
    result = splitter.estimate_gee_bytes([0.0, 0.0, 0.2, 0.2])
    assert result == 10_000_000  # stub value


# ── estimate_openeo_pu ────────────────────────────────────────────────────────


def test_estimate_openeo_pu_positive():
    pu = splitter.estimate_openeo_pu([0.0, 0.0, 0.5, 0.5])
    assert pu > 0.0


def test_estimate_openeo_pu_larger_area_costs_more():
    small = splitter.estimate_openeo_pu([0.0, 0.0, 0.1, 0.1])
    large = splitter.estimate_openeo_pu([0.0, 0.0, 0.5, 0.5])
    assert large > small


def test_estimate_openeo_pu_half_deg_reasonable():
    # 0.5° × 0.5° at equator ≈ 55 km × 55 km → ~5500×5500 px
    # PU ≈ (5500*5500 / (512*512)) * 3 * 2 ≈ 692
    pu = splitter.estimate_openeo_pu([0.0, 0.0, 0.5, 0.5])
    assert 100 < pu < 5000  # sanity range
