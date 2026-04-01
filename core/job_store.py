# -*- coding: utf-8 -*-
"""Persistent job store: save, load, update PWTT analysis jobs."""

import copy
import json
import os
import uuid
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

PWTT_JOBS_EXPORT_FORMAT = "pwtt_jobs_export"
PWTT_JOBS_EXPORT_VERSION = 1

# ── Status constants ─────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_ORDERS = "waiting_orders"
STATUS_STOPPED = "stopped"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
RESUMABLE_STATUSES = {STATUS_STOPPED, STATUS_WAITING_ORDERS, STATUS_FAILED}


# ── File I/O ─────────────────────────────────────────────────────────────────
def _jobs_path() -> str:
    from qgis.core import QgsApplication
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "PWTT")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "jobs.json")


def _read_raw() -> List[dict]:
    p = _jobs_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _write(jobs: List[dict]):
    with open(_jobs_path(), "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


# ── CRUD ─────────────────────────────────────────────────────────────────────
def create_job(
    backend_id: str,
    aoi_wkt: str,
    war_start: str,
    inference_start: str,
    pre_interval: int,
    post_interval: int,
    output_dir: str,
    include_footprints: bool,
    footprints_sources=None,
    damage_threshold: float = 3.3,
    gee_viz: bool = False,
    data_source: str = "cdse",
) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    if footprints_sources is None:
        footprints_sources = ["current_osm"] if include_footprints else []
    return {
        "id": uuid.uuid4().hex[:8],
        "backend_id": backend_id,
        "aoi_wkt": aoi_wkt,
        "war_start": war_start,
        "inference_start": inference_start,
        "pre_interval": pre_interval,
        "post_interval": post_interval,
        "output_dir": output_dir,
        "include_footprints": include_footprints,
        "footprints_sources": list(footprints_sources),
        "damage_threshold": float(damage_threshold),
        "gee_viz": bool(gee_viz),
        # Added in v0.1.44; older jobs.json entries lack this field.
        # Callers use .get("data_source") or "cdse" for backward compatibility.
        "data_source": (data_source or "cdse").strip().lower()
        if backend_id == "local"
        else "cdse",
        "status": STATUS_PENDING,
        "created_at": now,
        "updated_at": now,
        "error": None,
        "offline_product_ids": [],
        "offline_products": [],
        "remote_job_id": None,
        "output_tif": None,
        "footprints_gpkg": None,
        "footprints_gpkgs": {},
        # HTML-ish lines for Jobs dock + "View logs" (older jobs.json entries omit this).
        "activity_log": [],
    }


def load_jobs() -> List[dict]:
    return _read_raw()


def save_job(job: dict):
    """Insert or update a job (matched by id)."""
    jobs = _read_raw()
    for i, j in enumerate(jobs):
        if j["id"] == job["id"]:
            jobs[i] = job
            _write(jobs)
            return
    jobs.insert(0, job)
    _write(jobs)


def update_job(job_id: str, **fields):
    jobs = _read_raw()
    for j in jobs:
        if j["id"] == job_id:
            j.update(fields)
            j["updated_at"] = datetime.now().isoformat(timespec="seconds")
            break
    _write(jobs)


def get_job(job_id: str) -> Optional[dict]:
    for j in _read_raw():
        if j["id"] == job_id:
            return j
    return None


def delete_job(job_id: str):
    jobs = [j for j in _read_raw() if j["id"] != job_id]
    _write(jobs)


def recover_stale_jobs():
    """Reset 'running' jobs to 'stopped' (called once at plugin startup)."""
    jobs = _read_raw()
    changed = False
    for j in jobs:
        if j["status"] == STATUS_RUNNING:
            j["status"] = STATUS_STOPPED
            j["updated_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if changed:
        _write(jobs)


def _jobs_list_from_parsed_json(data: Any) -> List[dict]:
    """Resolve a jobs list from export payload or a raw JSON array."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if data.get("format") == PWTT_JOBS_EXPORT_FORMAT and isinstance(data.get("jobs"), list):
            return [x for x in data["jobs"] if isinstance(x, dict)]
        if isinstance(data.get("jobs"), list):
            return [x for x in data["jobs"] if isinstance(x, dict)]
    raise ValueError("Unrecognized jobs file (expected PWTT export or a JSON array of job objects).")


def export_jobs_to_file(path: str) -> int:
    """Write all stored jobs to *path* as PWTT export JSON. Returns job count."""
    jobs = _read_raw()
    payload = {
        "format": PWTT_JOBS_EXPORT_FORMAT,
        "version": PWTT_JOBS_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": jobs,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(jobs)


def export_single_job_zip(job: dict, dest_path: str) -> dict:
    """Write job metadata + output files to a zip archive.

    Files are stored flat (no subdirectory) so they can be found by name on import.
    Returns {"files_included": n, "files_missing": m}.
    """
    payload = {
        "format": PWTT_JOBS_EXPORT_FORMAT,
        "version": PWTT_JOBS_EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": [job],
    }
    json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    # Collect output file paths: arcname (flat) → source path.
    # Basenames are used as flat arcnames. If two fields resolve to the same
    # basename (e.g. footprints_gpkg and a footprints_gpkgs entry pointing to
    # the same file), the last path wins — they are the same physical file.
    output_files = {}
    for field in ("output_tif", "footprints_gpkg"):
        p = (job.get(field) or "").strip()
        if p:
            output_files[os.path.basename(p)] = p
    for p in (job.get("footprints_gpkgs") or {}).values():
        p = (p or "").strip()
        if p:
            output_files[os.path.basename(p)] = p

    included = 0
    missing = 0
    with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("job.json", json_bytes)
        for arcname, src_path in output_files.items():
            if os.path.isfile(src_path):
                zf.write(src_path, arcname)
                included += 1
            else:
                missing += 1

    return {"files_included": included, "files_missing": missing}


def merge_jobs_from_file(path: str) -> Dict[str, int]:
    """Append jobs from an export or legacy jobs.json array; avoid id collisions.

    Returns counts: added, skipped_invalid, ids_rewritten.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    incoming = _jobs_list_from_parsed_json(data)

    existing = _read_raw()
    used_ids = {j["id"] for j in existing if j.get("id")}
    added = 0
    skipped_invalid = 0
    ids_rewritten = 0

    for raw in incoming:
        if not raw.get("backend_id"):
            skipped_invalid += 1
            continue
        job = copy.deepcopy(raw)
        oid = job.get("id")
        if not oid or not isinstance(oid, str):
            job["id"] = uuid.uuid4().hex[:8]
        while job["id"] in used_ids:
            job["id"] = uuid.uuid4().hex[:8]
            ids_rewritten += 1
        used_ids.add(job["id"])
        if job.get("status") == STATUS_RUNNING:
            job["status"] = STATUS_STOPPED
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if not isinstance(job.get("activity_log"), list):
            job["activity_log"] = list(job.get("activity_log") or [])
        existing.insert(0, job)
        added += 1

    if added:
        _write(existing)
    return {
        "added": added,
        "skipped_invalid": skipped_invalid,
        "ids_rewritten": ids_rewritten,
    }
