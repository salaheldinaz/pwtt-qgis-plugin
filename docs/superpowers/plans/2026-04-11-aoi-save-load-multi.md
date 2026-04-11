# AOI Save / Load / Multi-AOI Batch Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-AOI section in `PWTTControlsDock` with a run queue + saved library that lets users persist named AOIs, select multiple, draw extras, and launch one job per AOI.

**Architecture:** A new `core/aoi_store.py` handles JSON persistence (mirrors `job_store.py`). `ui/main_dialog.py` replaces the single-`aoi_wkt` state with `self._queue: list[dict]` (session-only) and a `self._rubber_bands: dict[str, QgsRubberBand]`. A new `_BatchConfirmDialog` replaces the `QMessageBox.question` confirmation; `_run()` loops over confirmed AOIs to create one job each.

**Tech Stack:** Python 3, PyQt5 (via QGIS `qgis.PyQt`), QGIS API (`QgsSettings`, `QgsRubberBand`, `QgsRectangle`, `QgsGeometry`), `json`, `uuid`, `os`

---

## File Map

| Action | File | Purpose |
|---|---|---|
| Create | `core/aoi_store.py` | AOI CRUD + export/import (no QGIS UI deps) |
| Create | `tests/test_aoi_store.py` | Unit tests for aoi_store (pure Python) |
| Modify | `ui/main_dialog.py` | Replace AOI section; add queue+library UI; batch run |

`ui/aoi_tool.py` — **untouched**. `core/job_store.py` — **untouched**. `ui/jobs_dock.py` — **untouched**.

---

## Task 1: `core/aoi_store.py` — storage module

**Files:**
- Create: `core/aoi_store.py`
- Create: `tests/test_aoi_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_aoi_store.py`:

```python
# tests/test_aoi_store.py
import json
import os
import sys
import types
import pytest

# Stub out qgis.core so aoi_store can be imported without a running QGIS instance
qgis_core = types.ModuleType("qgis.core")
class _FakeApp:
    @staticmethod
    def qgisSettingsDirPath():
        return "/tmp/fake_qgis"
qgis_core.QgsApplication = _FakeApp
sys.modules.setdefault("qgis", types.ModuleType("qgis"))
sys.modules["qgis.core"] = qgis_core

# Point the store at a temp directory
import tempfile
_tmpdir = tempfile.mkdtemp()
qgis_core.QgsApplication.qgisSettingsDirPath = staticmethod(lambda: _tmpdir)

# Now import
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import importlib
import core.aoi_store as aoi_store
importlib.reload(aoi_store)  # pick up the patched path


def _fresh():
    """Delete saved_aois.json so each test starts clean."""
    p = os.path.join(_tmpdir, "PWTT", "saved_aois.json")
    if os.path.exists(p):
        os.remove(p)


def test_load_empty():
    _fresh()
    assert aoi_store.load_aois() == []


def test_save_and_load():
    _fresh()
    aoi = aoi_store.make_aoi("Test AOI", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0.0, 0.0, 1.0, 1.0])
    aoi_store.save_aoi(aoi)
    loaded = aoi_store.load_aois()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "Test AOI"
    assert loaded[0]["id"] == aoi["id"]


def test_save_updates_existing():
    _fresh()
    aoi = aoi_store.make_aoi("Original", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1])
    aoi_store.save_aoi(aoi)
    aoi["name"] = "Updated"
    aoi_store.save_aoi(aoi)
    loaded = aoi_store.load_aois()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "Updated"


def test_delete_aoi():
    _fresh()
    aoi = aoi_store.make_aoi("To delete", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1])
    aoi_store.save_aoi(aoi)
    aoi_store.delete_aoi(aoi["id"])
    assert aoi_store.load_aois() == []


def test_export_import(tmp_path):
    _fresh()
    aoi = aoi_store.make_aoi("Export me", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1])
    aoi_store.save_aoi(aoi)
    export_path = str(tmp_path / "export.json")
    count = aoi_store.export_aois_to_file(export_path)
    assert count == 1

    # Import into a clean state
    _fresh()
    result = aoi_store.import_aois_from_file(export_path)
    assert result["added"] == 1
    assert result["skipped_invalid"] == 0
    loaded = aoi_store.load_aois()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "Export me"


def test_import_no_duplicates(tmp_path):
    _fresh()
    aoi = aoi_store.make_aoi("Dup", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1])
    aoi_store.save_aoi(aoi)
    export_path = str(tmp_path / "export.json")
    aoi_store.export_aois_to_file(export_path)
    # Import again — same id already exists, should be skipped (ids_rewritten path)
    result = aoi_store.import_aois_from_file(export_path)
    # id collision → rewritten, still added
    assert result["added"] == 1
    assert len(aoi_store.load_aois()) == 2  # original + import with new id


def test_make_aoi_has_required_fields():
    aoi = aoi_store.make_aoi("X", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1])
    for field in ("id", "name", "wkt", "bbox", "created_at"):
        assert field in aoi
    assert aoi["id"]
    assert len(aoi["id"]) == 8
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Volumes/A2/Dev/Projects/pwtt-qgis-plugin
python -m pytest tests/test_aoi_store.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'core.aoi_store'`

- [ ] **Step 3: Create `core/aoi_store.py`**

