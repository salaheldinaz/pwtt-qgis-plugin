# AOI Large-Area Split into Tiles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a drawn AOI exceeds the backend's per-job size limit, automatically detect this and offer the user a tiling dialog to split the large AOI into a uniform grid of smaller tiles, each becoming an independent queue entry, with quota/time warnings shown in three places.

**Architecture:** A new `core/aoi_splitter.py` module handles all geometry math and size estimation (no Qt dependencies). `_AoiSplitDialog` in `ui/main_dialog.py` handles the UI (follows the `_BatchConfirmDialog` pattern). `_on_aoi_drawn()` is refactored to call `aoi_splitter.needs_split()` and dispatch accordingly.

**Tech Stack:** Python, QGIS PyQt5 (`QgsRubberBand`, `QgsGeometry`, `QDialog`), pytest (no QGIS instance needed for core tests).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `core/aoi_splitter.py` | **Create** | Backend limits, `needs_split`, `tile_grid_dims`, `split_bbox`, `estimate_gee_bytes`, `estimate_openeo_pu` |
| `tests/test_aoi_splitter.py` | **Create** | Unit tests for splitter math (no QGIS needed) |
| `ui/main_dialog.py` | **Modify** | Add `_AoiSplitDialog`; refactor `_on_aoi_drawn`; add `_add_drawn_aoi_to_queue`, `_add_tile_aoi_to_queue`; add `_queue_warning_label`; update `_BatchConfirmDialog` |

---

### Task 1: `core/aoi_splitter.py` — scaffold + `needs_split` + `tile_grid_dims`

**Files:**
- Create: `core/aoi_splitter.py`
- Create: `tests/test_aoi_splitter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aoi_splitter.py`:

```python
import math
import sys
import types

# Stub gee_backend so aoi_splitter can be imported without a QGIS instance
import importlib
import os
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
    # 1° × 1° exceeds openEO 0.5° limit
    assert splitter.needs_split([0.0, 0.0, 1.0, 1.0], "openeo") is True


def test_needs_split_openeo_small():
    # 0.3° × 0.3° is within openEO 0.5° limit
    assert splitter.needs_split([0.0, 0.0, 0.3, 0.3], "openeo") is False


def test_needs_split_local_large():
    # 2° × 2° exceeds local 1.0° limit
    assert splitter.needs_split([0.0, 0.0, 2.0, 2.0], "local") is True


def test_needs_split_local_small():
    # 0.5° × 0.5° is within local 1.0° limit
    assert splitter.needs_split([0.0, 0.0, 0.5, 0.5], "local") is False


def test_needs_split_gee_small():
    # Very small bbox — stub returns 10 MiB which is below 48 MiB cap
    assert splitter.needs_split([0.0, 0.0, 0.1, 0.1], "gee") is False


def test_needs_split_gee_large(monkeypatch):
    # Force stub to return over cap
    monkeypatch.setattr(
        gee_backend_mod,
        "estimate_gee_getdownload_request_bytes",
        lambda w, s, e, n: GEE_MAX + 1,
    )
    importlib.reload(splitter)
    assert splitter.needs_split([0.0, 0.0, 1.0, 1.0], "gee") is True
    # Restore
    monkeypatch.setattr(
        gee_backend_mod,
        "estimate_gee_getdownload_request_bytes",
        lambda w, s, e, n: 10_000_000,
    )
    importlib.reload(splitter)


# ── tile_grid_dims ────────────────────────────────────────────────────────────

def test_tile_grid_dims_openeo_2x2():
    # 1° × 1° bbox with 0.5° limit → 2 × 2
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 1.0, 1.0], "openeo")
    assert cols == 2
    assert rows == 2


def test_tile_grid_dims_openeo_exact():
    # 0.5° × 0.5° exactly at limit → 1 × 1
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 0.5, 0.5], "openeo")
    assert cols == 1
    assert rows == 1


def test_tile_grid_dims_openeo_3x2():
    # 1.4° wide × 0.9° tall → ceil(1.4/0.5)=3 cols, ceil(0.9/0.5)=2 rows
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 1.4, 0.9], "openeo")
    assert cols == 3
    assert rows == 2


def test_tile_grid_dims_local():
    # 2.5° × 1.5° with 1.0° limit → 3 × 2
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 2.5, 1.5], "local")
    assert cols == 3
    assert rows == 2


def test_tile_grid_dims_minimum_1():
    # Tiny bbox → always at least 1×1
    cols, rows = splitter.tile_grid_dims([0.0, 0.0, 0.01, 0.01], "openeo")
    assert cols >= 1
    assert rows >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Volumes/A2/Dev/Projects/pwtt-qgis-plugin
python -m pytest tests/test_aoi_splitter.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'core.aoi_splitter'`

