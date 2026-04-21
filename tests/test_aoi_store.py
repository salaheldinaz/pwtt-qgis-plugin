import json
import os
import sys
import tempfile
import types
import pytest

# Point the store at a temp directory created via tempfile
_tmpdir = tempfile.mkdtemp(prefix="pwtt_test_")

# Stub out qgis.core so aoi_store can be imported without a running QGIS instance
qgis_core = types.ModuleType("qgis.core")


class _FakeApp:
    @staticmethod
    def qgisSettingsDirPath():
        return _tmpdir


qgis_core.QgsApplication = _FakeApp
sys.modules.setdefault("qgis", types.ModuleType("qgis"))
sys.modules["qgis.core"] = qgis_core

# Now import
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import importlib  # noqa: E402
import core.aoi_store as aoi_store  # noqa: E402
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
    aoi = aoi_store.make_aoi("X", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))", [0, 0, 1, 1], project_id="p1")
    for field in ("id", "project_id", "name", "wkt", "bbox", "created_at"):
        assert field in aoi
    assert aoi["id"]
    assert len(aoi["id"]) == 8


def test_migration_v1_to_v2(tmp_path):
    """A bare JSON array (v1) is migrated to v2 on first read."""
    _fresh()
    import importlib
    import json
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


def test_orphan_repair(tmp_path):
    """AOIs with unknown project_id are reassigned to first project on load."""
    import json
    _fresh()
    p = os.path.join(_tmpdir, "PWTT", "saved_aois.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
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
    with open(p) as f:
        saved = json.load(f)
    assert saved["aois"][0]["project_id"] == "proj1"


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
    assert len(loaded) == 1
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
    pid = aois[0]["project_id"]
    assert any(p["id"] == pid for p in projects)


def test_import_geojson_feature_collection(tmp_path):
    _fresh()
    path = str(tmp_path / "zones.geojson")
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Alpha", "id": "A1"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"id": "B-only"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]],
                },
            },
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 2
    assert result["skipped_invalid"] == 0
    aois = {a["name"]: a for a in aoi_store.load_aois()}
    assert "Alpha" in aois
    assert "B-only" in aois
    assert aois["Alpha"]["bbox"] == [0.0, 0.0, 1.0, 1.0]
    assert "Polygon" in aois["Alpha"]["wkt"]


def test_import_geojson_single_feature(tmp_path):
    _fresh()
    path = str(tmp_path / "one.json")
    feat = {
        "type": "Feature",
        "properties": {"name": "Solo"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[10, 20], [11, 20], [11, 21], [10, 21], [10, 20]]],
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feat, f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 1
    assert aoi_store.load_aois()[0]["name"] == "Solo"


def test_import_geojson_multipolygon(tmp_path):
    _fresh()
    path = str(tmp_path / "multi.json")
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "MP"},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                        [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]],
                    ],
                },
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 1
    wkt = aoi_store.load_aois()[0]["wkt"]
    assert wkt.startswith("MultiPolygon")
    assert aoi_store.load_aois()[0]["bbox"] == [0.0, 0.0, 3.0, 1.0]


def test_import_geojson_skips_and_counts(tmp_path):
    _fresh()
    path = str(tmp_path / "mixed.json")
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": None, "properties": {}},
            {
                "type": "Feature",
                "properties": {"name": "Ok"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]]],
                },
            },
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}},
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 1
    assert result["skipped_invalid"] == 2
    assert aoi_store.load_aois()[0]["name"] == "Ok"


def test_import_geojson_all_invalid_no_new_project(tmp_path):
    _fresh()
    proj = aoi_store.make_project("Existing")
    aoi_store.save_project(proj)
    path = str(tmp_path / "bad_fc.json")
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": None, "properties": {}},
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 0
    assert result["skipped_invalid"] == 1
    assert len(aoi_store.load_projects()) == 1


def test_import_geojson_into_target_project(tmp_path):
    _fresh()
    proj = aoi_store.make_project("Dest")
    aoi_store.save_project(proj)
    path = str(tmp_path / "fc.json")
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "InDest"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            },
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    result = aoi_store.import_aois_from_file(path, target_project_id=proj["id"])
    assert result["added"] == 1
    loaded = aoi_store.load_aois(project_id=proj["id"])
    assert len(loaded) == 1
    assert loaded[0]["name"] == "InDest"