```python
# core/aoi_store.py
"""Persistent AOI library: save, load, delete, export, import named AOIs."""

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List

AOI_EXPORT_FORMAT = "pwtt_aois_export"
AOI_EXPORT_VERSION = 1


def _aois_path() -> str:
    from qgis.core import QgsApplication
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "saved_aois.json")


def _read_raw() -> List[dict]:
    p = _aois_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _write(aois: List[dict]):
    with open(_aois_path(), "w", encoding="utf-8") as f:
        json.dump(aois, f, indent=2, ensure_ascii=False)


def make_aoi(name: str, wkt: str, bbox: List[float]) -> dict:
    """Return a new AOI record (not yet saved to disk)."""
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "wkt": wkt,
        "bbox": list(bbox),  # [west, south, east, north]
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_aois() -> List[dict]:
    return _read_raw()


def save_aoi(aoi: dict):
    """Insert or update by id."""
    aois = _read_raw()
    for i, a in enumerate(aois):
        if a["id"] == aoi["id"]:
            aois[i] = aoi
            _write(aois)
            return
    aois.insert(0, aoi)
    _write(aois)


def delete_aoi(aoi_id: str):
    aois = [a for a in _read_raw() if a["id"] != aoi_id]
    _write(aois)


def export_aois_to_file(path: str) -> int:
    """Write all saved AOIs to *path* as export JSON. Returns count."""
    aois = _read_raw()
    payload = {
        "format": AOI_EXPORT_FORMAT,
        "version": AOI_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "aois": aois,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(aois)


def _aois_list_from_parsed_json(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if data.get("format") == AOI_EXPORT_FORMAT and isinstance(data.get("aois"), list):
            return [x for x in data["aois"] if isinstance(x, dict)]
        if isinstance(data.get("aois"), list):
            return [x for x in data["aois"] if isinstance(x, dict)]
    raise ValueError("Unrecognized AOI file (expected PWTT AOI export or a JSON array).")


def import_aois_from_file(path: str) -> Dict[str, int]:
    """Merge AOIs from file; avoid id collisions. Returns {added, skipped_invalid, ids_rewritten}."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    incoming = _aois_list_from_parsed_json(data)

    existing = _read_raw()
    used_ids = {a["id"] for a in existing if a.get("id")}
    added = 0
    skipped_invalid = 0
    ids_rewritten = 0

    for raw in incoming:
        if not raw.get("wkt") or not raw.get("name"):
            skipped_invalid += 1
            continue
        import copy
        aoi = copy.deepcopy(raw)
        oid = aoi.get("id")
        if not oid or not isinstance(oid, str):
            aoi["id"] = uuid.uuid4().hex[:8]
        while aoi["id"] in used_ids:
            aoi["id"] = uuid.uuid4().hex[:8]
            ids_rewritten += 1
        used_ids.add(aoi["id"])
        existing.insert(0, aoi)
        added += 1

    if added:
        _write(existing)
    return {"added": added, "skipped_invalid": skipped_invalid, "ids_rewritten": ids_rewritten}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_aoi_store.py -v
```

Expected output: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/aoi_store.py tests/test_aoi_store.py
git commit -m "feat: add aoi_store — persistent named AOI library with export/import"
```

---

## Task 2: Replace AOI `__init__` state variables

**Files:**
- Modify: `ui/main_dialog.py:57-74` (`__init__`)

The old single-AOI instance variables (`aoi_wkt`, `aoi_rect`, `_aoi_map_visible`, `_rubber_band`) must be replaced by queue + rubber band dict before any UI is rebuilt.

- [ ] **Step 1: Update `__init__` state block**

In `ui/main_dialog.py`, find the block (lines ~64–70):

```python
        self.aoi_wkt = None
        self.aoi_rect = None
        self._aoi_map_visible = True
        self.map_tool = None
        self._previous_map_tool = None

        self._rubber_band = None
```

Replace with:

```python
        # AOI queue: list of dicts with keys id, name, wkt, bbox, tag ('drawn'|'saved')
        self._queue: list = []
        # Rubber bands keyed by AOI id (including tmp_ ids)
        self._rubber_bands: dict = {}
        self.map_tool = None
        self._previous_map_tool = None
```

- [ ] **Step 2: Commit**

```bash
git add ui/main_dialog.py
git commit -m "refactor: replace single-AOI state with queue + rubber_bands dict"
```

---

## Task 3: Rebuild the AOI group box in `_build_ui()`

**Files:**
- Modify: `ui/main_dialog.py` — `_build_ui()`, lines ~317–370 (the AOI group box block)

Also add the colour palette constant near the top of the class (after `_hint` staticmethod).

- [ ] **Step 1: Add colour palette constant**

Add after the `_hint` staticmethod (~line 113):

```python
    _AOI_COLOURS = [
        (255, 100,   0),   # orange (first drawn AOI)
        ( 30, 120, 255),   # blue
        ( 50, 180,  50),   # green
        (180,  50, 180),   # purple
        (220, 180,   0),   # amber
    ]
```

- [ ] **Step 2: Replace the AOI group box block in `_build_ui()`**

Find the block starting `# AOI` and ending just before `# Parameters` (roughly lines 317–370). Replace it entirely with:

