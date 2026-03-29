# -*- coding: utf-8 -*-
"""Layer tree: group PWTT outputs by job id and tag layers with backend (gee / local / openeo)."""

from typing import Optional

from qgis.core import QgsLayerTreeGroup, QgsLayerTreeNode, QgsMapLayer, QgsProject


def pwtt_job_group_name(job_id: Optional[str], backend_id: Optional[str]) -> str:
    bid = (backend_id or "pwtt").lower()
    jid = job_id or "?"
    return f"PWTT {bid} ({jid})"


def pwtt_damage_layer_name(job_id: Optional[str], backend_id: Optional[str]) -> str:
    bid = (backend_id or "pwtt").lower()
    if job_id:
        return f"PWTT damage ({bid}, {job_id})"
    return f"PWTT damage ({bid})"


_FOOTPRINT_SOURCE_LABELS = {
    "current_osm": "OSM",
    "historical_war_start": "OSM @ war start",
    "historical_inference_start": "OSM @ inference start",
}


def pwtt_footprints_layer_name(
    job_id: Optional[str], backend_id: Optional[str], source: Optional[str] = None
) -> str:
    bid = (backend_id or "pwtt").lower()
    src = _FOOTPRINT_SOURCE_LABELS.get(source, source or "OSM")
    if job_id:
        return f"PWTT footprints {src} ({bid}, {job_id})"
    return f"PWTT footprints {src} ({bid})"


def _find_group_by_name(root: QgsLayerTreeNode, name: str) -> Optional[QgsLayerTreeGroup]:
    # Use children() not childCount()/child(): QgsLayerTree root lacks those in PyQGIS 3.44+.
    for node in root.children():
        if isinstance(node, QgsLayerTreeGroup) and node.name() == name:
            return node
    return None


def get_or_create_pwtt_job_group(project: QgsProject, job_id: Optional[str], backend_id: Optional[str]) -> QgsLayerTreeGroup:
    root = project.layerTreeRoot()
    name = pwtt_job_group_name(job_id, backend_id)
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
) -> None:
    group = get_or_create_pwtt_job_group(project, job_id, backend_id)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
