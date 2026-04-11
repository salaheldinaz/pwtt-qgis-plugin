# AOI Save / Load / Multi-AOI Batch Run — Design Spec

**Date:** 2026-04-11  
**Status:** Approved

---

## Overview

Three related features added to the PWTT QGIS plugin's AOI section:

1. **Save AOI** — persist a named AOI to a local library for reuse across sessions.
2. **Load saved AOI into run queue** — select one or more saved AOIs to include in the next run.
3. **Multiple AOIs → multiple jobs** — draw or select multiple AOIs; each produces an independent job with shared parameters.

---

## Data Model

### Saved AOI record

Stored in `PWTT/saved_aois.json` inside the QGIS profile directory (same location as `jobs.json`).

```json
{
  "id": "a1b2c3d4",
  "name": "Kyiv north",
  "wkt": "Polygon ((...))",
  "bbox": [west, south, east, north],
  "created_at": "2026-04-11T14:32:00"
}
```

- `id` — `uuid.uuid4().hex[:8]`, auto-generated, globally unique.
- `name` — user-defined string, not required to be unique (id disambiguates).
- `wkt` — EPSG:4326 WKT polygon (axis-aligned bbox).
- `bbox` — `[west, south, east, north]` floats for quick display without parsing WKT.
- `created_at` — ISO-8601 timestamp, seconds precision.

### Pending (session-only) AOI

Same fields, but `id` is prefixed `"tmp_"`. Held in `self._pending_aois: list[dict]` on the dock. Never written to disk unless the user explicitly saves it via the **Save** button on the queue row.

### Export envelope

```json
{
  "format": "pwtt_aois_export",
  "version": 1,
  "exported_at": "...",
  "aois": [ ... ]
}
```

Raw JSON arrays (no envelope) are also accepted on import for forward compatibility.

---

## Storage Module

**New file:** `core/aoi_store.py`

Mirrors the `job_store.py` pattern. Public API:

| Function | Description |
|---|---|
| `load_aois() -> list[dict]` | Read all saved AOIs from disk. |
| `save_aoi(aoi: dict)` | Insert or update by id. |
| `delete_aoi(aoi_id: str)` | Remove by id. |
| `export_aois_to_file(path: str) -> int` | Write export envelope; returns count. |
| `import_aois_from_file(path: str) -> dict` | Merge from file; returns `{added, skipped_invalid, ids_rewritten}`. |

Storage path: `QgsApplication.qgisSettingsDirPath() / "PWTT" / "saved_aois.json"`.

---

## UI Layout

The existing `"Area of interest"` `QGroupBox` is replaced with two stacked sub-sections inside the same group box.

### Sub-section 1 — Run Queue (always visible)

```
[ Draw rectangle on map ]

Queue  (N selected)
┌─────────────────────────────────────────────────────┐
│ ☑  Kyiv north          [drawn]   [Save] [Remove]   │
│ ☑  Mariupol port       [saved]         [Remove]    │
│ ☑  Kherson city        [saved]         [Remove]    │
└─────────────────────────────────────────────────────┘
[ Clear queue ]   [ Hide all on map / Show all ]
```

- Implemented as a `QListWidget` with custom item widgets.
- Each row has a checkbox (checked = included in next run), a name label, a tag (`[drawn]` or `[saved]`), and action buttons.
- **Draw rectangle on map** — activates `PWTTMapToolExtent`; on completion adds a new `tmp_`-prefixed pending AOI to the queue (checked by default).
- **Save** (drawn rows only) — prompts for a name via `QInputDialog`, saves to library via `aoi_store.save_aoi()`, updates the row tag to `[saved]` and replaces the `tmp_` id with the real id.
- **Remove** — removes from queue; does not delete from library.
- **Clear queue** — removes all queue rows.
- **Hide all on map / Show all** — toggles visibility of all rubber bands.

The queue label ("Queue (N selected)") updates live as checkboxes are toggled.

### Sub-section 2 — Saved AOI Library (collapsible, collapsed by default)

```
▶  Saved AOI Library  (5 saved)
┌─────────────────────────────────────────────────────┐
│   Kyiv north          2026-04-11                    │
│   Mariupol port       2026-04-10                    │
│   Kherson city        2026-03-28                    │
└─────────────────────────────────────────────────────┘
[ Load into queue ]  [ Rename ]  [ Delete ]
[ Export… ]  [ Import… ]
```