```python
        # ── AOI ──────────────────────────────────────────────────────────────
        aoi_group = QGroupBox("Area of interest")
        aoi_outer = QVBoxLayout(aoi_group)

        # --- Run Queue sub-section ---
        self.draw_aoi_btn = QPushButton(QIcon(":/pwtt/icon_draw_aoi.svg"), "Draw rectangle on map")
        self.draw_aoi_btn.clicked.connect(self._activate_aoi_tool)
        aoi_outer.addWidget(self.draw_aoi_btn)

        self.queue_label = QLabel("Queue  (0 selected)")
        self.queue_label.setStyleSheet("font-weight: bold;")
        aoi_outer.addWidget(self.queue_label)

        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(80)
        self.queue_list.setAlternatingRowColors(True)
        aoi_outer.addWidget(self.queue_list)

        queue_btn_row = QHBoxLayout()
        self.clear_queue_btn = QPushButton("Clear queue")
        self.clear_queue_btn.clicked.connect(self._clear_queue)
        self.clear_queue_btn.setEnabled(False)
        queue_btn_row.addWidget(self.clear_queue_btn)
        self.toggle_all_map_btn = QPushButton("Hide all on map")
        self.toggle_all_map_btn.clicked.connect(self._toggle_all_map_visibility)
        self.toggle_all_map_btn.setEnabled(False)
        queue_btn_row.addWidget(self.toggle_all_map_btn)
        aoi_outer.addLayout(queue_btn_row)

        # --- Saved AOI Library sub-section (collapsible) ---
        self._library_toggle_btn = QPushButton("▶  Saved AOI Library  (0 saved)")
        self._library_toggle_btn.setCheckable(True)
        self._library_toggle_btn.setChecked(False)
        self._library_toggle_btn.setFlat(True)
        aoi_outer.addWidget(self._library_toggle_btn)

        self._library_widget = QWidget()
        lib_layout = QVBoxLayout(self._library_widget)
        lib_layout.setContentsMargins(0, 0, 0, 0)
        lib_layout.setSpacing(4)

        self.library_list = QListWidget()
        self.library_list.setMinimumHeight(80)
        self.library_list.setAlternatingRowColors(True)
        self.library_list.setSelectionMode(QListWidget.ExtendedSelection)
        lib_layout.addWidget(self.library_list)

        lib_btn_row1 = QHBoxLayout()
        self.lib_load_btn = QPushButton("Load into queue")
        self.lib_load_btn.clicked.connect(self._lib_load_selected)
        self.lib_load_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_load_btn)
        self.lib_rename_btn = QPushButton("Rename")
        self.lib_rename_btn.clicked.connect(self._lib_rename_selected)
        self.lib_rename_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_rename_btn)
        self.lib_delete_btn = QPushButton("Delete")
        self.lib_delete_btn.clicked.connect(self._lib_delete_selected)
        self.lib_delete_btn.setEnabled(False)
        lib_btn_row1.addWidget(self.lib_delete_btn)
        lib_layout.addLayout(lib_btn_row1)

        lib_btn_row2 = QHBoxLayout()
        self.lib_export_btn = QPushButton("Export…")
        self.lib_export_btn.clicked.connect(self._lib_export)
        lib_btn_row2.addWidget(self.lib_export_btn)
        self.lib_import_btn = QPushButton("Import…")
        self.lib_import_btn.clicked.connect(self._lib_import)
        lib_btn_row2.addWidget(self.lib_import_btn)
        lib_layout.addLayout(lib_btn_row2)

        self._library_widget.setVisible(False)
        aoi_outer.addWidget(self._library_widget)

        self._library_toggle_btn.toggled.connect(self._on_library_toggled)
        self.library_list.itemSelectionChanged.connect(self._on_library_selection_changed)

        layout.addWidget(aoi_group)
```

- [ ] **Step 3: Add missing imports at the top of `main_dialog.py`**

The new UI uses `QListWidget`, `QInputDialog`, `QFileDialog`. Check current imports and add what's missing. Find the `from qgis.PyQt.QtWidgets import (` block and add:

```python
    QListWidget,
    QInputDialog,
    QFileDialog,
```

if not already present.

- [ ] **Step 4: Manual smoke test**

Reload the plugin in QGIS. Open the Damage Detection panel. Verify:
- "Draw rectangle on map" button is visible.
- "Queue (0 selected)" label is visible.
- Empty list widget is shown.
- "▶  Saved AOI Library  (0 saved)" toggle button is visible and collapsed.
- Clicking the toggle expands the library section.

- [ ] **Step 5: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: rebuild AOI group box with queue + library sub-sections"
```

---

## Task 4: Queue mechanics — add, remove, rubber bands, label

**Files:**
- Modify: `ui/main_dialog.py` — AOI section methods

Replace the old `_apply_aoi`, `_on_aoi_drawn`, `_apply_aoi_from_coordinates`, `_draw_rubber_band`, `_clear_rubber_band`, `_toggle_aoi_map_visibility`, `_clear_aoi`, `_sync_aoi_rubber_band`, `_sync_aoi_map_overlay`, `_sync_aoi_coord_spinboxes`, `_update_toggle_aoi_map_button_label` methods with the new queue-aware versions. Also update `_activate_aoi_tool` and `showEvent`/`hideEvent`.

- [ ] **Step 1: Delete the old AOI methods**

Remove the following methods entirely from `ui/main_dialog.py`:
- `_sync_aoi_rubber_band`
- `_sync_aoi_map_overlay`
- `_sync_aoi_coord_spinboxes`
- `_apply_aoi`
- `_on_aoi_drawn`
- `_apply_aoi_from_coordinates`
- `_draw_rubber_band`
- `_clear_rubber_band`
- `_update_toggle_aoi_map_button_label`
- `_toggle_aoi_map_visibility`
- `_clear_aoi`

(Keep `_activate_aoi_tool` — it will be updated in the next step.)

- [ ] **Step 2: Update `_activate_aoi_tool`**

Replace the existing `_activate_aoi_tool` method with:

```python
    def _activate_aoi_tool(self):
        canvas = self.iface.mapCanvas()
        if self.map_tool is None:
            from .aoi_tool import PWTTMapToolExtent
            self.map_tool = PWTTMapToolExtent(canvas, self._on_aoi_drawn)
        self._previous_map_tool = canvas.mapTool()
        canvas.setMapTool(self.map_tool)
        self.iface.messageBar().pushMessage(
            "PWTT", "Draw a rectangle on the map to add it to the AOI queue.",
            level=Qgis.Info, duration=5,
        )
