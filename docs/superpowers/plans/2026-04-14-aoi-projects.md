# AOI Project Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add flat project/folder grouping to the Saved AOI Library so users can organise AOIs into named projects.

**Architecture:** Extend `saved_aois.json` to a v2 versioned envelope with a `projects` array; each AOI gains a `project_id` field. The UI's flat `QListWidget` is replaced by a `QTreeWidget` with project items as collapsible parents and AOI items as children. Move operations are supported via drag-and-drop and a right-click context menu.

**Tech Stack:** Python 3, QGIS PyQt5 (`qgis.PyQt`), `QTreeWidget`, `pyqtSignal`, JSON file storage.

---

## File Map

| File | Change |
|------|--------|
| `core/aoi_store.py` | Rewrite internals for v2 format; add project CRUD; update AOI functions; update export/import |
| `ui/main_dialog.py` | Add `_LibraryTree` class; swap `QListWidget` → `QTreeWidget`; update all library methods; add context menu, drag-and-drop, "New project…" |
| `tests/test_aoi_store.py` | Extend with 10 new test cases for project functionality |

---

## Task 1: Update `aoi_store.py` internals for v2 format

**Files:**
- Modify: `core/aoi_store.py`
- Test: `tests/test_aoi_store.py`

- [ ] **Step 1: Write failing migration test**

Add to `tests/test_aoi_store.py`:

```python
def test_migration_v1_to_v2(tmp_path):
    """A bare JSON array (v1) is migrated to v2 on first read."""
    import tempfile, json, types, sys, importlib
    # Write a v1 file directly
    p = os.path.join(_tmpdir, "PWTT", "saved_aois.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    v1_aois = [
        {"id": "aa000001", "name": "Old AOI", "wkt": "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
         "bbox": [0, 0, 1, 1], "created_at": "2024-01-01T00:00:00"},
    ]
    with open(p, "w") as f:
        json.dump(v1_aois, f)

    importlib.reload(aoi_store)
    projects = aoi_store.load_projects()
    aois = aoi_store.load_aois()

    assert len(projects) == 1
    assert projects[0]["name"] == "Default"
    assert len(aois) == 1
    assert aois[0]["project_id"] == projects[0]["id"]
    # File should now be in v2 format
    with open(p) as f:
        data = json.load(f)
    assert data.get("version") == 2
    assert "projects" in data
    assert "aois" in data
```

- [ ] **Step 2: Run test — verify it fails**

```
cd /Volumes/A2/Dev/Projects/pwtt-qgis-plugin
python -m pytest tests/test_aoi_store.py::test_migration_v1_to_v2 -v
```

Expected: `FAILED` — `AttributeError: module 'core.aoi_store' has no attribute 'load_projects'`

- [ ] **Step 3: Write failing orphan-repair test**

Add to `tests/test_aoi_store.py`:

```python
def test_orphan_repair(tmp_path):
    """AOIs with unknown project_id are reassigned to first project on load."""
    import json
    _fresh()
    p = os.path.join(_tmpdir, "PWTT", "saved_aois.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # Write a v2 file with an orphan AOI
    data = {
        "version": 2,
        "projects": [{"id": "proj1", "name": "Proj1", "created_at": "2024-01-01T00:00:00"}],
        "aois": [
            {"id": "aoi1", "project_id": "NONEXISTENT", "name": "Orphan",
             "wkt": "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", "bbox": [0, 0, 1, 1],
             "created_at": "2024-01-01T00:00:00"},
        ],
    }
    with open(p, "w") as f:
        json.dump(data, f)

    aois = aoi_store.load_aois()
    assert aois[0]["project_id"] == "proj1"
    # File repaired on disk
    with open(p) as f:
        saved = json.load(f)
    assert saved["aois"][0]["project_id"] == "proj1"
```

- [ ] **Step 4: Rewrite `_read_raw()` and `_write()` in `core/aoi_store.py`**

Replace the existing `_read_raw` and `_write` functions with:

```python
def _read_raw():
    """Return (projects, aois). Handles v1 migration and orphan repair.
    May write to disk on first call if migration or repair is needed."""
    p = _aois_path()
    if not os.path.isfile(p):
        return [], []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], []

    # v1: bare list — migrate to v2
    if isinstance(data, list):
        aois = [x for x in data if isinstance(x, dict)]
        if aois:
            default = make_project("Default")
            for aoi in aois:
                aoi["project_id"] = default["id"]
            projects = [default]
        else:
            projects = []
        _write(projects, aois)
        return projects, aois

    # v2: versioned envelope
    if isinstance(data, dict):
        projects = [x for x in data.get("projects", []) if isinstance(x, dict)]
        aois = [x for x in data.get("aois", []) if isinstance(x, dict)]
        # Orphan repair
        project_ids = {proj["id"] for proj in projects}
        fallback = projects[0]["id"] if projects else None
        repaired = False
        for aoi in aois:
            if aoi.get("project_id") not in project_ids and fallback:
                aoi["project_id"] = fallback
                repaired = True
        if repaired:
            _write(projects, aois)
        return projects, aois

    return [], []


def _write(projects, aois):
    data = {
        "version": 2,
        "projects": projects,
        "aois": aois,
    }
    with open(_aois_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
```

Also add `make_project()` near the top of `core/aoi_store.py` (before `_read_raw`, since `_read_raw` calls it during migration):

```python
def make_project(name: str) -> dict:
    """Return a new project record (not yet saved to disk)."""
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
```

- [ ] **Step 5: Run both tests — verify they pass**