- [ ] **Step 3: Create `core/aoi_splitter.py` with the implementation**

```python
# -*- coding: utf-8 -*-
"""AOI tiling: detect oversized AOIs, split into uniform tile grids, estimate costs."""

from __future__ import annotations

import math
from typing import List, Tuple

# ── Backend per-job tile limits ─────────────────────────────────────────────���─

# openEO/CDSE: tested to ~100×100 km; conservative for free-tier 10,000 PU/month
_OPENEO_MAX_DEG: float = 0.5
# Local: no hard limit; large sensible default
_LOCAL_MAX_DEG: float = 1.0

# GEE constants mirrored from gee_backend.py (do NOT import at module level to
# avoid requiring earthengine-api at import time)
_GEE_SCALE_M: int = 10
_GEE_BANDS: int = 3
_GEE_BYTES_PER_BAND: int = 4  # float32

# Re-export so callers (e.g. _AoiSplitDialog) can read it without importing gee_backend
GEE_GETDOWNLOAD_MAX_BYTES: int = 50_331_648  # 48 MiB


def _m_per_deg_lon(mid_lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(mid_lat))


def _m_per_deg_lat() -> float:
    return 111_320.0


def _gee_max_tile_deg(mid_lat: float) -> float:
    """Largest square tile side (degrees) that stays under GEE_GETDOWNLOAD_MAX_BYTES."""
    mpd_lon = _m_per_deg_lon(mid_lat)
    mpd_lat = _m_per_deg_lat()
    # bytes ≈ (d * mpd_lon / scale) * (d * mpd_lat / scale) * bands * bpb
    # solve for d:
    max_pixels = GEE_GETDOWNLOAD_MAX_BYTES / (_GEE_BANDS * _GEE_BYTES_PER_BAND)
    side_m = math.sqrt(max_pixels) * _GEE_SCALE_M
    return min(side_m / mpd_lon, side_m / mpd_lat)


def _max_tile_deg(backend_id: str, mid_lat: float) -> float:
    if backend_id == "gee":
        return _gee_max_tile_deg(mid_lat)
    if backend_id == "openeo":
        return _OPENEO_MAX_DEG
    return _LOCAL_MAX_DEG  # local and any unknown backend


# ── Public API ────────────────────────────────────────────────────────────────

def needs_split(bbox: List[float], backend_id: str) -> bool:
    """True if bbox exceeds the backend's per-job size limit."""
    west, south, east, north = bbox
    width = east - west
    height = north - south
    mid_lat = (south + north) / 2.0

    if backend_id == "gee":
        # Use the actual estimator (accounts for latitude)
        from .gee_backend import estimate_gee_getdownload_request_bytes
        est = estimate_gee_getdownload_request_bytes(west, south, east, north)
        return est > GEE_GETDOWNLOAD_MAX_BYTES

    max_deg = _max_tile_deg(backend_id, mid_lat)
    return width > max_deg or height > max_deg


def tile_grid_dims(bbox: List[float], backend_id: str) -> Tuple[int, int]:
    """Return (cols, rows) for the split grid — no overlap applied."""
    west, south, east, north = bbox
    width = east - west
    height = north - south
    mid_lat = (south + north) / 2.0
    max_deg = _max_tile_deg(backend_id, mid_lat)
    cols = max(1, math.ceil(width / max_deg))
    rows = max(1, math.ceil(height / max_deg))
    return cols, rows


def split_bbox(
    bbox: List[float],
    backend_id: str,
    overlap_deg: float = 0.01,
) -> List[List[float]]:
    """Return list of [west, south, east, north] tile bboxes covering bbox.

    Tiles are uniform (bbox divided evenly). Each tile is expanded outward by
    overlap_deg on all sides. Tiles are ordered left-to-right, top-to-bottom.
    """
    west, south, east, north = bbox
    cols, rows = tile_grid_dims(bbox, backend_id)
    cell_w = (east - west) / cols
    cell_h = (north - south) / rows
    tiles = []
    for r in range(rows - 1, -1, -1):   # top-to-bottom (north first)
        for c in range(cols):            # left-to-right
            tile_west  = west  + c * cell_w - overlap_deg
            tile_east  = west  + (c + 1) * cell_w + overlap_deg
            tile_south = south + r * cell_h - overlap_deg
            tile_north = south + (r + 1) * cell_h + overlap_deg
            tiles.append([tile_west, tile_south, tile_east, tile_north])
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_aoi_splitter.py -v
```