```

- [ ] **Step 3: Add new queue/rubber-band helper methods**

Add these methods in the `# ── AOI` section of `main_dialog.py`:

```python
    # ── AOI ───────────────────────────────────────────────────────────────────

    def _queue_colour(self, index: int) -> tuple:
        return self._AOI_COLOURS[index % len(self._AOI_COLOURS)]

    def _add_to_queue(self, aoi_entry: dict):
        """Add an AOI dict to the queue (no-op if same id already present)."""
        if any(a["id"] == aoi_entry["id"] for a in self._queue):
            return
        self._queue.append(aoi_entry)
        self._rebuild_queue_list()
        self._draw_rubber_band_for(aoi_entry)
        self._update_queue_buttons()

    def _rebuild_queue_list(self):
        """Sync the QListWidget with self._queue."""
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for aoi in self._queue:
            tag = aoi.get("tag", "drawn")
            label = f"{aoi['name']}  [{tag}]"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if aoi.get("checked", True) else Qt.Unchecked)
            item.setData(Qt.UserRole, aoi["id"])
            self.queue_list.addItem(item)

            # Add Save / Remove buttons via a widget in the item
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 4, 0)
            row_layout.addStretch()

            if tag == "drawn":
                save_btn = QPushButton("Save")
                aoi_id = aoi["id"]
                save_btn.clicked.connect(lambda _checked, aid=aoi_id: self._queue_save_aoi(aid))
                row_layout.addWidget(save_btn)

            remove_btn = QPushButton("Remove")
            aoi_id = aoi["id"]
            remove_btn.clicked.connect(lambda _checked, aid=aoi_id: self._queue_remove_aoi(aid))
            row_layout.addWidget(remove_btn)

            self.queue_list.setItemWidget(item, row_widget)

        self.queue_list.blockSignals(False)
        self.queue_list.itemChanged.connect(self._on_queue_item_changed)
        self._update_queue_label()

    def _on_queue_item_changed(self, item):
        aoi_id = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        for aoi in self._queue:
            if aoi["id"] == aoi_id:
                aoi["checked"] = checked
                break
        self._update_queue_label()

    def _update_queue_label(self):
        total = len(self._queue)
        selected = sum(1 for a in self._queue if a.get("checked", True))
        self.queue_label.setText(f"Queue  ({selected} selected)")

    def _update_queue_buttons(self):
        has_items = bool(self._queue)
        self.clear_queue_btn.setEnabled(has_items)
        self.toggle_all_map_btn.setEnabled(has_items)

    def _draw_rubber_band_for(self, aoi_entry: dict):
        """Draw a rubber band for a single AOI entry."""
        aoi_id = aoi_entry["id"]
        self._remove_rubber_band(aoi_id)
        bbox = aoi_entry.get("bbox")
        if not bbox or len(bbox) < 4:
            return
        west, south, east, north = bbox
        rect = QgsRectangle(west, south, east, north)
        geom = QgsGeometry.fromRect(rect)
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != src_crs:
            transform = QgsCoordinateTransform(src_crs, canvas_crs, QgsProject.instance())
            geom.transform(transform)
        idx = len(self._rubber_bands)
        r, g, b = self._queue_colour(idx)
        rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        rb.setColor(QColor(r, g, b, 50))
        rb.setStrokeColor(QColor(r, g, b, 220))
        rb.setWidth(2)
        rb.setToGeometry(geom, None)
        self._rubber_bands[aoi_id] = rb

    def _remove_rubber_band(self, aoi_id: str):
        rb = self._rubber_bands.pop(aoi_id, None)
        if rb is not None:
            rb.reset(QgsWkbTypes.PolygonGeometry)

    def _clear_all_rubber_bands(self):
        for rb in self._rubber_bands.values():
            rb.reset(QgsWkbTypes.PolygonGeometry)
        self._rubber_bands.clear()

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
        try:
            self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        except Exception:
            pass

    def _queue_save_aoi(self, aoi_id: str):
        """Prompt for name, save to library, update queue row tag."""
        from ..core import aoi_store
        aoi_entry = next((a for a in self._queue if a["id"] == aoi_id), None)
        if aoi_entry is None:
            return
        name, ok = QInputDialog.getText(
            self, "Save AOI", "AOI name:", text=aoi_entry["name"]
        )
        if not ok or not name.strip():
            return
        new_aoi = aoi_store.make_aoi(name.strip(), aoi_entry["wkt"], aoi_entry["bbox"])
        aoi_store.save_aoi(new_aoi)
        # Replace tmp entry in queue with saved entry
        aoi_entry.update({"id": new_aoi["id"], "name": new_aoi["name"], "tag": "saved"})
        # Move rubber band to new id
        rb = self._rubber_bands.pop(aoi_id, None)
        if rb is not None:
            self._rubber_bands[new_aoi["id"]] = rb
        self._rebuild_queue_list()
        self._refresh_library_list()
        self.iface.messageBar().pushMessage(
            "PWTT", f'AOI "{name.strip()}" saved to library.', level=Qgis.Success, duration=4,
        )

    def _queue_remove_aoi(self, aoi_id: str):
        self._queue = [a for a in self._queue if a["id"] != aoi_id]
        self._remove_rubber_band(aoi_id)
        self._rebuild_queue_list()
        self._update_queue_buttons()

    def _clear_queue(self):
        self._queue.clear()
        self._clear_all_rubber_bands()
        self._rebuild_queue_list()
        self._update_queue_buttons()

    def _toggle_all_map_visibility(self):
        # If any rubber band exists, toggle all by hiding/showing
        if self._rubber_bands:
            first = next(iter(self._rubber_bands.values()))
            visible = first.isVisible()
            for rb in self._rubber_bands.values():
                rb.setVisible(not visible)
            self.toggle_all_map_btn.setText(
                "Show all on map" if visible else "Hide all on map"
            )
```