def test_import_into_target_project(tmp_path):
    _fresh()
    proj = aoi_store.make_project("Existing")
    aoi_store.save_project(proj)
    proj2 = aoi_store.make_project("FromFile")
    aoi_store.save_project(proj2)
    aoi = aoi_store.make_aoi("A", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                             [0, 0, 1, 1], project_id=proj2["id"])
    aoi_store.save_aoi(aoi)
    path = str(tmp_path / "proj2.json")
    aoi_store.export_project_to_file(proj2["id"], path)

    result = aoi_store.import_aois_from_file(path, target_project_id=proj["id"])
    assert result["added"] == 1
    loaded = aoi_store.load_aois(project_id=proj["id"])
    assert any(a["name"] == "A" for a in loaded)


def test_import_full_export_multi_project(tmp_path):
    """Full export with multiple projects is imported with correct project mapping."""
    _fresh()
    # Create two projects with one AOI each
    p1 = aoi_store.make_project("Alpha")
    p2 = aoi_store.make_project("Beta")
    aoi_store.save_project(p1)
    aoi_store.save_project(p2)
    a1 = aoi_store.make_aoi("AOI-A", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                            [0, 0, 1, 1], project_id=p1["id"])
    a2 = aoi_store.make_aoi("AOI-B", "Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))",
                            [0, 0, 1, 1], project_id=p2["id"])
    aoi_store.save_aoi(a1)
    aoi_store.save_aoi(a2)
    path = str(tmp_path / "full.json")
    count = aoi_store.export_aois_to_file(path)
    assert count == 2

    # Import into a fresh store
    _fresh()
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 2

    projects = aoi_store.load_projects()
    assert len(projects) == 2
    project_names = {p["name"] for p in projects}
    assert project_names == {"Alpha", "Beta"}

    aois = aoi_store.load_aois()
    assert len(aois) == 2
    # Each AOI must be assigned to the project matching its original name
    alpha_id = next(p["id"] for p in projects if p["name"] == "Alpha")
    beta_id = next(p["id"] for p in projects if p["name"] == "Beta")
    aoi_a = next(a for a in aois if a["name"] == "AOI-A")
    aoi_b = next(a for a in aois if a["name"] == "AOI-B")
    assert aoi_a["project_id"] == alpha_id
    assert aoi_b["project_id"] == beta_id


def test_import_flat_array_all_invalid_is_noop(tmp_path):
    """Flat-array import with all invalid AOIs must not create a project."""
    _fresh()
    proj = aoi_store.make_project("Existing")
    aoi_store.save_project(proj)
    path = str(tmp_path / "invalid.json")
    with open(path, "w") as f:
        json.dump([{"id": "bad1"}, {"id": "bad2", "name": "No WKT"}], f)
    result = aoi_store.import_aois_from_file(path)
    assert result["added"] == 0
    assert result["skipped_invalid"] == 2
    projects = aoi_store.load_projects()
    assert len(projects) == 1  # no new project created


def test_save_project_duplicate_name_raises():
    _fresh()
    proj1 = aoi_store.make_project("MyProject")
    aoi_store.save_project(proj1)
    proj2 = aoi_store.make_project("MyProject")
    with pytest.raises(ValueError, match="already exists"):
        aoi_store.save_project(proj2)


def test_save_project_duplicate_name_case_insensitive_raises():
    _fresh()
    proj1 = aoi_store.make_project("myproject")
    aoi_store.save_project(proj1)
    proj2 = aoi_store.make_project("MYPROJECT")
    with pytest.raises(ValueError, match="already exists"):
        aoi_store.save_project(proj2)


def test_save_project_rename_to_own_name_allowed():
    """Updating a project's other fields (not renaming to conflict) must succeed."""
    _fresh()
    proj = aoi_store.make_project("Alpha")
    aoi_store.save_project(proj)
    proj["name"] = "Alpha"  # same name, same id — should not raise
    aoi_store.save_project(proj)
    assert aoi_store.load_projects()[0]["name"] == "Alpha"