Expected: all tests pass (the `monkeypatch` test for GEE may need a reload — if it fails, that test can be skipped for now).

- [ ] **Step 5: Commit**

```bash
git add core/aoi_splitter.py tests/test_aoi_splitter.py
git commit -m "feat: add aoi_splitter core module with needs_split and tile_grid_dims"
```

---

### Task 2: `split_bbox` tests + verification

**Files:**
- Modify: `tests/test_aoi_splitter.py`

- [ ] **Step 1: Add `split_bbox` tests to `tests/test_aoi_splitter.py`**

Append to `tests/test_aoi_splitter.py`:

```python
# ── split_bbox ────────────────────────────────────────────────────────────────

def test_split_bbox_count():
    # 1° × 1° openEO → 2×2 = 4 tiles
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    assert len(tiles) == 4


def test_split_bbox_no_overlap_union():
    # With no overlap, tiles exactly tile the bbox (no gaps, no duplicates)
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    # westernmost and easternmost tiles span the full width
    wests  = [t[0] for t in tiles]
    easts  = [t[2] for t in tiles]
    souths = [t[1] for t in tiles]
    norths = [t[3] for t in tiles]
    assert min(wests)  == pytest.approx(0.0)
    assert max(easts)  == pytest.approx(1.0)
    assert min(souths) == pytest.approx(0.0)
    assert max(norths) == pytest.approx(1.0)


def test_split_bbox_overlap_expands_tiles():
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.05)
    # Every tile should be larger than the no-overlap cell
    no_ov = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    for t, b in zip(tiles, no_ov):
        w_ov  = t[2] - t[0]
        w_base = b[2] - b[0]
        assert w_ov > w_base


def test_split_bbox_order_top_to_bottom_left_to_right():
    # 2×2 grid → tiles[0] is top-left, tiles[1] is top-right,
    #             tiles[2] is bottom-left, tiles[3] is bottom-right
    tiles = splitter.split_bbox([0.0, 0.0, 1.0, 1.0], "openeo", overlap_deg=0.0)
    assert len(tiles) == 4
    # Top row has higher south/north than bottom row
    assert tiles[0][1] > tiles[2][1]   # top-left south > bottom-left south
    # Within a row, first tile is leftmost
    assert tiles[0][0] < tiles[1][0]   # top-left west < top-right west


def test_split_bbox_uniform_cells():
    # All tiles in a 3×2 grid should have the same cell dimensions (before overlap)
    tiles = splitter.split_bbox([0.0, 0.0, 1.5, 1.0], "openeo", overlap_deg=0.0)
    widths  = [round(t[2] - t[0], 8) for t in tiles]
    heights = [round(t[3] - t[1], 8) for t in tiles]
    assert len(set(widths))  == 1
    assert len(set(heights)) == 1
```

- [ ] **Step 2: Run new tests**