- [ ] **Step 4: Update `showEvent` and `hideEvent`**

Replace the existing `showEvent` and `hideEvent` methods:

```python
    def showEvent(self, event):
        super().showEvent(event)
        # Restore rubber bands after dock is re-shown
        for aoi_entry in self._queue:
            if aoi_entry["id"] not in self._rubber_bands:
                self._draw_rubber_band_for(aoi_entry)
        self._on_backend_changed(self.backend_combo.currentIndex())

    def hideEvent(self, event):
        super().hideEvent(event)
        # Keep rubber bands visible when hidden (user may have drawn AOIs)
```

- [ ] **Step 5: Update `cleanup_map_canvas`**

Replace the existing `cleanup_map_canvas` method:

```python
    def cleanup_map_canvas(self):
        """Remove all AOI overlays and extent map tool; call before dock teardown."""
        self._clear_all_rubber_bands()
        canvas = self.iface.mapCanvas()
        if self.map_tool and canvas.mapTool() == self.map_tool and self._previous_map_tool:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except Exception:
                pass
```

- [ ] **Step 6: Manual smoke test**

In QGIS:
1. Click "Draw rectangle on map" → draw a box → verify it appears in queue list as "Drawn AOI 1 [drawn]" with "Save" and "Remove" buttons and an orange rubber band.
2. Draw a second box → verify "Drawn AOI 2 [drawn]" with a blue rubber band.
3. Click "Save" on first AOI → name dialog appears → enter name → row tag changes to `[saved]`.
4. Click "Remove" on second AOI → it disappears from list, blue rubber band clears.
5. Click "Clear queue" → both entries removed, all rubber bands gone.

