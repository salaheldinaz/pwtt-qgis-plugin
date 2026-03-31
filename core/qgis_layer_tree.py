# -*- coding: utf-8 -*-
"""Layer tree: group PWTT outputs by job id and tag layers with backend (gee / local / openeo)."""

from datetime import date
from typing import Optional

from qgis.core import QgsLayerTreeGroup, QgsLayerTreeNode, QgsMapLayer, QgsProject

_LOCAL_GRD_SHORT = {"cdse": "CDSE", "asf": "ASF", "pc": "PC"}


def local_grd_source_short(source: Optional[str]) -> str:
    """Short label for GRD catalog (cdse/asf/pc) used in logs and layer names."""
    s = (source or "cdse").strip().lower()
    return _LOCAL_GRD_SHORT.get(s, s.upper() if s else "CDSE")


def pwtt_backend_display_segment(
    backend_id: Optional[str], data_source: Optional[str] = None
) -> str:
    """Layer/log segment: ``local - CDSE`` style for local backend; else ``openeo`` / ``gee``."""
    bid = (backend_id or "pwtt").lower()
    if bid == "local":
        return f"local - {local_grd_source_short(data_source)}"
    return bid


def job_backend_log_label(job: dict) -> str:
    """Human-oriented backend line for Jobs log / table (openEO, GEE, local - CDSE, …)."""
    bid = job.get("backend_id") or ""
    if bid == "local":
        return f"local - {local_grd_source_short(job.get('data_source'))}"
    return {"openeo": "openEO", "gee": "GEE"}.get(bid, bid)


def pwtt_job_group_name(
    job_id: Optional[str],
    backend_id: Optional[str],
    data_source: Optional[str] = None,
) -> str:
    seg = pwtt_backend_display_segment(backend_id, data_source)
    jid = job_id or "?"
    return f"PWTT {seg} ({jid})"


def pwtt_damage_layer_name(
    job_id: Optional[str],
    backend_id: Optional[str],
    data_source: Optional[str] = None,
) -> str:
    seg = pwtt_backend_display_segment(backend_id, data_source)
    if job_id:
        return f"PWTT damage ({seg}, {job_id})"
    return f"PWTT damage ({seg})"


_FOOTPRINT_SOURCE_LABELS = {
    "current_osm": "OSM",
    "historical_war_start": "OSM @ war start",
    "historical_inference_start": "OSM @ inference start",
}


def footprint_snapshot_date_iso(
    source: Optional[str],
    war_start: Optional[str] = None,
    inference_start: Optional[str] = None,
) -> str:
    """YYYY-MM-DD for the OSM snapshot: historical sources use job dates; else today (current OSM)."""
    ws = (war_start or "")[:10]
    ins = (inference_start or "")[:10]
    if source == "historical_war_start" and ws:
        return ws
    if source == "historical_inference_start" and ins:
        return ins
    return date.today().isoformat()


def pwtt_footprints_layer_name(
    job_id: Optional[str],
    backend_id: Optional[str],
    source: Optional[str] = None,
    *,
    data_source: Optional[str] = None,
    war_start: Optional[str] = None,
    inference_start: Optional[str] = None,
    snapshot_date: Optional[str] = None,
) -> str:
    seg = pwtt_backend_display_segment(backend_id, data_source)
    src = _FOOTPRINT_SOURCE_LABELS.get(source, source or "OSM")
    snap = snapshot_date or footprint_snapshot_date_iso(source, war_start, inference_start)
    if job_id:
        return f"PWTT footprints {src} ({seg}, {job_id}) · {snap}"
    return f"PWTT footprints {src} ({seg}) · {snap}"


def _find_group_by_name(root: QgsLayerTreeNode, name: str) -> Optional[QgsLayerTreeGroup]:
    # Use children() not childCount()/child(): QgsLayerTree root lacks those in PyQGIS 3.44+.
    for node in root.children():
        if isinstance(node, QgsLayerTreeGroup) and node.name() == name:
            return node
    return None


def get_or_create_pwtt_job_group(
    project: QgsProject,
    job_id: Optional[str],
    backend_id: Optional[str],
    data_source: Optional[str] = None,
) -> QgsLayerTreeGroup:
    root = project.layerTreeRoot()
    name = pwtt_job_group_name(job_id, backend_id, data_source)
    existing = _find_group_by_name(root, name)
    if existing is not None:
        return existing
    grp = QgsLayerTreeGroup(name)
    root.insertChildNode(0, grp)
    return grp


def add_map_layer_to_pwtt_job_group(
    project: QgsProject,
    layer: QgsMapLayer,
    job_id: Optional[str],
    backend_id: Optional[str],
    data_source: Optional[str] = None,
) -> None:
    group = get_or_create_pwtt_job_group(project, job_id, backend_id, data_source)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