```bash
python -m pytest tests/test_aoi_splitter.py::test_split_bbox_count \
                 tests/test_aoi_splitter.py::test_split_bbox_no_overlap_union \
                 tests/test_aoi_splitter.py::test_split_bbox_overlap_expands_tiles \
                 tests/test_aoi_splitter.py::test_split_bbox_order_top_to_bottom_left_to_right \
                 tests/test_aoi_splitter.py::test_split_bbox_uniform_cells -v
```

Expected: all 5 pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_aoi_splitter.py
git commit -m "test: add split_bbox tests for aoi_splitter"
```

---

### Task 3: `estimate_gee_bytes` + `estimate_openeo_pu` tests

**Files:**
- Modify: `tests/test_aoi_splitter.py`

- [ ] **Step 1: Add estimation tests**

Append to `tests/test_aoi_splitter.py`:

```python
import pytest

# ── estimate_gee_bytes ────────────────────────────────────────────────────────

def test_estimate_gee_bytes_returns_int():
    result = splitter.estimate_gee_bytes([0.0, 0.0, 0.2, 0.2])
    assert isinstance(result, int)
    assert result > 0


def test_estimate_gee_bytes_larger_area_is_more():
    small = splitter.estimate_gee_bytes([0.0, 0.0, 0.1, 0.1])
    large = splitter.estimate_gee_bytes([0.0, 0.0, 0.2, 0.2])
    assert large > small


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
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_aoi_splitter.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_aoi_splitter.py
git commit -m "test: add estimate_gee_bytes and estimate_openeo_pu tests"
```

---

### Task 4: Refactor `_on_aoi_drawn` → `_add_drawn_aoi_to_queue` + `_add_tile_aoi_to_queue`

**Files:**
- Modify: `ui/main_dialog.py` — around line 1039

- [ ] **Step 1: Add `_add_drawn_aoi_to_queue` and `_add_tile_aoi_to_queue` methods**

In `ui/main_dialog.py`, after the `_on_aoi_drawn` method (around line 1066), add these two new methods:

```python
def _add_drawn_aoi_to_queue(self, wkt: str, rect) -> None:
    """Create a tmp_ AOI entry from a drawn rect and add it to the queue."""
    import uuid as _uuid
    aoi_id = "tmp_" + _uuid.uuid4().hex[:8]
    bbox = [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]
    name = f"Drawn AOI {len(self._queue) + 1}"
    aoi_entry = {
        "id": aoi_id,
        "name": name,
        "wkt": wkt,
        "bbox": bbox,
        "tag": "drawn",
        "checked": True,
    }
    self._add_to_queue(aoi_entry)

def _add_tile_aoi_to_queue(self, tile_bbox: list, tile_index: int) -> None:
    """Create a tmp_ AOI entry from a tile bbox and add it to the queue."""
    import uuid as _uuid
    west, south, east, north = tile_bbox
    rect = QgsRectangle(west, south, east, north)
    geom = QgsGeometry.fromRect(rect)
    wkt = geom.asWkt()
    aoi_id = "tmp_" + _uuid.uuid4().hex[:8]
    aoi_entry = {
        "id": aoi_id,
        "name": f"Tile {tile_index}",
        "wkt": wkt,
        "bbox": list(tile_bbox),
        "tag": "drawn",
        "checked": True,
    }
    self._add_to_queue(aoi_entry)
```

- [ ] **Step 2: Rewrite `_on_aoi_drawn` to use the new helpers**

Replace the existing `_on_aoi_drawn` method (lines 1039–1066) with:

```python
def _on_aoi_drawn(self, wkt, rect):
    if wkt is None or rect is None:
        self.iface.messageBar().pushMessage(
            "PWTT", "Please draw a rectangle with non-zero area.",
            level=Qgis.Warning, duration=5,
        )
        try:
            self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        except Exception:
            pass
        return

    try:
        self.iface.mapCanvas().setMapTool(self._previous_map_tool)
    except Exception:
        pass

    backend_id = self.backend_combo.currentData()
    bbox = [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]

    from ..core import aoi_splitter
    if aoi_splitter.needs_split(bbox, backend_id):
        dlg = _AoiSplitDialog(self, bbox, backend_id, self.iface.mapCanvas())
        action = dlg.exec()
        if action == "cancel":
            return
        elif action == "single":
            self._add_drawn_aoi_to_queue(wkt, rect)
        else:  # "tiles"
            for i, tile_bbox in enumerate(dlg.confirmed_tiles(), start=1):
                self._add_tile_aoi_to_queue(tile_bbox, i)
    else:
        self._add_drawn_aoi_to_queue(wkt, rect)
