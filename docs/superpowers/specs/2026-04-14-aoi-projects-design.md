# AOI Project Folders — Design Spec

**Date:** 2026-04-14  
**Status:** Approved  
**Scope:** Add flat project/folder grouping to the Saved AOI Library.

---

## Summary

Allow users to organise saved AOIs into named projects (flat, single-level folders).
Every AOI must belong to exactly one project. A "Default" project is auto-created and
all existing AOIs are migrated into it on first load.

---

## Decisions

| Question | Decision |
|----------|----------|
| Nesting depth | Flat only — one level of projects |
| Ungrouped AOIs | Not allowed; every AOI belongs to exactly one project |
| Project operations | Create, Rename, Delete (cascade), Move AOIs, single-project export/import |
| UI widget | Replace `QListWidget` with `QTreeWidget` |
| Move mechanism | Drag-and-drop + right-click "Move to project…" context menu |
| Storage strategy | Extend `saved_aois.json` with a versioned envelope (Approach A) |

---

## Data Model

### `saved_aois.json` — v2 format

```json
{
  "version": 2,
  "projects": [
    {"id": "abc12345", "name": "Default", "created_at": "2026-04-14T10:00:00"}
  ],
  "aois": [
    {
      "id": "x1y2z3w4",
      "project_id": "abc12345",
      "name": "Kyiv center",
      "wkt": "...",
      "bbox": [...],
      "created_at": "2026-04-14T10:00:00"
    }
  ]
}
```

### Migration (v1 → v2)

Triggered automatically by `_read_raw()` when the file contains a bare JSON array (old format):

1. Create a "Default" project with a new UUID and current timestamp.
2. Assign every existing AOI's `project_id` to that project's id.
3. Rewrite the file in v2 envelope format immediately.

Migration is silent and non-destructive.

---

## `core/aoi_store.py` — API changes

### New functions

```python
def make_project(name: str) -> dict
    """Return a new project record (not yet saved to disk)."""

def load_projects() -> List[dict]
    """Return all projects in name-sorted order."""

def save_project(project: dict)
    """Insert or update project by id."""

def delete_project(project_id: str, cascade: bool = True)
    """Delete a project. If cascade=True, also deletes all its AOIs.
    Raises ValueError if this is the last remaining project."""

def move_aoi(aoi_id: str, target_project_id: str)
    """Reassign an AOI to a different project.
    Raises ValueError if aoi_id or target_project_id is not found."""

def export_project_to_file(project_id: str, path: str) -> int
    """Write a single project and its AOIs to path as export JSON. Returns AOI count."""
```

### Modified functions

```python
def load_aois(project_id: str = None) -> List[dict]
    """Return AOIs, optionally filtered by project_id. None = all projects."""

def import_aois_from_file(path: str) -> Dict[str, int]
    """Existing function extended:
    - If the file contains a 'project' key (single-project export), prompt is
      handled upstream in the UI; the store accepts an optional target_project_id kwarg.
    - If the file is an old flat-array export, all AOIs are assigned to a new
      auto-named project: 'Imported <ISO date>'.
    - Returns dict with keys: added, skipped_invalid, ids_rewritten."""
```

### Internal changes

- `_read_raw()` → returns `(projects, aois)` tuple; handles v1 migration.
- `_write(projects, aois)` → writes the full v2 envelope.
- Orphan repair: on load, any AOI whose `project_id` has no matching project is
  silently reassigned to the first available project; the file is rewritten.

---

## UI — `ui/main_dialog.py`

### Widget replacement

`self.library_list: QListWidget` → `self.library_tree: QTreeWidget`

- **Project items** (top-level, bold): label `ProjectName (N)` where N = AOI count; collapsible.
- **AOI items** (children): label `name  date` (same as current flat list).
- `item.data(Qt.UserRole)` stores `{"type": "project"|"aoi", "id": "..."}`.

### Button row changes

The existing buttons remain but are context-sensitive:

| Selection | Load into queue | Rename | Delete | Export… |
|-----------|----------------|--------|--------|---------|
| AOI(s) | loads AOIs | renames AOI | deletes AOI(s) | exports selected AOIs |
| Project | loads all AOIs in project | renames project | deletes project + AOIs (confirm) | exports project |
| Mixed | loads all | disabled | disabled | disabled |
| Nothing | disabled | disabled | disabled | enabled (export all) |

A **"New project…"** button is added to the first button row.

### Context menu (right-click)

**On an AOI item:**
- Load into queue
- Rename
- Move to project ▶ `[project name list, current project greyed out]`
- Delete

**On a project item:**
- Rename
- New project…
- Delete project and AOIs
- Export project…

### Drag-and-drop

- AOI items: `Qt.MoveAction` draggable.
- Project items: drop targets only (accept drops from AOI items).
- Dropping an AOI onto its own current project is a no-op.
- On successful drop: calls `move_aoi()`, refreshes only the affected project rows
  (no full tree rebuild required).

### Tree refresh

`_refresh_library_list()` → renamed `_refresh_library_tree()`.

Builds the tree in one pass:
1. Load all projects (name-sorted).
2. Load all AOIs.
3. Group AOIs by `project_id` (sorted by `created_at` descending within each group).
4. Render project items with AOI children.

The toggle button header ("Saved AOI Library") and collapsible behaviour are unchanged.
The count shown updates to total AOI count across all projects.

---

## Error Handling & Edge Cases

### Project deletion
- Confirm dialog: _"Delete project 'X' and its N AOI(s)? This cannot be undone."_
- Cascade: project record + all AOIs with that `project_id` removed in a single `_write()`.
- AOIs already loaded in the processing queue are unaffected (queue holds copies).

### Last-project guard
- `delete_project()` raises `ValueError` if only one project remains.
- UI shows: _"You must have at least one project. Create a new project first."_

### Move AOI
- Context menu "Move to project…" greys out the AOI's current project.
- Drag-and-drop onto own project is ignored silently.

### Import with project data
- File has a `project` envelope key (single-project export): UI dialog asks
  _"Import into existing project or create new project 'X'?"_
- File is an old flat-array (no project data): all AOIs go into a new project
  named `"Imported YYYY-MM-DD"`.
- ID collision handling for both projects and AOIs preserved from existing logic.

### Orphan AOIs
- AOIs with an unrecognised `project_id` are silently reassigned to the first
  available project on `_read_raw()` and the file is rewritten immediately.

---

## Testing

All new tests are added to `tests/test_aoi_store.py`.

### `aoi_store.py` unit tests

| Test | What it covers |
|------|---------------|
| `test_migration_v1_to_v2` | Bare array → Default project created; all AOIs assigned; file rewritten as v2 |
| `test_save_and_load_project` | Round-trip `save_project` / `load_projects` |
| `test_delete_project_cascade` | Project + all its AOIs deleted |
| `test_delete_last_project_raises` | `ValueError` when only one project remains |
| `test_move_aoi` | `project_id` updated on disk |
| `test_move_aoi_invalid_ids` | `ValueError` for bad `aoi_id` or `target_project_id` |
| `test_orphan_repair` | Orphan AOI reassigned and file rewritten on load |
| `test_export_project_round_trip` | `export_project_to_file` → `import_aois_from_file` restores AOIs under correct project |
| `test_import_flat_array_creates_project` | Old flat export → new auto-named project |
| `test_load_aois_filtered` | `load_aois(project_id=x)` returns only that project's AOIs |

### UI tests
No automated UI tests (consistent with existing suite). Manual verification via the QGIS plugin.

---

## Files Changed

| File | Change |
|------|--------|
| `core/aoi_store.py` | Extend with project CRUD, migration, `move_aoi`, updated export/import |
| `ui/main_dialog.py` | Replace `QListWidget` with `QTreeWidget`; update all library methods; add context menu, drag-and-drop, new project button |
| `tests/test_aoi_store.py` | Extend with project-related test cases |
