# -*- coding: utf-8 -*-
"""Shared dock helpers and constants used by multiple PWTT dock widgets."""

import os

BACKENDS = [
    ("openeo", "openEO"),
    ("gee", "Google Earth Engine"),
    ("local", "Local Processing"),
]

STATUS_LABELS = {
    "pending": "Pending",
    "running": "Running\u2026",
    "waiting_orders": "Waiting",
    "stopped": "Stopped",
    "completed": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
STATUS_COLORS = {
    "pending": "#888",
    "running": "#2196F3",
    "waiting_orders": "#FF9800",
    "stopped": "#888",
    "completed": "#4CAF50",
    "failed": "#F44336",
    "cancelled": "#888",
}


def offline_grd_catalog_rows(job: dict) -> list:
    """Merge stored product ids with optional name/date metadata for display."""
    ids = list(job.get("offline_product_ids") or [])
    raw = job.get("offline_products")
    if not isinstance(raw, list):
        raw = []
    by_id = {
        p["id"]: p
        for p in raw
        if isinstance(p, dict) and p.get("id")
    }
    return [
        {
            "id": pid,
            "name": str(by_id.get(pid, {}).get("name", "")),
            "date": str(by_id.get(pid, {}).get("date", "")),
        }
        for pid in ids
    ]


def read_plugin_version(plugin_dir):
    """Single source of truth: pwtt_qgis/metadata.txt version= line."""
    if not plugin_dir:
        return None
    meta = os.path.join(plugin_dir, "metadata.txt")
    try:
        with open(meta, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def dock_title(base, plugin_dir):
    v = read_plugin_version(plugin_dir)
    return f"{base} ({v})" if v else base


def job_footprints_sources(job: dict) -> list:
    """Return footprints_sources for a job, with backwards-compat fallback."""
    sources = job.get("footprints_sources")
    if sources:
        return list(sources)
    return ["current_osm"] if job.get("include_footprints") else []