- [ ] **Step 7: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: queue mechanics — add/remove/save AOIs, multi-colour rubber bands"
```

---

## Task 5: Library mechanics — load, rename, delete, export, import

**Files:**
- Modify: `ui/main_dialog.py` — library section methods

- [ ] **Step 1: Add library helper methods**

Add these methods to `ui/main_dialog.py` in the AOI section:

```python
    # ── Library ───────────────────────────────────────────────────────────────

    def _refresh_library_list(self):
        """Reload library list widget from aoi_store."""
        from ..core import aoi_store
        aois = aoi_store.load_aois()
        self.library_list.clear()
        for aoi in aois:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(aoi.get("created_at", ""))
                date_str = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_str = ""
            label = f"{aoi['name']}  {date_str}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, aoi["id"])
            self.library_list.addItem(item)
        count = len(aois)
        checked = self._library_toggle_btn.isChecked()
        self._library_toggle_btn.setText(
            f"{'▼' if checked else '▶'}  Saved AOI Library  ({count} saved)"
        )
        self._on_library_selection_changed()

    def _on_library_toggled(self, checked: bool):
        self._library_widget.setVisible(checked)
        if checked:
            self._refresh_library_list()
        aois = self.library_list.count()
        self._library_toggle_btn.setText(
            f"{'▼' if checked else '▶'}  Saved AOI Library  ({aois} saved)"
        )

    def _on_library_selection_changed(self):
        has_sel = bool(self.library_list.selectedItems())
        self.lib_load_btn.setEnabled(has_sel)
        self.lib_rename_btn.setEnabled(has_sel)
        self.lib_delete_btn.setEnabled(has_sel)

    def _lib_load_selected(self):
        from ..core import aoi_store
        all_aois = {a["id"]: a for a in aoi_store.load_aois()}
        for item in self.library_list.selectedItems():
            aoi_id = item.data(Qt.UserRole)
            aoi = all_aois.get(aoi_id)
            if aoi is None:
                continue
            entry = {
                "id": aoi["id"],
                "name": aoi["name"],
                "wkt": aoi["wkt"],
                "bbox": aoi["bbox"],
                "tag": "saved",
                "checked": True,
            }
            self._add_to_queue(entry)

    def _lib_rename_selected(self):
        from ..core import aoi_store
        items = self.library_list.selectedItems()
        if not items:
            return
        item = items[0]
        aoi_id = item.data(Qt.UserRole)
        aoi = next((a for a in aoi_store.load_aois() if a["id"] == aoi_id), None)
        if aoi is None:
            return
        name, ok = QInputDialog.getText(self, "Rename AOI", "New name:", text=aoi["name"])
        if not ok or not name.strip():
            return
        aoi["name"] = name.strip()
        aoi_store.save_aoi(aoi)
        # Update queue label if loaded
        for q_aoi in self._queue:
            if q_aoi["id"] == aoi_id:
                q_aoi["name"] = name.strip()
        self._rebuild_queue_list()
        self._refresh_library_list()

    def _lib_delete_selected(self):
        from ..core import aoi_store
        items = self.library_list.selectedItems()
        if not items:
            return
        names = [it.text().split("  ")[0] for it in items]
        confirm = QMessageBox.question(
            self, "PWTT",
            f"Delete {len(items)} saved AOI(s)?\n" + "\n".join(f"  • {n}" for n in names),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        for item in items:
            aoi_store.delete_aoi(item.data(Qt.UserRole))
        self._refresh_library_list()

    def _lib_export(self):
        from ..core import aoi_store
        path, _ = QFileDialog.getSaveFileName(
            self, "Export saved AOIs", "", "JSON files (*.json)"
        )
        if not path:
            return
        count = aoi_store.export_aois_to_file(path)
        self.iface.messageBar().pushMessage(
            "PWTT", f"Exported {count} AOI(s) to {path}.", level=Qgis.Success, duration=5,
        )

    def _lib_import(self):
        from ..core import aoi_store
        path, _ = QFileDialog.getOpenFileName(
            self, "Import AOIs", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            result = aoi_store.import_aois_from_file(path)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Import failed: {e}")
            return
        self._refresh_library_list()
        self.iface.messageBar().pushMessage(
            "PWTT",
            f"Imported {result['added']} AOI(s). Skipped invalid: {result['skipped_invalid']}.",
            level=Qgis.Success, duration=5,
        )
```

- [ ] **Step 2: Manual smoke test**

In QGIS:
1. Draw an AOI → click "Save" → confirm it appears in the library (expand "Saved AOI Library").
2. Select a library entry → click "Load into queue" → verify it appears in queue as `[saved]` with correct rubber band.
3. Attempt to load the same entry again → no duplicate appears in queue.
4. Select a library entry → click "Rename" → enter new name → verify both list and queue (if loaded) update.
5. Select a library entry → click "Delete" → confirm dialog appears → entry removed.
6. Click "Export…" → save to a path → open the file and confirm JSON envelope with `"format": "pwtt_aois_export"`.
7. Click "Import…" → import the file → entry re-appears in library.

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: library mechanics — load/rename/delete/export/import saved AOIs"
```

---

## Task 6: `_BatchConfirmDialog` — multi-AOI confirmation dialog

**Files:**
- Modify: `ui/main_dialog.py` — add new inner class before `PWTTControlsDock`

- [ ] **Step 1: Add `_BatchConfirmDialog` class**

Add this class in `ui/main_dialog.py` immediately **before** the `class PWTTControlsDock(QDockWidget):` definition:

```python
class _BatchConfirmDialog:
    """
    Minimal wrapper around QMessageBox-equivalent logic using a QDialog.
    Shows run summary + per-AOI checkboxes so user can deselect individual AOIs.
    """

    def __init__(self, parent, summary_text: str, aois: list):
        from qgis.PyQt.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QScrollArea, QWidget,
            QDialogButtonBox,
        )
        from qgis.PyQt.QtCore import Qt

        self._dialog = QDialog(parent)
        self._dialog.setWindowTitle("PWTT \u2014 Confirm run")
        self._dialog.setMinimumWidth(480)

        outer = QVBoxLayout(self._dialog)

        # Run summary (scrollable)
        summary_label = QLabel(summary_text)
        summary_label.setWordWrap(True)
        outer.addWidget(summary_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        outer.addWidget(QLabel(f"<b>AOIs to run ({len(aois)}):</b>"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(200)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(2)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        self._checkboxes = []
        for aoi in aois:
            cb = QCheckBox(aoi["name"])
            cb.setChecked(True)
            cb.setProperty("aoi_data", aoi)
            cb.stateChanged.connect(self._update_button)
            inner_layout.addWidget(cb)
            self._checkboxes.append(cb)

        self._buttons = QDialogButtonBox()
        self._run_btn = self._buttons.addButton("Run 0 jobs", QDialogButtonBox.AcceptRole)
        self._buttons.addButton(QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._dialog.accept)
        self._buttons.rejected.connect(self._dialog.reject)
        outer.addWidget(self._buttons)

        self._update_button()

    def _update_button(self, _state=None):
        count = sum(1 for cb in self._checkboxes if cb.isChecked())
        self._run_btn.setText(f"Run {count} job{'s' if count != 1 else ''}")
        self._run_btn.setEnabled(count > 0)

    def exec(self) -> list:
        """Show dialog; return list of confirmed AOI dicts (empty if cancelled)."""
        result = self._dialog.exec_()
        if result != self._dialog.Accepted:
            return []
        return [
            cb.property("aoi_data")
            for cb in self._checkboxes
            if cb.isChecked()
        ]
```

- [ ] **Step 2: Add missing imports**

Ensure `QFrame`, `QCheckBox`, `QDialogButtonBox`, `QDialog` are imported at the top of `main_dialog.py`. These are all in `qgis.PyQt.QtWidgets`. Add any that are missing to the existing import block.

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: add _BatchConfirmDialog for per-AOI run confirmation"
```

---

## Task 7: Update `_run()` — loop over confirmed AOIs

**Files:**
- Modify: `ui/main_dialog.py` — `_run()` method (lines ~1310–1436)

- [ ] **Step 1: Replace the `_run()` method**

Replace the entire `_run` method with:

```python
    def _run(self):
        from ..core import deps, job_store

        checked_aois = [a for a in self._queue if a.get("checked", True)]
        if not checked_aois:
            QMessageBox.warning(
                self,
                "PWTT",
                "Please add at least one area of interest to the queue: "
                "draw on the map or load from the saved library.",
            )
            return

        war = self.war_start.date()
        inf = self.inference_start.date()
        if inf < war:
            QMessageBox.warning(
                self, "PWTT",
                "Inference start date should be on or after war start date.",
            )
            return

        backend_id = self.backend_combo.currentData()
        local_src = self._local_data_source_id() if backend_id == "local" else None

        if backend_id == "local" and not confirm_local_processing_storage(self):
            return

        # ── Batch confirmation dialog ────────────────────────────────────────
        dlg = _BatchConfirmDialog(
            self,
            self._run_confirmation_summary_text(),
            checked_aois,
        )
        confirmed_aois = dlg.exec()
        if not confirmed_aois:
            return

        # ── Check backend dependencies ───────────────────────────────────────
        missing, pip_names = deps.backend_missing(backend_id, local_src)
        if missing:
            reply = QMessageBox.question(
                self, "PWTT",
                f"Missing packages: {', '.join(pip_names)}\n\nInstall now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if is_message_box_yes(reply):
                if not deps.install_with_dialog(pip_names, parent=self):
                    return
                missing, _ = deps.backend_missing(backend_id, local_src)
            if missing:
                QMessageBox.warning(
                    self, "PWTT",
                    f"Cannot run: missing {', '.join(missing)}.",
                )
                return
            self._on_backend_changed(self.backend_combo.currentIndex())

        # ── Check footprint dependencies ─────────────────────────────────────
        if self.include_footprints.isChecked():
            if not ensure_footprint_dependencies(self):
                return

        # ── Authenticate backend ─────────────────────────────────────────────
        credentials = self._get_credentials(backend_id)
        try:
            backend = backend_auth_create_and_auth_backend(
                backend_id,
                parent=self,
                controls_dock=self,
                local_data_source=credentials.get("source") if backend_id == "local" else None,
            )
        except RuntimeError as e:
            if str(e) != "Authentication cancelled.":
                QMessageBox.warning(self, "PWTT", str(e))
            else:
                self.iface.messageBar().pushMessage(
                    "PWTT", "Authentication cancelled.", level=Qgis.Info, duration=5,
                )
            return
        except Exception as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return

        self._save_settings()
        base_dir = self.output_dir.filePath()
        if not base_dir:
            proj_path = QgsProject.instance().absolutePath()
            base_dir = proj_path if proj_path else os.path.expanduser("~/PWTT")
            self.output_dir.setFilePath(base_dir)

        fp_sources = []
        if self.include_footprints.isChecked():
            if self.fp_current_osm.isChecked():
                fp_sources.append("current_osm")
            if self.fp_historical_war_start.isChecked():
                fp_sources.append("historical_war_start")
            if self.fp_historical_inference_start.isChecked():
                fp_sources.append("historical_inference_start")
            if not fp_sources:
                fp_sources = ["current_osm"]

        # ── Create and launch one job per confirmed AOI ──────────────────────
        launched_ids = []
        for aoi_entry in confirmed_aois:
            job = job_store.create_job(
                backend_id=backend_id,
                aoi_wkt=aoi_entry["wkt"],
                war_start=self.war_start.date().toString("yyyy-MM-dd"),
                inference_start=self.inference_start.date().toString("yyyy-MM-dd"),
                pre_interval=self.pre_interval.value(),
                post_interval=self.post_interval.value(),
                output_dir="",
                include_footprints=bool(fp_sources),
                footprints_sources=fp_sources,
                damage_threshold=self.damage_threshold_spin.value(),
                gee_viz=self.gee_map_preview_cb.isChecked() if backend_id == "gee" else False,
                data_source=self._local_data_source_id() if backend_id == "local" else "cdse",
                gee_method=self.gee_method_combo.currentData() if backend_id == "gee" else "stouffer",
                gee_ttest_type=self.gee_ttest_type_combo.currentData() if backend_id == "gee" else "welch",
                gee_smoothing=self.gee_smoothing_combo.currentData() if backend_id == "gee" else "default",
                gee_mask_before_smooth=self.gee_mask_before_smooth_cb.isChecked() if backend_id == "gee" else True,
                gee_lee_mode=self.gee_lee_mode_combo.currentData() if backend_id == "gee" else "per_image",
            )
            job["output_dir"] = os.path.join(base_dir, job["id"])
            os.makedirs(job["output_dir"], exist_ok=True)
            job_store.save_job(job)
            if self.jobs_dock.launch_job(job, backend):
                launched_ids.append(aoi_entry["id"])

        # ── Post-run cleanup ─────────────────────────────────────────────────
        for aoi_id in launched_ids:
            self._queue = [a for a in self._queue if a["id"] != aoi_id]
            self._remove_rubber_band(aoi_id)
        self._rebuild_queue_list()
        self._update_queue_buttons()
```

- [ ] **Step 2: Update `_run_confirmation_summary_text()`**

The summary text is shown in the batch dialog header. Find `_run_confirmation_summary_text` and update the AOI line. Find the line that references `self.aoi_wkt` or `aoi_wkt` in the summary method and replace it with a count:

```python
        # Replace any single-AOI line in the summary with:
        n_aois = sum(1 for a in self._queue if a.get("checked", True))
        lines.append(f"AOIs selected: {n_aois}")
```

Read the full current `_run_confirmation_summary_text` method (lines ~1256–1308) and apply only this replacement, keeping all other lines intact.

- [ ] **Step 3: Manual smoke test — single AOI**

1. Draw one AOI → check it → click Run.
2. Batch confirm dialog appears showing 1 AOI with a checkbox.
3. Click "Run 1 job" → job appears in Jobs panel.
4. Queue is cleared after launch.

- [ ] **Step 4: Manual smoke test — multiple AOIs**

1. Draw two AOIs.
2. Click Run → batch dialog shows 2 AOIs.
3. Uncheck one → button label changes to "Run 1 job".
4. Confirm → only 1 job created and launched; unchecked AOI remains in queue.

- [ ] **Step 5: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: batch run — one job per confirmed AOI in queue"
```

---

## Task 8: Update `load_job_params()` — add to queue instead of single AOI

**Files:**
- Modify: `ui/main_dialog.py` — `load_job_params()`, AOI section (~lines 1013–1033)

When a user clicks "Re-run" on a job in the Jobs panel, `load_job_params()` is called. It must add to the queue instead of calling the removed `_apply_aoi`.

- [ ] **Step 1: Update the AOI block inside `load_job_params()`**

Find the block:

```python
        # AOI — parse WKT, set rubber band, zoom
        aoi_wkt = job.get("aoi_wkt")
        if aoi_wkt:
            bbox = wkt_to_bbox(aoi_wkt)
            if bbox:
                west, south, east, north = bbox
                rect = QgsRectangle(west, south, east, north)
                self._apply_aoi(aoi_wkt, rect)

                # Zoom to AOI
                canvas = self.iface.mapCanvas()
                ...
                canvas.refresh()
```

Replace it with:

```python
        # AOI — load into queue and zoom to it
        aoi_wkt = job.get("aoi_wkt")
        if aoi_wkt:
            from ..core.utils import wkt_to_bbox
            import uuid as _uuid
            bbox = wkt_to_bbox(aoi_wkt)
            if bbox:
                west, south, east, north = bbox
                aoi_entry = {
                    "id": "tmp_" + _uuid.uuid4().hex[:8],
                    "name": f"Job {job.get('id', '?')} AOI",
                    "wkt": aoi_wkt,
                    "bbox": list(bbox),
                    "tag": "drawn",
                    "checked": True,
                }
                self._add_to_queue(aoi_entry)

                # Zoom to AOI
                rect = QgsRectangle(west, south, east, north)
                canvas = self.iface.mapCanvas()
                canvas_crs = canvas.mapSettings().destinationCrs()
                src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
                geom = QgsGeometry.fromRect(rect)
                if canvas_crs != src_crs:
                    transform = QgsCoordinateTransform(
                        src_crs, canvas_crs, QgsProject.instance()
                    )
                    geom.transform(transform)
                canvas.setExtent(geom.boundingBox())
                canvas.refresh()
```

- [ ] **Step 2: Manual smoke test**

In the Jobs panel, right-click a completed job → "Re-run" (or equivalent) → confirm the AOI appears in the queue list with a rubber band on the map, and the rest of the panel fields populate correctly.

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "fix: load_job_params adds job AOI to queue instead of calling removed _apply_aoi"
```

---

## Task 9: Polish and edge cases

**Files:**
- Modify: `ui/main_dialog.py`

- [ ] **Step 1: Disconnect `itemChanged` before reconnecting in `_rebuild_queue_list`**

The `_rebuild_queue_list` method reconnects `itemChanged` each call, which can stack up signal connections. Fix by disconnecting first:

In `_rebuild_queue_list`, before `self.queue_list.itemChanged.connect(...)`, add:

```python
        try:
            self.queue_list.itemChanged.disconnect()
        except Exception:
            pass
```

- [ ] **Step 2: Library auto-refreshes when expanded**

`_on_library_toggled` already calls `_refresh_library_list()` when toggled on. Verify this works: save an AOI in one session, reopen QGIS, expand library — entries should persist.

- [ ] **Step 3: Verify `closeEvent` still calls `cleanup_map_canvas`**

Confirm `closeEvent` in `PWTTControlsDock` still calls `self.cleanup_map_canvas()` and `super().closeEvent(event)`. No change needed if it does.

- [ ] **Step 4: Run all unit tests**

```bash
python -m pytest tests/test_aoi_store.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Final end-to-end manual test**

1. Open QGIS, load the plugin.
2. Draw 3 AOIs → verify 3 different coloured rubber bands.
3. Save AOI 1 to library → rename it → verify name updates in queue.
4. Expand library → load AOI 2 from library (after saving it) → verify no duplicate in queue.
5. Export library → clear library → import → verify entries restored.
6. Select all 3 AOIs in queue → click Run → batch dialog shows 3 entries.
7. Uncheck one in dialog → "Run 2 jobs" → confirm → 2 jobs appear in Jobs panel.
8. Unchecked AOI remains in queue; the 2 launched AOIs are removed.
9. Close and reopen QGIS → library still has saved AOIs; queue is empty (session-only).

- [ ] **Step 6: Commit**

```bash
git add ui/main_dialog.py
git commit -m "fix: disconnect itemChanged before reconnect in rebuild_queue_list; final polish"
```

---

## Self-Review

**Spec coverage:**
- ✅ Save AOI (user-defined name + auto id) → Task 4 `_queue_save_aoi`
- ✅ Persistent JSON store (`aoi_store.py`) → Task 1
- ✅ Export/import → Task 1 + Task 5 library buttons
- ✅ Queue UI (checkbox list) → Task 3 `_rebuild_queue_list`
- ✅ Draw multiple one-off AOIs (pending) → Task 4 `_on_aoi_drawn`
- ✅ Load saved AOIs into queue → Task 5 `_lib_load_selected`
- ✅ No duplicate loading → Task 4 `_add_to_queue` guard
- ✅ Rename → Task 5 `_lib_rename_selected`
- ✅ Delete from library → Task 5 `_lib_delete_selected`
- ✅ Multi-colour rubber bands → Task 4 `_draw_rubber_band_for` + `_queue_colour`
- ✅ Batch confirm dialog with per-AOI deselect → Task 6
- ✅ One job per confirmed AOI → Task 7 `_run` loop
- ✅ Post-run: launched AOIs removed from queue; saved library unchanged → Task 7
- ✅ `load_job_params` updated → Task 8
- ✅ `cleanup_map_canvas` clears all rubber bands → Task 4

**Placeholder scan:** None found. All steps contain code.

**Type consistency:** `aoi_entry` dict schema (id, name, wkt, bbox, tag, checked) used consistently across Tasks 4–8. `_rubber_bands: dict[str, QgsRubberBand]` keyed by `aoi_entry["id"]` throughout. `_BatchConfirmDialog.exec()` returns `list[dict]` consumed directly in `_run()`.