```
python -m pytest tests/test_aoi_store.py::test_migration_v1_to_v2 tests/test_aoi_store.py::test_orphan_repair -v
```

Expected: `2 passed`

- [ ] **Step 6: Run full test suite — verify no regressions**

```
python -m pytest tests/test_aoi_store.py -v
```

Expected: all pre-existing tests fail because `load_aois()` and `save_aoi()` etc. still call the old `_read_raw()` / `_write()` signatures. That's expected — we fix those in Task 3. For now, check only the two new tests pass.

Actually: existing tests will **fail** now because `_write(aois)` is called with one argument but now expects `(projects, aois)`. Skip this step — proceed to Task 2 and fix all callers together.

- [ ] **Step 7: Commit**

```bash
git add core/aoi_store.py tests/test_aoi_store.py
git commit -m "feat(aoi-store): add v2 format internals with migration and orphan repair"
```

---

## Task 2: Add project CRUD to `aoi_store.py`

**Files:**
- Modify: `core/aoi_store.py`
- Test: `tests/test_aoi_store.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_aoi_store.py`:

```python
def test_save_and_load_project():
    _fresh()
    proj = aoi_store.make_project("Ukraine")
    aoi_store.save_project(proj)
    projects = aoi_store.load_projects()
    assert len(projects) == 1
    assert projects[0]["name"] == "Ukraine"
    assert projects[0]["id"] == proj["id"]


def test_save_project_updates_existing():
    _fresh()
    proj = aoi_store.make_project("Old Name")
    aoi_store.save_project(proj)
    proj["name"] = "New Name"
    aoi_store.save_project(proj)
    projects = aoi_store.load_projects()
    assert len(projects) == 1
    assert projects[0]["name"] == "New Name"


def test_delete_project_cascade():
    _fresh()
    proj = aoi_store.make_project("ToDelete")
    aoi_store.save_project(proj)
    proj2 = aoi_store.make_project("Keep")
    aoi_store.save_project(proj2)
    aoi = aoi_store.make_aoi("An AOI", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id=proj["id"])
    aoi_store.save_aoi(aoi)
    aoi_store.delete_project(proj["id"], cascade=True)
    assert all(p["id"] != proj["id"] for p in aoi_store.load_projects())
    assert all(a["id"] != aoi["id"] for a in aoi_store.load_aois())


def test_delete_last_project_raises():
    _fresh()
    proj = aoi_store.make_project("Only")
    aoi_store.save_project(proj)
    with pytest.raises(ValueError, match="last remaining project"):
        aoi_store.delete_project(proj["id"])


def test_load_projects_sorted_by_name():
    _fresh()
    for name in ["Zebra", "Apple", "Mango"]:
        aoi_store.save_project(aoi_store.make_project(name))
    names = [p["name"] for p in aoi_store.load_projects()]
    assert names == ["Apple", "Mango", "Zebra"]
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_aoi_store.py::test_save_and_load_project tests/test_aoi_store.py::test_delete_project_cascade tests/test_aoi_store.py::test_delete_last_project_raises tests/test_aoi_store.py::test_load_projects_sorted_by_name -v
```

Expected: `FAILED` — `AttributeError: module 'core.aoi_store' has no attribute 'save_project'`

- [ ] **Step 3: Add project CRUD functions to `core/aoi_store.py`**

Add after `make_project()`:

```python
def load_projects() -> List[dict]:
    """Return all projects sorted by name (case-insensitive)."""
    projects, _ = _read_raw()
    return sorted(projects, key=lambda p: p.get("name", "").lower())


def save_project(project: dict):
    """Insert or update project by id."""
    projects, aois = _read_raw()
    for i, p in enumerate(projects):
        if p["id"] == project["id"]:
            projects[i] = project
            _write(projects, aois)
            return
    projects.append(project)
    _write(projects, aois)


def delete_project(project_id: str, cascade: bool = True):
    """Delete project. Raises ValueError if it is the last project.
    If cascade=True (default), also deletes all AOIs belonging to this project."""
    projects, aois = _read_raw()
    if len(projects) <= 1:
        raise ValueError("Cannot delete the last remaining project.")
    projects = [p for p in projects if p["id"] != project_id]
    if cascade:
        aois = [a for a in aois if a.get("project_id") != project_id]
    _write(projects, aois)
```

- [ ] **Step 4: Run tests — verify they pass**

```
python -m pytest tests/test_aoi_store.py::test_save_and_load_project tests/test_aoi_store.py::test_save_project_updates_existing tests/test_aoi_store.py::test_delete_project_cascade tests/test_aoi_store.py::test_delete_last_project_raises tests/test_aoi_store.py::test_load_projects_sorted_by_name -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add core/aoi_store.py tests/test_aoi_store.py
git commit -m "feat(aoi-store): add project CRUD (make, load, save, delete)"
```

---

## Task 3: Update AOI functions for v2 format

**Files:**
- Modify: `core/aoi_store.py`
- Test: `tests/test_aoi_store.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_aoi_store.py`:

```python
def test_make_aoi_includes_project_id():
    aoi = aoi_store.make_aoi("X", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id="proj1")
    assert aoi["project_id"] == "proj1"


def test_load_aois_filtered_by_project():
    _fresh()
    p1 = aoi_store.make_project("P1")
    p2 = aoi_store.make_project("P2")
    aoi_store.save_project(p1)
    aoi_store.save_project(p2)
    a1 = aoi_store.make_aoi("A1", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                             [0, 0, 1, 1], project_id=p1["id"])
    a2 = aoi_store.make_aoi("A2", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                             [0, 0, 1, 1], project_id=p2["id"])
    aoi_store.save_aoi(a1)
    aoi_store.save_aoi(a2)
    assert len(aoi_store.load_aois(project_id=p1["id"])) == 1
    assert aoi_store.load_aois(project_id=p1["id"])[0]["name"] == "A1"
    assert len(aoi_store.load_aois()) == 2


def test_move_aoi():
    _fresh()
    p1 = aoi_store.make_project("Src")
    p2 = aoi_store.make_project("Dst")
    aoi_store.save_project(p1)
    aoi_store.save_project(p2)
    aoi = aoi_store.make_aoi("M", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id=p1["id"])
    aoi_store.save_aoi(aoi)
    aoi_store.move_aoi(aoi["id"], p2["id"])
    loaded = aoi_store.load_aois()
    assert loaded[0]["project_id"] == p2["id"]


def test_move_aoi_invalid_aoi_raises():
    _fresh()
    proj = aoi_store.make_project("P")
    aoi_store.save_project(proj)
    with pytest.raises(ValueError, match="not found"):
        aoi_store.move_aoi("nonexistent_id", proj["id"])


def test_move_aoi_invalid_project_raises():
    _fresh()
    proj = aoi_store.make_project("P")
    aoi_store.save_project(proj)
    aoi = aoi_store.make_aoi("A", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id=proj["id"])
    aoi_store.save_aoi(aoi)
    with pytest.raises(ValueError, match="not found"):
        aoi_store.move_aoi(aoi["id"], "bad_project_id")
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_aoi_store.py::test_make_aoi_includes_project_id tests/test_aoi_store.py::test_load_aois_filtered_by_project tests/test_aoi_store.py::test_move_aoi -v
```

Expected: `FAILED`

- [ ] **Step 3: Update `make_aoi`, `load_aois`, `save_aoi`, `delete_aoi` and add `move_aoi`**

Replace the existing `make_aoi`, `load_aois`, `save_aoi`, `delete_aoi` functions and add `move_aoi` in `core/aoi_store.py`:

```python
def make_aoi(name: str, wkt: str, bbox: List[float], project_id: str = "") -> dict:
    """Return a new AOI record (not yet saved to disk)."""
    return {
        "id": uuid.uuid4().hex[:8],
        "project_id": project_id,
        "name": name,
        "wkt": wkt,
        "bbox": list(bbox),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_aois(project_id: str = None) -> List[dict]:
    """Return AOIs. Pass project_id to filter to one project; None returns all."""
    _, aois = _read_raw()
    if project_id is not None:
        return [a for a in aois if a.get("project_id") == project_id]
    return aois


def save_aoi(aoi: dict):
    """Insert or update AOI by id. Auto-creates a Default project if none exist."""
    projects, aois = _read_raw()
    # Ensure the AOI has a valid project
    project_ids = {p["id"] for p in projects}
    if not projects:
        default = make_project("Default")
        projects.append(default)
        aoi["project_id"] = default["id"]
    elif aoi.get("project_id") not in project_ids:
        aoi["project_id"] = projects[0]["id"]
    for i, a in enumerate(aois):
        if a["id"] == aoi["id"]:
            aois[i] = aoi
            _write(projects, aois)
            return
    aois.insert(0, aoi)
    _write(projects, aois)


def delete_aoi(aoi_id: str):
    projects, aois = _read_raw()
    aois = [a for a in aois if a["id"] != aoi_id]
    _write(projects, aois)


def move_aoi(aoi_id: str, target_project_id: str):
    """Reassign an AOI to a different project. Raises ValueError on bad ids."""
    projects, aois = _read_raw()
    project_ids = {p["id"] for p in projects}
    if target_project_id not in project_ids:
        raise ValueError(f"Project {target_project_id!r} not found.")
    for aoi in aois:
        if aoi["id"] == aoi_id:
            aoi["project_id"] = target_project_id
            _write(projects, aois)
            return
    raise ValueError(f"AOI {aoi_id!r} not found.")
```

- [ ] **Step 4: Run new tests**

```
python -m pytest tests/test_aoi_store.py::test_make_aoi_includes_project_id tests/test_aoi_store.py::test_load_aois_filtered_by_project tests/test_aoi_store.py::test_move_aoi tests/test_aoi_store.py::test_move_aoi_invalid_aoi_raises tests/test_aoi_store.py::test_move_aoi_invalid_project_raises -v
```

Expected: `5 passed`

- [ ] **Step 5: Run full test suite**

```
python -m pytest tests/test_aoi_store.py -v
```

Expected: all tests pass (migration, orphan, project CRUD, AOI CRUD, existing tests).

- [ ] **Step 6: Commit**

```bash
git add core/aoi_store.py tests/test_aoi_store.py
git commit -m "feat(aoi-store): update AOI functions for v2 format; add move_aoi"
```

---

## Task 4: Update export/import in `aoi_store.py`

**Files:**
- Modify: `core/aoi_store.py`
- Test: `tests/test_aoi_store.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_aoi_store.py`:

```python
def test_export_project_round_trip(tmp_path):
    _fresh()
    proj = aoi_store.make_project("Kyiv")
    aoi_store.save_project(proj)
    aoi = aoi_store.make_aoi("City center", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id=proj["id"])
    aoi_store.save_aoi(aoi)
    path = str(tmp_path / "kyiv.json")
    count = aoi_store.export_project_to_file(proj["id"], path)
    assert count == 1

    _fresh()
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 1
    aois = aoi_store.load_aois()
    assert aois[0]["name"] == "City center"
    projects = aoi_store.load_projects()
    assert any(p["name"] == "Kyiv" for p in projects)


def test_import_flat_array_creates_auto_project(tmp_path):
    _fresh()
    # Write an old flat-array file
    path = str(tmp_path / "old.json")
    with open(path, "w") as f:
        json.dump([
            {"id": "xx000001", "name": "Flat AOI",
             "wkt": "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", "bbox": [0, 0, 1, 1],
             "created_at": "2024-01-01T00:00:00"}
        ], f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 1
    projects = aoi_store.load_projects()
    assert any(p["name"].startswith("Imported") for p in projects)
    aois = aoi_store.load_aois()
    assert aois[0]["name"] == "Flat AOI"
    # AOI is assigned to the auto-created project
    pid = aois[0]["project_id"]
    assert any(p["id"] == pid for p in projects)


def test_import_into_target_project(tmp_path):
    _fresh()
    proj = aoi_store.make_project("Existing")
    aoi_store.save_project(proj)
    # Export a single-project file
    proj2 = aoi_store.make_project("FromFile")
    aoi_store.save_project(proj2)
    aoi = aoi_store.make_aoi("A", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                              [0, 0, 1, 1], project_id=proj2["id"])
    aoi_store.save_aoi(aoi)
    path = str(tmp_path / "proj2.json")
    aoi_store.export_project_to_file(proj2["id"], path)

    # Import into proj (not creating a new project)
    result = aoi_store.import_aois_from_file(path, target_project_id=proj["id"])
    assert result["added"] == 1
    loaded = aoi_store.load_aois(project_id=proj["id"])
    assert any(a["name"] == "A" for a in loaded)
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_aoi_store.py::test_export_project_round_trip tests/test_aoi_store.py::test_import_flat_array_creates_auto_project tests/test_aoi_store.py::test_import_into_target_project -v
```

Expected: `FAILED` — `AttributeError: ... has no attribute 'export_project_to_file'`

- [ ] **Step 3: Replace `export_aois_to_file`, `import_aois_from_file`; add `export_project_to_file` in `core/aoi_store.py`**

Remove the `_aois_list_from_parsed_json` helper (it is no longer needed) and replace `export_aois_to_file` and `import_aois_from_file`, and add `export_project_to_file`:

```python
def export_aois_to_file(path: str) -> int:
    """Write all projects and AOIs to path as v2 export JSON. Returns AOI count."""
    projects, aois = _read_raw()
    payload = {
        "format": AOI_EXPORT_FORMAT,
        "version": AOI_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "projects": projects,
        "aois": aois,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(aois)


def export_project_to_file(project_id: str, path: str) -> int:
    """Write a single project and its AOIs to path. Returns AOI count.
    The export file has a 'project' key (singular) so import can detect it."""
    projects, aois = _read_raw()
    project = next((p for p in projects if p["id"] == project_id), None)
    if project is None:
        raise ValueError(f"Project {project_id!r} not found.")
    project_aois = [a for a in aois if a.get("project_id") == project_id]
    payload = {
        "format": AOI_EXPORT_FORMAT,
        "version": AOI_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "aois": project_aois,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(project_aois)


def import_aois_from_file(path: str, target_project_id: str = None) -> Dict[str, int]:
    """Merge AOIs from file into the library. Handles v1 flat arrays, v2 full exports,
    and single-project exports. target_project_id overrides project assignment."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    existing_projects, existing_aois = _read_raw()
    used_ids = {a["id"] for a in existing_aois if a.get("id")}
    used_project_ids = {p["id"] for p in existing_projects}

    incoming_aois: List[dict] = []
    incoming_project = None   # single-project export
    incoming_projects: List[dict] = []  # multi-project export

    if isinstance(data, list):
        incoming_aois = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if data.get("format") == AOI_EXPORT_FORMAT:
            incoming_aois = [x for x in (data.get("aois") or []) if isinstance(x, dict)]
            if "project" in data and isinstance(data["project"], dict):
                incoming_project = data["project"]
            elif "projects" in data and isinstance(data["projects"], list):
                incoming_projects = [x for x in data["projects"] if isinstance(x, dict)]
        else:
            raise ValueError("Unrecognized AOI file (expected PWTT AOI export or a JSON array).")
    else:
        raise ValueError("Unrecognized AOI file format.")

    added = 0
    skipped_invalid = 0
    ids_rewritten = 0

    # Determine effective project for all imported AOIs (unless multi-project)
    project_id_map: Dict[str, str] = {}
    effective_project_id: str = ""

    if target_project_id is not None:
        effective_project_id = target_project_id

    elif incoming_project is not None:
        # Single-project export: recreate the project
        proj = copy.deepcopy(incoming_project)
        old_id = proj.get("id", "")
        while proj.get("id") in used_project_ids:
            proj["id"] = uuid.uuid4().hex[:8]
        used_project_ids.add(proj["id"])
        existing_projects.append(proj)
        effective_project_id = proj["id"]

    elif incoming_projects:
        # Multi-project full export: map old ids to new ids
        for proj_raw in incoming_projects:
            proj = copy.deepcopy(proj_raw)
            old_id = proj.get("id", "")
            while proj.get("id") in used_project_ids:
                proj["id"] = uuid.uuid4().hex[:8]
            used_project_ids.add(proj["id"])
            existing_projects.append(proj)
            if old_id:
                project_id_map[old_id] = proj["id"]

    else:
        # Old flat-array — create an auto-named project
        from datetime import datetime as _dt
        auto_name = f"Imported {_dt.now().strftime('%Y-%m-%d')}"
        auto_proj = make_project(auto_name)
        while auto_proj["id"] in used_project_ids:
            auto_proj["id"] = uuid.uuid4().hex[:8]
        used_project_ids.add(auto_proj["id"])
        existing_projects.append(auto_proj)
        effective_project_id = auto_proj["id"]

    # Assign project_id and merge AOIs
    for raw in incoming_aois:
        if not raw.get("wkt") or not raw.get("name"):
            skipped_invalid += 1
            continue
        aoi = copy.deepcopy(raw)
        if effective_project_id:
            aoi["project_id"] = effective_project_id
        elif project_id_map:
            old_pid = aoi.get("project_id", "")
            aoi["project_id"] = project_id_map.get(
                old_pid,
                existing_projects[0]["id"] if existing_projects else "",
            )
        oid = aoi.get("id")
        if not oid or not isinstance(oid, str):
            aoi["id"] = uuid.uuid4().hex[:8]
        while aoi["id"] in used_ids:
            aoi["id"] = uuid.uuid4().hex[:8]
            ids_rewritten += 1
        used_ids.add(aoi["id"])
        existing_aois.insert(0, aoi)
        added += 1

    if added or incoming_projects or incoming_project or (not isinstance(data, list) and not incoming_aois):
        _write(existing_projects, existing_aois)
    return {"added": added, "skipped_invalid": skipped_invalid, "ids_rewritten": ids_rewritten}
```

