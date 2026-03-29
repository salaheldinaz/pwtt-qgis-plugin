# -*- coding: utf-8 -*-
"""Persistent job store: save, load, update PWTT analysis jobs."""

import json
import os
import uuid
from datetime import datetime
from typing import List, Optional

from qgis.core import QgsApplication

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
