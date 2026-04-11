# -*- coding: utf-8 -*-
"""Persistent AOI library: save, load, delete, export, import named AOIs."""

import copy
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