Also update the constant at the top of the file:

```python
AOI_EXPORT_VERSION = 2
```

- [ ] **Step 4: Run new export/import tests**

```
python -m pytest tests/test_aoi_store.py::test_export_project_round_trip tests/test_aoi_store.py::test_import_flat_array_creates_auto_project tests/test_aoi_store.py::test_import_into_target_project -v
```

Expected: `3 passed`

- [ ] **Step 5: Run full test suite**

```
python -m pytest tests/test_aoi_store.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add core/aoi_store.py tests/test_aoi_store.py
git commit -m "feat(aoi-store): update export/import for v2 format; add export_project_to_file"
```

---

## Task 5: Add `_LibraryTree` widget and replace `QListWidget` in the UI

**Files:**
- Modify: `ui/main_dialog.py`

- [ ] **Step 1: Update imports in `ui/main_dialog.py`**

In the `from qgis.PyQt.QtWidgets import (...)` block, add `QTreeWidget`, `QTreeWidgetItem`, `QMenu` and remove `QListWidget`, `QListWidgetItem`:

```python
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QStackedWidget,
    QWidget,
    QLineEdit,
    QPushButton,
    QDateEdit,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QProgressBar,
    QGroupBox,
    QFormLayout,
    QMessageBox,
    QScrollArea,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QInputDialog,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
)
```

(Keep `QListWidget` and `QListWidgetItem` — they are still used by `self.queue_list`.)

Also add `pyqtSignal` to the QtCore import:

```python
from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
```

- [ ] **Step 2: Add `_LibraryTree` class before `PWTTControlsDock`**

Insert the following class definition directly above `class PWTTControlsDock(QDockWidget):` in `ui/main_dialog.py`:

```python
class _LibraryTree(QTreeWidget):
    """QTreeWidget that emits aoi_moved(aoi_id, target_project_id) on drag-drop."""

    aoi_moved = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setHeaderHidden(True)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(100)

    def dragEnterEvent(self, event):
        item = self.currentItem()
        if item is not None and item.parent() is not None:
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        target = self.itemAt(event.pos())
        if target is not None:
            data = target.data(0, Qt.UserRole)
            if data and data.get("type") == "project":
                event.accept()
                return
            if target.parent() is not None:
                event.accept()
                return
        event.ignore()

    def dropEvent(self, event):
        target = self.itemAt(event.pos())
        if target is None:
            event.ignore()
            return
        # Resolve drop target to a project item
        data = target.data(0, Qt.UserRole)
        if data and data.get("type") == "aoi":
            target = target.parent()
            data = target.data(0, Qt.UserRole) if target else None
        if not data or data.get("type") != "project":
            event.ignore()
            return
        dragged = self.currentItem()
        if dragged is None or dragged.parent() is None:
            event.ignore()
            return
        dragged_data = dragged.data(0, Qt.UserRole)
        if not dragged_data or dragged_data.get("type") != "aoi":
            event.ignore()
            return
        # No-op if same project
        current_proj_data = dragged.parent().data(0, Qt.UserRole)
        if current_proj_data and current_proj_data.get("id") == data.get("id"):
            event.ignore()
            return
        self.aoi_moved.emit(dragged_data["id"], data["id"])
        event.accept()
```

- [ ] **Step 3: Swap the widget in `_build_ui` and rename the refresh method**

In `_build_ui()`, find the library sub-section (lines ~661–707) and replace the `QListWidget` construction with `_LibraryTree`:

Replace:
```python
        self.library_list = QListWidget()
        self.library_list.setMinimumHeight(80)
        self.library_list.setAlternatingRowColors(True)
        self.library_list.setSelectionMode(QListWidget.ExtendedSelection)
        lib_layout.addWidget(self.library_list)
```

With:
```python
        self.library_tree = _LibraryTree()
        lib_layout.addWidget(self.library_tree)
```

Replace the connection at the bottom of that section:
```python
        self._library_toggle_btn.toggled.connect(self._on_library_toggled)
        self.library_list.itemSelectionChanged.connect(self._on_library_selection_changed)
```

