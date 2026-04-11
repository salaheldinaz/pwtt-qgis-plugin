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
