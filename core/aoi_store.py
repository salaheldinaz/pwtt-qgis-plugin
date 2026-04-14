# -*- coding: utf-8 -*-
"""Persistent AOI library: save, load, delete, export, import named AOIs."""

import copy
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List

AOI_EXPORT_FORMAT = "pwtt_aois_export"
AOI_EXPORT_VERSION = 2


def _aois_path() -> str:
    from qgis.core import QgsApplication
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "saved_aois.json")


def make_project(name: str) -> dict:
    """Return a new project record (not yet saved to disk)."""
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


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
        project_ids = {proj["id"] for proj in projects if "id" in proj}
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


def make_aoi(name: str, wkt: str, bbox: List[float], project_id: str = None) -> dict:
    """Return a new AOI record (not yet saved to disk)."""
    aoi = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "wkt": wkt,
        "bbox": list(bbox),  # [west, south, east, north]
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if project_id is not None:
        aoi["project_id"] = project_id
    return aoi


def load_projects() -> List[dict]:
    projects, _ = _read_raw()
    return sorted(projects, key=lambda p: p.get("name", "").lower())


def save_project(project: dict):
    """Insert or update project by id. Raises ValueError if another project has the same name."""
    projects, aois = _read_raw()
    pid = project.get("id")
    pname = (project.get("name") or "").strip().lower()
    for p in projects:
        if p.get("id") != pid and (p.get("name") or "").lower() == pname:
            raise ValueError(f"A project named {project.get('name')!r} already exists.")
    for i, p in enumerate(projects):
        if p.get("id") == pid:
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


def load_aois(project_id: str = None) -> List[dict]:
    """Return AOIs. Pass project_id to filter to one project; None returns all."""
    _, aois = _read_raw()
    if project_id is not None:
        return [a for a in aois if a.get("project_id") == project_id]
    return aois


def save_aoi(aoi: dict):
    """Insert or update AOI by id. Auto-creates a Default project if none exist."""
    aoi = dict(aoi)
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
    filtered = [a for a in aois if a["id"] != aoi_id]
    if len(filtered) != len(aois):
        _write(projects, filtered)


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

    project_id_map: Dict[str, str] = {}
    effective_project_id: str = ""

    if target_project_id is not None:
        if target_project_id not in used_project_ids:
            raise ValueError(f"target_project_id {target_project_id!r} does not exist.")
        effective_project_id = target_project_id

    elif incoming_project is not None:
        # Single-project export: recreate the project
        proj = copy.deepcopy(incoming_project)
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
        # Old flat-array — create an auto-named project only if there are valid AOIs
        if any(a.get("wkt") and a.get("name") for a in incoming_aois):
            auto_name = f"Imported {datetime.now().strftime('%Y-%m-%d')}"
            auto_proj = make_project(auto_name)
            while auto_proj["id"] in used_project_ids:
                auto_proj["id"] = uuid.uuid4().hex[:8]
            used_project_ids.add(auto_proj["id"])
            existing_projects.append(auto_proj)
            effective_project_id = auto_proj["id"]

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
        if aoi["id"] in used_ids:
            ids_rewritten += 1
            while aoi["id"] in used_ids:
                aoi["id"] = uuid.uuid4().hex[:8]
        used_ids.add(aoi["id"])
        existing_aois.insert(0, aoi)
        added += 1

    if added or incoming_projects or incoming_project:
        _write(existing_projects, existing_aois)
    return {"added": added, "skipped_invalid": skipped_invalid, "ids_rewritten": ids_rewritten}