```

- [ ] **Step 3: Verify existing tests still pass**

```bash
python -m pytest tests/ -v
```

Expected: all existing tests pass (no regressions).

- [ ] **Step 4: Commit**

```bash
git add ui/main_dialog.py
git commit -m "refactor: extract _add_drawn_aoi_to_queue and _add_tile_aoi_to_queue from _on_aoi_drawn"
```

---

### Task 5: Add `_queue_warning_label` to the queue section

**Files:**
- Modify: `ui/main_dialog.py` — `_build_ui` (around line 401) and `_update_queue_label` (line 993)

- [ ] **Step 1: Add the warning label widget in `_build_ui`**

In `_build_ui`, locate this block (around line 401):

```python
        self.queue_label = QLabel("Queue  (0 selected)")
        self.queue_label.setStyleSheet("font-weight: bold;")
        aoi_outer.addWidget(self.queue_label)

        self.queue_list = QListWidget()
```

Replace it with:

```python
        self.queue_label = QLabel("Queue  (0 selected)")
        self.queue_label.setStyleSheet("font-weight: bold;")
        aoi_outer.addWidget(self.queue_label)

        self._queue_warning_label = QLabel(
            "⚠ Large batch — check API quota before running."
        )
        self._queue_warning_label.setStyleSheet("color: #b85c00; font-size: 0.9em;")
        self._queue_warning_label.setVisible(False)
        aoi_outer.addWidget(self._queue_warning_label)

        self.queue_list = QListWidget()
```

- [ ] **Step 2: Update `_update_queue_label` to show/hide the warning**

Locate `_update_queue_label` (line 993):

```python
    def _update_queue_label(self):
        selected = sum(1 for a in self._queue if a.get("checked", True))
        self.queue_label.setText(f"Queue  ({selected} selected)")
```

Replace with:

```python
    def _update_queue_label(self):
        selected = sum(1 for a in self._queue if a.get("checked", True))
        self.queue_label.setText(f"Queue  ({selected} selected)")
        self._queue_warning_label.setVisible(selected >= 4)
```

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: add queue warning label for large batches (>=4 AOIs)"
```

---

### Task 6: Update `_BatchConfirmDialog` to warn when N ≥ 4

**Files:**
- Modify: `ui/main_dialog.py` — `_BatchConfirmDialog.__init__` (lines 67–111)

- [ ] **Step 1: Insert warning block into `_BatchConfirmDialog.__init__`**

Locate this block in `_BatchConfirmDialog.__init__` (around line 78):

```python
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        outer.addWidget(QLabel(f"<b>AOIs to run ({len(aois)}):</b>"))
```

Replace with:

```python
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        if len(aois) >= 4:
            warn_frame = QFrame()
            warn_frame.setStyleSheet(
                "background-color: #fff3cd; border: 1px solid #ffc107; border-radius: 4px;"
            )
            warn_layout = QVBoxLayout(warn_frame)
            warn_layout.setContentsMargins(8, 6, 8, 6)
            warn_text = QLabel(
                f"⚠  <b>{len(aois)} jobs queued</b> — this may take a long time and consume "
                "a significant portion of your monthly API quota."
            )
            warn_text.setWordWrap(True)
            warn_layout.addWidget(warn_text)
            # Dashboard link — shown for openEO backend (parent is PWTTControlsDock)
            try:
                backend_id = parent.backend_combo.currentData()
            except AttributeError:
                backend_id = None
            if backend_id == "openeo":
                link = QLabel(
                    '<a href="https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings">'
                    "Check CDSE balance ↗</a>"
                )
                link.setOpenExternalLinks(True)
                warn_layout.addWidget(link)
            outer.addWidget(warn_frame)

        outer.addWidget(QLabel(f"<b>AOIs to run ({len(aois)}):</b>"))
```