With:
```python
        self._library_toggle_btn.toggled.connect(self._on_library_toggled)
        self.library_tree.itemSelectionChanged.connect(self._on_library_selection_changed)
        self.library_tree.aoi_moved.connect(self._lib_move_aoi)
        self.library_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.library_tree.customContextMenuRequested.connect(self._show_library_context_menu)
```

- [ ] **Step 4: Add the "New project…" button to the first button row**

Find the first button row in the library section (around `lib_btn_row1`). Add the new project button:

```python
        lib_btn_row1 = QHBoxLayout()
        self.lib_new_project_btn = QPushButton("New project…")
        self.lib_new_project_btn.clicked.connect(self._lib_new_project)
        lib_btn_row1.addWidget(self.lib_new_project_btn)
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
```

- [ ] **Step 5: Rename `_refresh_library_list` → `_refresh_library_tree` and rewrite it**

Find `_refresh_library_list` and replace with:

```python
    def _refresh_library_tree(self):
        """Rebuild the library QTreeWidget from aoi_store."""
        from ..core import aoi_store
        self.library_tree.blockSignals(True)
        self.library_tree.clear()
        projects = aoi_store.load_projects()
        aois_by_project: Dict[str, List[dict]] = {p["id"]: [] for p in projects}
        for aoi in aoi_store.load_aois():
            pid = aoi.get("project_id", "")
            if pid in aois_by_project:
                aois_by_project[pid].append(aoi)
        total = 0
        for proj in projects:
            proj_aois = sorted(
                aois_by_project[proj["id"]],
                key=lambda a: a.get("created_at", ""),
                reverse=True,
            )
            proj_item = QTreeWidgetItem(self.library_tree)
            proj_item.setText(0, f"{proj['name']}  ({len(proj_aois)})")
            font = proj_item.font(0)
            font.setBold(True)
            proj_item.setFont(0, font)
            proj_item.setData(0, Qt.UserRole, {"type": "project", "id": proj["id"]})
            proj_item.setExpanded(True)
            for aoi in proj_aois:
                date_str = format_iso_date_display(aoi.get("created_at", "") or "")
                aoi_item = QTreeWidgetItem(proj_item)
                aoi_item.setText(0, f"{aoi['name']}  {date_str}")
                aoi_item.setData(0, Qt.UserRole, {"type": "aoi", "id": aoi["id"]})
            total += len(proj_aois)
        self.library_tree.blockSignals(False)
        checked = self._library_toggle_btn.isChecked()
        self._library_toggle_btn.setText(
            f"{'▼' if checked else '▶'}  Saved AOI Library  ({total} saved)"
        )
        self._on_library_selection_changed()
```

Also add the `Dict` import at the top of `main_dialog.py` — the file already uses type annotations; add `Dict` to the existing imports if not present:

```python
from typing import Dict, List
```