- Implemented as a `QListWidget` (single-selection for Rename/Delete, multi-selection for Load).
- **Load into queue** — adds selected saved AOI(s) to the run queue (checked by default). Does not duplicate if the same id is already in the queue.
- **Rename** — `QInputDialog`; updates `saved_aois.json` and any matching queue row label.
- **Delete** — removes from library only; does not remove from queue if already loaded.
- **Export…** — `QFileDialog` save → `aoi_store.export_aois_to_file()`.
- **Import…** — `QFileDialog` open → `aoi_store.import_aois_from_file()`; refreshes library list.

The collapse toggle button shows the count: `▶  Saved AOI Library  (5 saved)`.

---

## Rubber Band Management

Replace the current single `self._rubber_band: QgsRubberBand` with:

```python
self._rubber_bands: dict[str, QgsRubberBand]  # keyed by AOI id (including tmp_ ids)
```

A fixed colour palette cycles across queued AOIs:

```python
_AOI_COLOURS = [
    QColor(255, 100,   0, 180),  # orange  (current default)
    QColor( 30, 120, 255, 180),  # blue
    QColor( 50, 180,  50, 180),  # green
    QColor(180,  50, 180, 180),  # purple
    QColor(220, 180,   0, 180),  # amber
]
```

Each AOI in the queue gets the next colour in the palette (modulo length). Rubber bands are created when an AOI is added to the queue and removed when it is removed or the queue is cleared. All rubber bands are cleared after a successful batch launch.

---

## Run Flow

### Validation

If no AOIs are checked in the queue → warning message box (same wording as current "no AOI set").

### Batch Confirmation Dialog

A new `_BatchConfirmDialog(QDialog)` class defined in `main_dialog.py`:

- Header: the existing run summary text (backend, dates, parameters).
- AOI list: one `QCheckBox` per checked queue item, all pre-checked.
- Footer buttons: **Run N jobs** / **Cancel**.
- The "N" in the button label updates live as the user toggles checkboxes in the dialog.
- Clicking **Run N jobs** returns the confirmed subset; **Cancel** returns an empty list.

### Job Creation Loop

```python
confirmed_aois = batch_confirm_dialog.confirmed_aois()
for aoi in confirmed_aois:
    job = job_store.create_job(aoi_wkt=aoi["wkt"], ...)
    job["output_dir"] = os.path.join(base_dir, job["id"])
    os.makedirs(job["output_dir"], exist_ok=True)
    job_store.save_job(job)
    jobs_dock.launch_job(job, backend)
```

All jobs share the same backend, dates, parameters, and output base directory. Each gets its own `output_dir = base_dir / job_id`.

### Post-Run

- All successfully launched queue rows are removed from the queue.
- Pending (`tmp_`) AOIs that were launched are discarded (not auto-saved).
- Saved AOIs that were loaded into the queue are removed from the queue but remain in the library.
- All rubber bands are cleared.

---

## Architecture Summary

### New files

| File | Purpose |
|---|---|
| `core/aoi_store.py` | Persistent AOI CRUD + export/import |

### Modified files

| File | Changes |
|---|---|
| `ui/main_dialog.py` | Replace single-AOI section with queue + library sub-sections; add `_pending_aois`, `_rubber_bands`; update `_run()` to loop over confirmed AOIs; add `_BatchConfirmDialog` |

### Removed state

| Attribute | Replacement |
|---|---|
| `self.aoi_wkt` | `self._pending_aois` list + library |
| `self.aoi_rect` | stored per-AOI in queue entries |
| `self._aoi_map_visible` | per-AOI rubber band visibility toggled via `_rubber_bands` |
| `self._rubber_band` | `self._rubber_bands: dict[str, QgsRubberBand]` |

### No changes

- `ui/aoi_tool.py` — unchanged; still emits `(wkt, rect)` via callback.
- `core/job_store.py` — unchanged; job schema unchanged.
- `ui/jobs_dock.py` — unchanged; `launch_job()` called per AOI as before.
- `QgsSettings` AOI persistence — none (queue is intentionally session-only).

---

## Backward Compatibility

- Existing single-AOI run is a special case of the batch loop (N=1).
- No changes to `jobs.json` schema.
- `aoi_store.py` creates `saved_aois.json` on first save; missing file → empty library (no error).