- [ ] **Step 2: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: add quota warning to batch confirm dialog for >=4 jobs"
```

---

### Task 7: Implement `_AoiSplitDialog`

**Files:**
- Modify: `ui/main_dialog.py` — insert new class between `_BatchConfirmDialog` and `PWTTControlsDock` (around line 130)

- [ ] **Step 1: Insert `_AoiSplitDialog` class**

In `ui/main_dialog.py`, locate the blank line between `_BatchConfirmDialog` and `PWTTControlsDock` (line 130):

```python
        ]


class PWTTControlsDock(QDockWidget):
```

Insert the new class before `PWTTControlsDock`:

```python
        ]


class _AoiSplitDialog:
    """
    Shown when a drawn AOI exceeds the backend's per-job size limit.
    Lets the user preview tile grid on map, adjust overlap, then confirm.

    exec() returns "tiles", "single", or "cancel".
    confirmed_tiles() returns list of [west, south, east, north] bboxes.
    """

    TILES  = "tiles"
    SINGLE = "single"
    CANCEL = "cancel"

    def __init__(self, parent, bbox: list, backend_id: str, canvas):
        from ..core import aoi_splitter
        from .dock_common import BACKENDS
        self._bbox       = bbox
        self._backend_id = backend_id
        self._canvas     = canvas
        self._preview_bands: list = []
        self._confirmed_tiles: list = []
        self._action = self.CANCEL

        backend_name = next((n for bid, n in BACKENDS if bid == backend_id), backend_id)

        self._dialog = QDialog(parent)
        self._dialog.setWindowTitle(f"PWTT — AOI too large for {backend_name}")
        self._dialog.setMinimumWidth(520)
        self._dialog.finished.connect(self._on_dialog_finished)

        outer = QVBoxLayout(self._dialog)

        # ── Info header ──────────────────────────────────────────────────────
        west, south, east, north = bbox
        width  = east - west
        height = north - south
        cols, rows = aoi_splitter.tile_grid_dims(bbox, backend_id)
        n_tiles = cols * rows

        self._info_label = QLabel()
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("color: #b85c00; font-weight: bold;")
        outer.addWidget(self._info_label)

        # ── Overlap control ───────────────────────────────────────────────────
        overlap_row = QHBoxLayout()
        overlap_row.addWidget(QLabel("Tile overlap:"))
        self._overlap_spin = QDoubleSpinBox()
        self._overlap_spin.setRange(0.0, 0.1)
        self._overlap_spin.setSingleStep(0.001)
        self._overlap_spin.setDecimals(3)
        self._overlap_spin.setValue(0.01)
        self._overlap_spin.setSuffix("°")
        self._overlap_spin.valueChanged.connect(self._on_overlap_changed)
        overlap_row.addWidget(self._overlap_spin)
        overlap_row.addWidget(QLabel("  (extends each tile edge outward)"))
        overlap_row.addStretch()
        outer.addLayout(overlap_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        # ── Quota section ────────────────────────────────────────────────────
        outer.addWidget(QLabel("<b>Quota / processing time</b>"))
        self._quota_label = QLabel()
        self._quota_label.setWordWrap(True)
        outer.addWidget(self._quota_label)

        if backend_id == "openeo":
            link_label = QLabel(
                "Free tier: 10,000 PU/month  "
                '<a href="https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings">'
                "Check balance ↗</a>"
            )
            link_label.setOpenExternalLinks(True)
            outer.addWidget(link_label)

        warn_lbl = QLabel(
            "Running multiple jobs will take significantly longer than a single job.\n"
            "Large batches may exhaust your monthly API quota."
        )
        warn_lbl.setWordWrap(True)
        warn_lbl.setStyleSheet("color: #666;")
        outer.addWidget(warn_lbl)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep2)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._add_tiles_btn = QPushButton()
        self._add_tiles_btn.clicked.connect(self._on_add_tiles)
        btn_row.addWidget(self._add_tiles_btn)
        add_single_btn = QPushButton("Add as single AOI")
        add_single_btn.clicked.connect(self._on_add_single)
        btn_row.addWidget(add_single_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

        self._refresh_labels()
        self._draw_preview()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _current_tiles(self) -> list:
        from ..core import aoi_splitter
        return aoi_splitter.split_bbox(
            self._bbox, self._backend_id, self._overlap_spin.value()
        )

    def _refresh_labels(self):
        from ..core import aoi_splitter
        tiles  = self._current_tiles()
        n      = len(tiles)
        cols, rows = aoi_splitter.tile_grid_dims(self._bbox, self._backend_id)
        west, south, east, north = self._bbox
        width  = east - west
        height = north - south

        self._info_label.setText(
            f"⚠  This area ({width:.2f}° × {height:.2f}°) exceeds the backend per-job limit.\n"
            f"It has been split into a {cols} × {rows} grid ({n} tiles)."
        )
        self._add_tiles_btn.setText(f"Add {n} tiles to queue")

        if self._backend_id == "gee" and tiles:
            per_mb  = aoi_splitter.estimate_gee_bytes(tiles[0]) / (1024 * 1024)
            lim_mb  = aoi_splitter.GEE_GETDOWNLOAD_MAX_BYTES / (1024 * 1024)
            self._quota_label.setText(
                f"Estimated per tile: ~{per_mb:.0f} MiB  (GEE limit: {lim_mb:.0f} MiB)"
            )
        elif self._backend_id == "openeo" and tiles:
            per_pu   = aoi_splitter.estimate_openeo_pu(tiles[0])
            total_pu = per_pu * n
            self._quota_label.setText(
                f"Estimated per tile: ~{per_pu:.0f} PU  |  Total: ~{total_pu:.0f} PU"
            )
        else:
            self._quota_label.setText("")

    def _draw_preview(self):
        self._clear_preview()
        # Import here to avoid circular dependency at module level
        from .main_dialog import PWTTControlsDock  # noqa: F401 — only for colour palette
        colours = [
            (255, 100,   0),
            ( 30, 120, 255),
            ( 50, 180,  50),
            (180,  50, 180),
            (220, 180,   0),
        ]
        src_crs    = QgsCoordinateReferenceSystem("EPSG:4326")
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        for i, tile_bbox in enumerate(self._current_tiles()):
            west, south, east, north = tile_bbox
            rect = QgsRectangle(west, south, east, north)
            geom = QgsGeometry.fromRect(rect)
            if canvas_crs != src_crs:
                transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
                geom.transform(transform)
            r, g, b = colours[i % len(colours)]
            rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            rb.setColor(QColor(r, g, b, 30))
            rb.setStrokeColor(QColor(r, g, b, 180))
            rb.setWidth(2)
            rb.setLineStyle(Qt.DashLine)
            rb.setToGeometry(geom, None)
            self._preview_bands.append(rb)

    def _clear_preview(self):
        for rb in self._preview_bands:
            rb.reset(QgsWkbTypes.PolygonGeometry)
        self._preview_bands.clear()

    def _on_overlap_changed(self, _value):
        self._refresh_labels()
        self._draw_preview()

    def _on_add_tiles(self):
        self._confirmed_tiles = self._current_tiles()
        self._action = self.TILES
        self._clear_preview()
        self._dialog.accept()

    def _on_add_single(self):
        self._action = self.SINGLE
        self._clear_preview()
        self._dialog.accept()

    def _on_cancel(self):
        self._action = self.CANCEL
        self._clear_preview()
        self._dialog.reject()

    def _on_dialog_finished(self, _result):
        # Safety net: clear preview if dialog closed via window X button
        self._clear_preview()

    # ── Public API ────────────────────────────────────────────────────────────

    def exec(self) -> str:
        """Show dialog. Returns "tiles", "single", or "cancel"."""
        self._dialog.exec_()
        return self._action

    def confirmed_tiles(self) -> list:
        """List of [west, south, east, north] bboxes confirmed by user."""
        return self._confirmed_tiles


class PWTTControlsDock(QDockWidget):
```

- [ ] **Step 2: Fix the self-referential import in `_draw_preview`**

The `_draw_preview` method above has a circular import (`from .main_dialog import PWTTControlsDock`). Replace the colour palette import with an inline definition (the colours are already in the snippet above as a local `colours` list — that's correct, no import needed). Verify the method body uses `colours` directly:

```python
    def _draw_preview(self):
        self._clear_preview()
        colours = [
            (255, 100,   0),
            ( 30, 120, 255),
            ( 50, 180,  50),
            (180,  50, 180),
            (220, 180,   0),
        ]
        src_crs    = QgsCoordinateReferenceSystem("EPSG:4326")
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        for i, tile_bbox in enumerate(self._current_tiles()):
            west, south, east, north = tile_bbox
            rect = QgsRectangle(west, south, east, north)
            geom = QgsGeometry.fromRect(rect)
            if canvas_crs != src_crs:
                transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
                geom.transform(transform)
            r, g, b = colours[i % len(colours)]
            rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            rb.setColor(QColor(r, g, b, 30))
            rb.setStrokeColor(QColor(r, g, b, 180))
            rb.setWidth(2)
            rb.setLineStyle(Qt.DashLine)
            rb.setToGeometry(geom, None)
            self._preview_bands.append(rb)
```

(The `from .main_dialog import PWTTControlsDock` line in the Step 1 snippet was a mistake — remove it. The corrected version above is correct.)

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: add _AoiSplitDialog with map preview and quota estimates"
```

---

### Task 8: Final check + manual smoke test

- [ ] **Step 1: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Verify imports are clean**

```bash
python -c "
import sys, types
# stub qgis
for m in ['qgis','qgis.core','qgis.gui','qgis.PyQt','qgis.PyQt.QtWidgets',
          'qgis.PyQt.QtCore','qgis.PyQt.QtGui']:
    sys.modules[m] = types.ModuleType(m)
import core.aoi_splitter as s
print('needs_split 1x1 openeo:', s.needs_split([0,0,1,1],'openeo'))
print('tile_grid_dims 1x1 openeo:', s.tile_grid_dims([0,0,1,1],'openeo'))
tiles = s.split_bbox([0,0,1,1],'openeo',overlap_deg=0.01)
print('tile count:', len(tiles))
print('first tile:', tiles[0])
"
```

Expected output:
```
needs_split 1x1 openeo: True
tile_grid_dims 1x1 openeo: (2, 2)
tile count: 4
first tile: [-0.01, 0.49, 0.51, 1.01]
```

- [ ] **Step 3: Manual QGIS test checklist**

Load the plugin in QGIS and verify:

1. Draw a small AOI (< 0.5° × 0.5° with openEO backend) → added to queue directly, no dialog.
2. Draw a large AOI (> 0.5° wide or tall with openEO) → `_AoiSplitDialog` appears.
3. Tile outlines appear as dashed rubber bands on map.
4. Changing overlap spinbox redraws rubber bands.
5. Click **Add N tiles to queue** → tiles appear in queue with names "Tile 1", "Tile 2", etc.
6. Click **Add as single AOI** → single entry in queue, all preview bands removed.
7. Click **Cancel** → nothing added, all preview bands removed.
8. Queue with ≥ 4 checked items → `⚠ Large batch` label visible below queue header.
9. Queue with < 4 items → warning label hidden.
10. Batch confirm dialog with ≥ 4 AOIs → yellow warning box visible.
11. Batch confirm dialog with openEO and ≥ 4 AOIs → "Check CDSE balance ↗" link present.
12. Switch to GEE backend, draw an oversized AOI → dialog shows MiB estimate (not PU), no dashboard link.

- [ ] **Step 4: Final commit**

```bash
git add -u
git commit -m "feat: AOI large-area split into tiles with quota warnings"
```