(Check if it's already imported — search for `from typing import` in the file.)

- [ ] **Step 6: Update all call-sites of `_refresh_library_list` → `_refresh_library_tree`**

Search for every occurrence of `_refresh_library_list` in `ui/main_dialog.py` and replace with `_refresh_library_tree`. There should be 4–5 call-sites.

- [ ] **Step 7: Update `_on_library_toggled`**

Find `_on_library_toggled` and update to call `_refresh_library_tree`:

```python
    def _on_library_toggled(self, checked: bool):
        self._library_widget.setVisible(checked)
        if checked:
            self._refresh_library_tree()
        count = sum(
            self.library_tree.topLevelItem(i).childCount()
            for i in range(self.library_tree.topLevelItemCount())
        )
        self._library_toggle_btn.setText(
            f"{'▼' if checked else '▶'}  Saved AOI Library  ({count} saved)"
        )
```

- [ ] **Step 8: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat(ui): replace QListWidget with _LibraryTree (QTreeWidget) for AOI library"
```

---

## Task 6: Update library button callbacks

**Files:**
- Modify: `ui/main_dialog.py`

- [ ] **Step 1: Rewrite `_on_library_selection_changed`**

Replace the existing method:

```python
    def _on_library_selection_changed(self):
        items = self.library_tree.selectedItems()
        types = {item.data(0, Qt.UserRole).get("type") for item in items if item.data(0, Qt.UserRole)}
        has_sel = bool(items)
        single_type = len(types) == 1
        self.lib_load_btn.setEnabled(has_sel)
        self.lib_rename_btn.setEnabled(has_sel and single_type and len(items) == 1)
        self.lib_delete_btn.setEnabled(has_sel and single_type)
```

- [ ] **Step 2: Add `_lib_new_project`**

Add this method to `PWTTControlsDock` in the `# ── Library` section:

```python
    def _lib_new_project(self):
        from ..core import aoi_store
        name, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not name.strip():
            return
        proj = aoi_store.make_project(name.strip())
        aoi_store.save_project(proj)
        self._refresh_library_tree()
```

- [ ] **Step 3: Rewrite `_lib_load_selected`**

Replace the existing method:

```python
    def _lib_load_selected(self):
        from ..core import aoi_store
        all_aois = {a["id"]: a for a in aoi_store.load_aois()}
        aoi_ids_to_load: list = []
        for item in self.library_tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            if data["type"] == "aoi":
                aoi_ids_to_load.append(data["id"])
            elif data["type"] == "project":
                # Load all AOIs in this project
                for i in range(item.childCount()):
                    child_data = item.child(i).data(0, Qt.UserRole)
                    if child_data and child_data["type"] == "aoi":
                        aoi_ids_to_load.append(child_data["id"])
        for aoi_id in aoi_ids_to_load:
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
```

- [ ] **Step 4: Rewrite `_lib_rename_selected`**

Replace the existing method:

```python
    def _lib_rename_selected(self):
        from ..core import aoi_store
        items = self.library_tree.selectedItems()
        if not items:
            return
        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if data["type"] == "aoi":
            aoi = next((a for a in aoi_store.load_aois() if a["id"] == data["id"]), None)
            if aoi is None:
                return
            name, ok = QInputDialog.getText(self, "Rename AOI", "New name:", text=aoi["name"])
            if not ok or not name.strip():
                return
            aoi["name"] = name.strip()
            aoi_store.save_aoi(aoi)
            for q_aoi in self._queue:
                if q_aoi["id"] == data["id"]:
                    q_aoi["name"] = name.strip()
            self._rebuild_queue_list()

        elif data["type"] == "project":
            proj = next((p for p in aoi_store.load_projects() if p["id"] == data["id"]), None)
            if proj is None:
                return
            name, ok = QInputDialog.getText(self, "Rename Project", "New name:", text=proj["name"])
            if not ok or not name.strip():
                return
            proj["name"] = name.strip()
            aoi_store.save_project(proj)

        self._refresh_library_tree()
```

- [ ] **Step 5: Rewrite `_lib_delete_selected`**

Replace the existing method:

```python
    def _lib_delete_selected(self):
        from ..core import aoi_store
        items = self.library_tree.selectedItems()
        if not items:
            return
        types = {item.data(0, Qt.UserRole).get("type") for item in items if item.data(0, Qt.UserRole)}
        if len(types) != 1:
            return

        if "aoi" in types:
            names = []
            for item in items:
                d = item.data(0, Qt.UserRole)
                if d and d["type"] == "aoi":
                    names.append(item.text(0).split("  ")[0])
            confirm = QMessageBox.question(
                self, "PWTT",
                f"Delete {len(items)} saved AOI(s)?\n" + "\n".join(f"  \u2022 {n}" for n in names),
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            for item in items:
                d = item.data(0, Qt.UserRole)
                if d and d["type"] == "aoi":
                    aoi_store.delete_aoi(d["id"])

        elif "project" in types:
            if len(items) != 1:
                return
            item = items[0]
            data = item.data(0, Qt.UserRole)
            proj = next((p for p in aoi_store.load_projects() if p["id"] == data["id"]), None)
            if proj is None:
                return
            aoi_count = item.childCount()
            confirm = QMessageBox.question(
                self, "PWTT",
                f"Delete project '{proj['name']}' and its {aoi_count} AOI(s)?\nThis cannot be undone.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            try:
                aoi_store.delete_project(data["id"], cascade=True)
            except ValueError as e:
                QMessageBox.warning(self, "PWTT", str(e))
                return

        self._refresh_library_tree()
```

- [ ] **Step 6: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat(ui): update library button callbacks for project/AOI tree"
```

---

## Task 7: Add context menu and drag-and-drop handler

**Files:**
- Modify: `ui/main_dialog.py`

- [ ] **Step 1: Add `_show_library_context_menu`**

Add this method to `PWTTControlsDock` in the `# ── Library` section:

```python
    def _show_library_context_menu(self, pos):
        from ..core import aoi_store
        item = self.library_tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        menu = QMenu(self)

        if data["type"] == "aoi":
            menu.addAction("Load into queue", self._lib_load_selected)
            menu.addAction("Rename", self._lib_rename_selected)
            move_menu = menu.addMenu("Move to project")
            current_proj_data = item.parent().data(0, Qt.UserRole) if item.parent() else None
            current_pid = current_proj_data.get("id") if current_proj_data else None
            for proj in aoi_store.load_projects():
                action = move_menu.addAction(proj["name"])
                if proj["id"] == current_pid:
                    action.setEnabled(False)
                else:
                    action.triggered.connect(
                        lambda checked, aid=data["id"], pid=proj["id"]: self._lib_move_aoi(aid, pid)
                    )
            menu.addSeparator()
            menu.addAction("Delete", self._lib_delete_selected)

        elif data["type"] == "project":
            menu.addAction("Rename", self._lib_rename_selected)
            menu.addAction("New project…", self._lib_new_project)
            menu.addSeparator()
            menu.addAction("Delete project and AOIs", self._lib_delete_selected)
            menu.addAction(
                "Export project…",
                lambda: self._lib_export_project(data["id"]),
            )

        menu.exec_(self.library_tree.viewport().mapToGlobal(pos))
```

- [ ] **Step 2: Add `_lib_move_aoi`**

Add this method to `PWTTControlsDock`:

```python
    def _lib_move_aoi(self, aoi_id: str, target_project_id: str):
        from ..core import aoi_store
        try:
            aoi_store.move_aoi(aoi_id, target_project_id)
        except ValueError as e:
            self.iface.messageBar().pushMessage("PWTT", str(e), level=Qgis.Warning, duration=5)
            return
        self._refresh_library_tree()
```

- [ ] **Step 3: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat(ui): add context menu and drag-and-drop for AOI project management"
```

---

## Task 8: Update export/import UI and queue save dialog

**Files:**
- Modify: `ui/main_dialog.py`

- [ ] **Step 1: Rewrite `_lib_export` and add `_lib_export_project`**

Replace the existing `_lib_export` method:

```python
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

    def _lib_export_project(self, project_id: str):
        from ..core import aoi_store
        path, _ = QFileDialog.getSaveFileName(
            self, "Export project", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            count = aoi_store.export_project_to_file(project_id, path)
        except ValueError as e:
            QMessageBox.warning(self, "PWTT", str(e))
            return
        self.iface.messageBar().pushMessage(
            "PWTT", f"Exported {count} AOI(s) to {path}.", level=Qgis.Success, duration=5,
        )
```

- [ ] **Step 2: Rewrite `_lib_import`**

Replace the existing `_lib_import` method:

```python
    def _lib_import(self):
        from ..core import aoi_store
        import json as _json
        path, _ = QFileDialog.getOpenFileName(
            self, "Import AOIs", "", "JSON files (*.json)"
        )
        if not path:
            return

        # Peek at the file to see if it's a single-project export
        target_project_id = None
        try:
            with open(path, encoding="utf-8") as f:
                peek = _json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Could not read file: {e}")
            return

        if isinstance(peek, dict) and "project" in peek and isinstance(peek["project"], dict):
            proj_name = peek["project"].get("name", "")
            projects = aoi_store.load_projects()
            choices = [f"Create new project '{proj_name}'"] + [p["name"] for p in projects]
            choice, ok = QInputDialog.getItem(
                self,
                "Import AOIs",
                f"The file contains project '{proj_name}'.\nImport into:",
                choices,
                0,
                False,
            )
            if not ok:
                return
            if choice != choices[0]:
                # User picked an existing project
                target_project_id = next(p["id"] for p in projects if p["name"] == choice)

        try:
            result = aoi_store.import_aois_from_file(path, target_project_id=target_project_id)
        except Exception as e:
            QMessageBox.warning(self, "PWTT", f"Import failed: {e}")
            return
        self._refresh_library_tree()
        self.iface.messageBar().pushMessage(
            "PWTT",
            f"Imported {result['added']} AOI(s). Skipped invalid: {result['skipped_invalid']}.",
            level=Qgis.Success, duration=5,
        )
```

- [ ] **Step 3: Update `_queue_save_aoi` to select a project**

Replace the existing `_queue_save_aoi` method:

```python
    def _queue_save_aoi(self, aoi_id: str):
        """Prompt for name and project, save to library, update queue row tag."""
        from ..core import aoi_store
        aoi_entry = next((a for a in self._queue if a["id"] == aoi_id), None)
        if aoi_entry is None:
            return
        name, ok = QInputDialog.getText(
            self, "Save AOI", "AOI name:", text=aoi_entry["name"]
        )
        if not ok or not name.strip():
            return

        # Project selection
        projects = aoi_store.load_projects()
        if not projects:
            project_id = ""  # save_aoi will auto-create Default
        elif len(projects) == 1:
            project_id = projects[0]["id"]
        else:
            proj_names = [p["name"] for p in projects]
            proj_name, ok2 = QInputDialog.getItem(
                self, "Save AOI", "Save into project:", proj_names, 0, False
            )
            if not ok2:
                return
            project_id = next(p["id"] for p in projects if p["name"] == proj_name)

        new_aoi = aoi_store.make_aoi(
            name.strip(), aoi_entry["wkt"], aoi_entry["bbox"], project_id=project_id
        )
        aoi_store.save_aoi(new_aoi)
        # Move rubber band to new id before updating entry
        rb = self._rubber_bands.pop(aoi_id, None)
        if rb is not None:
            self._rubber_bands[new_aoi["id"]] = rb
        # Update queue entry in place
        aoi_entry.update({"id": new_aoi["id"], "name": new_aoi["name"], "tag": "saved"})
        self._rebuild_queue_list()
        self._refresh_library_tree()
        self.iface.messageBar().pushMessage(
            "PWTT", f'AOI "{name.strip()}" saved to library.', level=Qgis.Success, duration=4,
        )
```

- [ ] **Step 4: Run the full test suite one final time**

```
python -m pytest tests/test_aoi_store.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat(ui): update export/import UI and queue save dialog for project support"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task covering it |
|-----------------|-----------------|
| Flat project grouping (single level) | Tasks 2, 5 |
| Every AOI belongs to one project | Task 3 (`save_aoi` auto-creates Default) |
| Migrate existing AOIs → Default project | Task 1 (`_read_raw` migration) |
| `make_project`, `load_projects`, `save_project`, `delete_project` | Task 2 |
| `move_aoi` | Task 3 |
| `export_project_to_file` | Task 4 |
| Updated `import_aois_from_file` | Task 4 |
| QTreeWidget with project/AOI items | Task 5 |
| "New project…" button | Task 5, 6 |
| Load/Rename/Delete context-sensitive buttons | Task 6 |
| Right-click context menu | Task 7 |
| Drag-and-drop reparenting | Task 5 (`_LibraryTree`), Task 7 (`_lib_move_aoi`) |
| "Move to project…" sub-menu | Task 7 |
| Last-project guard | Task 2 |
| Orphan repair | Task 1 |
| Single-project export/import | Tasks 4, 8 |
| Import dialog for project selection | Task 8 |
| Queue save dialog with project selector | Task 8 |
| All existing tests continue to pass | Verified in Tasks 3, 4 |

No gaps found.

**Type consistency:**
- `_LibraryTree.aoi_moved` signal → connected to `_lib_move_aoi(aoi_id, project_id)` ✓
- `item.data(0, Qt.UserRole)` → dict with `type` and `id` keys used consistently ✓
- `aoi_store.move_aoi(aoi_id, target_project_id)` matches call-sites in Task 7 ✓
- `aoi_store.export_project_to_file(project_id, path)` matches call-site in Task 8 ✓
- `aoi_store.import_aois_from_file(path, target_project_id=...)` matches call-site in Task 8 ✓
