# AOI Large-Area Split into Tiles — Design Spec

**Date:** 2026-04-12  
**Status:** Approved

---

## Overview

When a user draws an AOI that exceeds the selected backend's per-job processing limit, the plugin automatically detects this and prompts the user to split the large AOI into a uniform grid of smaller tiles. Each tile becomes an independent job in the queue. A quota/time warning is shown in three places: the split dialog, the batch confirmation dialog (when N ≥ 4 jobs), and a persistent label in the queue section.

---

## Trigger Conditions

On completion of every map draw (`_on_aoi_drawn()`), the drawn bbox is checked against the currently selected backend's per-job limit via `aoi_splitter.needs_split(bbox, backend_id)`. If the bbox exceeds the limit, `_AoiSplitDialog` is opened immediately.

---

## Backend Tile Limits

| Backend | Max tile width | Max tile height | Basis |
|---|---|---|---|
| `gee` | back-calculated | back-calculated | `GEE_GETDOWNLOAD_MAX_BYTES` (48 MiB) at bbox mid-latitude |
| `openeo` | 0.5° | 0.5° | CDSE-tested ~100×100 km; conservative for free tier (10 000 PU/month) |
| `local` | 1.0° | 1.0° | No hard limit; large sensible default |

For GEE the max tile dimensions in degrees are derived by inverting `estimate_gee_getdownload_request_bytes()`: find the largest square bbox (at the drawn area's mid-latitude) whose estimated bytes ≤ `GEE_GETDOWNLOAD_MAX_BYTES`.

---

## Core Module: `core/aoi_splitter.py`

New file. Pure geometry and estimation math — no Qt imports. Follows the `aoi_store.py` / `gee_backend.py` pattern.

### Public API

```python
def needs_split(bbox: list, backend_id: str) -> bool:
    """True if bbox exceeds the backend's per-job limit."""

def split_bbox(
    bbox: list,
    backend_id: str,
    overlap_deg: float = 0.01,
) -> list[list]:
    """Return list of [west, south, east, north] tile bboxes covering bbox.

    Tiles are uniform: the bbox is divided evenly into cols × rows cells
    (not just max-size chunks), so all tiles are the same size.
    Each tile is expanded outward by overlap_deg on all sides.
    """

def tile_grid_dims(bbox: list, backend_id: str) -> tuple[int, int]:
    """Return (cols, rows) for the split grid (no overlap applied)."""

def estimate_gee_bytes(bbox: list) -> int:
    """Estimated uncompressed download bytes for a GEE job over this bbox.
    Delegates to gee_backend.estimate_gee_getdownload_request_bytes()."""

def estimate_openeo_pu(bbox: list) -> float:
    """Estimated PU cost for an openEO batch job over this bbox.
    Formula: (width_px * height_px / (512*512)) * bands * float32_multiplier.
    Assumes S1/S2 at 10 m resolution, 3 bands, float32 (2× multiplier)."""
```

### Split algorithm

1. Call `tile_grid_dims()` → `(cols, rows)`:
   - `tile_w = max_tile_width(backend_id, mid_lat)` — backend-specific
   - `cols = ceil((east - west) / tile_w)`
   - `rows = ceil((north - south) / tile_h)`
2. Divide bbox evenly: `cell_w = (east - west) / cols`, `cell_h = (north - south) / rows`
3. For each `(c, r)` in `cols × rows`, compute cell bbox, then expand each edge by `overlap_deg`.
4. Return list of `[west, south, east, north]` floats.

### Overlap

Default `overlap_deg = 0.01` (~1.1 km at mid-latitudes). Exposed as a spinbox in the dialog. Each tile edge extends outward by this amount — tiles intentionally overlap their neighbours to avoid processing boundary artefacts.

---

## UI: `_AoiSplitDialog` in `ui/main_dialog.py`

Single-use dialog class, following the `_BatchConfirmDialog` pattern.

### Constructor

```python
_AoiSplitDialog(parent, bbox: list, backend_id: str, canvas)
```

### Layout

```
┌─ PWTT — AOI too large for [Backend] ──────────────────────────────────┐
│ ⚠  This area (2.4° × 1.8°) exceeds the [Backend] per-job limit.       │
│    It has been split into a 3 × 2 grid (6 tiles).                     │
│                                                                        │
│    Tile overlap:  [0.010]°   (extends each tile edge outward)          │
│                                                                        │
│ ── Quota / processing time ──────────────────────────────────────      │
│    Estimated per tile:  ~38 MiB  (GEE limit: 48 MiB)                  │
│    — or (openEO) —                                                     │
│    Estimated per tile:  ~420 PU  |  Total: ~2 520 PU                  │
│    Free tier: 10 000 PU/month  [Check balance ↗]                      │
│                                                                        │
│    Running N jobs will take significantly longer than a single job.    │
│    Large batches may exhaust your monthly API quota.                   │
│                                                                        │
│       [ Add 6 tiles to queue ]   [ Add as single AOI ]   [ Cancel ]   │
└────────────────────────────────────────────────────────────────────────┘
```

### Map preview rubber bands

- On open: all tile outlines are drawn on the canvas using dashed `QgsRubberBand` lines (not stored in `self._rubber_bands` — preview-only, held in `self._preview_bands: list[QgsRubberBand]`).
- Colour palette cycles through `_AOI_COLOURS` (same as queue rubber bands).
- When the overlap spinbox changes: tiles are recomputed, all preview bands removed and redrawn, labels updated.
- On **Add tiles**: preview bands removed; tiles added to queue (queue draws its own solid rubber bands via `_add_tile_aoi_to_queue()`).
- On **Add as single AOI** or **Cancel**: preview bands removed.

### Return values

`exec()` returns one of three string constants:
- `"tiles"` — user confirmed tiling; call `dlg.confirmed_tiles()` for the list
- `"single"` — user chose to add as single AOI
- `"cancel"` — user cancelled (or closed dialog)

`confirmed_tiles()` returns `list[list]` of `[west, south, east, north]` bboxes.

### "Check balance ↗" link

Shown for `openeo` backend only. Opens `https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings` via `webbrowser.open()`. Not shown for GEE or local.

---

## Integration: `_on_aoi_drawn()` refactor

```python
def _on_aoi_drawn(self, wkt, rect):
    # ... existing empty-rect validation ...

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
            for tile_bbox in dlg.confirmed_tiles():
                self._add_tile_aoi_to_queue(tile_bbox)
    else:
        self._add_drawn_aoi_to_queue(wkt, rect)
```

**`_add_drawn_aoi_to_queue(wkt, rect)`** — extracts the existing inline AOI-creation block from `_on_aoi_drawn()`. No behaviour change.

**`_add_tile_aoi_to_queue(bbox)`** — builds an AOI entry from `[W, S, E, N]`, generates WKT via `QgsGeometry.fromRect()`, assigns a `tmp_` id and a name like `"Tile 1"`, `"Tile 2"`, etc. (numbered left-to-right, top-to-bottom across the grid).

---

## Warnings

### 1. Split dialog

Always shown in the dialog body when `_AoiSplitDialog` is opened. Content adapts per backend:
- GEE: shows estimated MiB per tile vs 48 MiB limit.
- openEO: shows estimated PU per tile, total PU, free tier limit, and dashboard link.
- local: shows estimated tile count and processing time note only.

### 2. `_BatchConfirmDialog` (N ≥ 4 jobs)

A yellow `QLabel` warning block is inserted between the summary text and the AOI checklist when `len(aois) >= 4`:

```
⚠  N jobs queued — this may take a long time and consume a significant
   portion of your monthly API quota.
   [Check CDSE balance ↗]    ← openEO only
```

The warning is built once at dialog construction; the dashboard link uses `webbrowser.open()`.

### 3. Queue section persistent label

`self._queue_warning_label: QLabel` placed immediately below `self.queue_label`. Updated in `_update_queue_buttons()` (already called on every queue change):

```
⚠ Large batch — check API quota before running.
```

Visible when the count of checked AOIs ≥ 4. Hidden otherwise.

---

## Architecture Summary

### New files

| File | Purpose |
|---|---|
| `core/aoi_splitter.py` | Tiling math, size estimation (GEE bytes, openEO PU) |
| `tests/test_aoi_splitter.py` | Unit tests for splitter math |

### Modified files

| File | Changes |
|---|---|
| `ui/main_dialog.py` | Add `_AoiSplitDialog`; refactor `_on_aoi_drawn()` into `_add_drawn_aoi_to_queue()` + `_add_tile_aoi_to_queue()`; add `_queue_warning_label`; update `_BatchConfirmDialog` to show warning at N ≥ 4 |

### No changes

- `core/aoi_store.py` — tile AOIs use same `tmp_` flow as drawn AOIs
- `core/gee_backend.py` — `estimate_gee_getdownload_request_bytes()` reused, not modified
- `ui/aoi_tool.py` — unchanged
- `core/job_store.py` — unchanged
- `ui/jobs_dock.py` — unchanged

---

## Backward Compatibility

- Drawing a small AOI (below backend limit) follows the existing path exactly — no behaviour change.
- Single-AOI runs are unaffected.
- Tile AOIs use the same `tmp_` session-only AOI flow as drawn AOIs — they can be saved to library via the existing Save button on each queue row.
