# -*- coding: utf-8 -*-
"""Default QGIS styling and abstracts for PWTT raster and footprint layers."""

import json
import os

from qgis.PyQt.QtGui import QColor
from qgis.core import QgsSingleSymbolRenderer, QgsSymbol


PWTT_THRESHOLDS_URL = "https://github.com/oballinger/PWTT#recommended-thresholds"


def damage_threshold_from_job_meta(tif_path: str, default: float = 3.3) -> float:
    """Read damage_threshold from adjacent job_info.json if present (plugin runs)."""
    try:
        meta_path = os.path.join(os.path.dirname(tif_path), "job_info.json")
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("damage_threshold")
            if v is not None:
                return float(v)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return float(default)


def pwtt_raster_abstract(damage_threshold: float) -> str:
    """Human-readable band / color meaning for the PWTT GeoTIFF."""
    thr = float(damage_threshold)
    return (
        "PWTT GeoTIFF — what the bands mean\n\n"
        "Typical outputs have three bands (T_statistic, damage, p_value); some backends "
        "may export fewer — check band count in layer properties.\n\n"
        "If QGIS shows this layer as multiband RGB, the on-screen colors are simply "
        "R=band 1, G=band 2, B=band 3. That composite is not a physical damage palette; "
        "use symbology below for interpretation.\n\n"
        "Bands:\n"
        "• Band 1 — T_statistic: smoothed pixel-wise t-test strength (higher = larger "
        "pre- vs post-change in Sentinel-1 backscatter).\n"
        "• Band 2 — damage: 1 where T_statistic exceeds the threshold used for this run "
        f"({thr:g}), else 0.\n"
        "• Band 3 — p_value: approximate two-tailed p-value (normal approximation).\n\n"
        "Suggested symbology:\n"
        "• Singleband pseudocolor or singleband gray on band 1 to read change strength.\n"
        "• Singleband gray or paletted on band 2 for the binary damage mask.\n\n"
        "Recommended T thresholds (validation on UNOSAT footprints; precision/recall tradeoff):\n"
        "T > 2 — max sensitivity / screening; T > 3.3 — balanced default; "
        "T > 4 — fewer false positives; T > 5 — only strongest changes.\n"
        f"Details: {PWTT_THRESHOLDS_URL}\n"
    )


def style_pwtt_raster_layer(layer, damage_threshold: float = 3.3) -> None:
    """Set layer abstract (color/band explainer) and overall opacity for basemap context."""
    if not layer or not layer.isValid():
        return
    layer.setAbstract(pwtt_raster_abstract(damage_threshold))
    layer.setOpacity(0.9)


def style_pwtt_footprints_layer(layer) -> None:
    """Hollow building footprints with a clear outline (no fill)."""
    if not layer or not layer.isValid():
        return
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    for i in range(symbol.symbolLayerCount()):
        sl = symbol.symbolLayer(i)
        if hasattr(sl, "setFillColor"):
            sl.setFillColor(QColor(0, 0, 0, 0))
        if hasattr(sl, "setStrokeColor"):
            sl.setStrokeColor(QColor(35, 35, 40, 255))
        if hasattr(sl, "setStrokeWidth"):
            sl.setStrokeWidth(0.85)
        elif hasattr(sl, "setWidth"):
            sl.setWidth(0.85)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.setAbstract(
        "Building footprints with mean T_statistic per polygon (zonal mean of band 1). "
        "Outlines only — no fill — so the raster stays visible underneath."
    )
    layer.triggerRepaint()
