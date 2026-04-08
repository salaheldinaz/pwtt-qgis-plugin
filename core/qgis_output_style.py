# -*- coding: utf-8 -*-
"""Default QGIS styling and abstracts for PWTT raster and footprint layers."""

import json
import os

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsSymbol,
)

from .viz_constants import (
    T_STATISTIC_VIZ_HEX_HIGH,
    T_STATISTIC_VIZ_HEX_LOW,
    T_STATISTIC_VIZ_HEX_MID,
    T_STATISTIC_VIZ_MAX,
    T_STATISTIC_VIZ_MIN,
    T_STATISTIC_VIZ_OPACITY,
)

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
        "Default styling: singleband pseudocolor on band 1 (T_statistic), yellow→red→purple, "
        f"min {T_STATISTIC_VIZ_MIN:g} / max {T_STATISTIC_VIZ_MAX:g} / opacity "
        f"{int(T_STATISTIC_VIZ_OPACITY * 100)}% — same stretch as code/pwtt.py Earth Engine viz. "
        "Higher T in that stretch is more purple, lower is more yellow. "
        "Change symbology in layer properties to view multiband RGB or other bands.\n\n"
        "Bands:\n"
        "• Band 1 — T_statistic: smoothed pixel-wise t-test strength (higher = larger "
        "pre- vs post-change in Sentinel-1 backscatter).\n"
        "• Band 2 — damage: binary mask where T_statistic exceeds the T-statistic cutoff "
        f"used for this run ({thr:g}; stored as damage_threshold in job metadata), else 0. "
        "This is a test-statistic cutoff, not a damage probability; a higher cutoff flags "
        "fewer pixels (stricter mask).\n"
        "• Band 3 — p_value: approximate two-tailed p-value (normal approximation); "
        "not interpreted as P(damage) per pixel.\n\n"
        "Other symbology options:\n"
        "• Singleband gray on band 1 if you prefer a neutral ramp.\n"
        "• Singleband gray or paletted on band 2 for the binary damage mask.\n\n"
        "Recommended cutoffs (UNOSAT footprint validation; precision/recall tradeoff; "
        'upstream PWTT calls these "thresholds"):\n'
        "T > 2 — max sensitivity / screening; T > 3.3 — balanced default; "
        "T > 4 — fewer false positives; T > 5 — only strongest changes.\n"
        f"Details: {PWTT_THRESHOLDS_URL}\n"
    )


def _pwtt_tstatistic_color_ramp():
    """Gradient ramp (0–1) with mid stop — same hues as viz_constants hex stops."""
    return QgsGradientColorRamp(
        QColor(T_STATISTIC_VIZ_HEX_LOW),
        QColor(T_STATISTIC_VIZ_HEX_HIGH),
        False,
        [QgsGradientStop(0.5, QColor(T_STATISTIC_VIZ_HEX_MID))],
    )


def _pwtt_manual_color_ramp_shader():
    """Fallback when createShader is missing or fails (older QGIS)."""
    ramp_fn = QgsColorRampShader()
    ramp_fn.setColorRampType(QgsColorRampShader.Interpolated)
    ramp_fn.setMinimumValue(T_STATISTIC_VIZ_MIN)
    ramp_fn.setMaximumValue(T_STATISTIC_VIZ_MAX)
    mid = (T_STATISTIC_VIZ_MIN + T_STATISTIC_VIZ_MAX) / 2.0
    items = [
        QgsColorRampShader.ColorRampItem(
            T_STATISTIC_VIZ_MIN, QColor(T_STATISTIC_VIZ_HEX_LOW), ""
        ),
        QgsColorRampShader.ColorRampItem(mid, QColor(T_STATISTIC_VIZ_HEX_MID), ""),
        QgsColorRampShader.ColorRampItem(
            T_STATISTIC_VIZ_MAX, QColor(T_STATISTIC_VIZ_HEX_HIGH), ""
        ),
    ]
    ramp_fn.setColorRampItemList(items)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp_fn)
    return shader


def _pwtt_pseudocolor_renderer(layer):
    """Renderer the symbology panel understands; fixed 3–5 stretch (not NaN / stats)."""
    provider = layer.dataProvider()
    renderer = QgsSingleBandPseudoColorRenderer(provider, 1, None)

    # MUST set before createShader(): QGIS builds QgsColorRampShader from
    # classificationMin/Max; if they are still the default NaN, ramp items and
    # labels stay NaN and nothing useful renders.
    renderer.setClassificationMin(T_STATISTIC_VIZ_MIN)
    renderer.setClassificationMax(T_STATISTIC_VIZ_MAX)

    try:
        mmo = QgsRasterMinMaxOrigin()
        mmo.setLimits(Qgis.RasterRangeLimit.NotSet)
        renderer.setMinMaxOrigin(mmo)
    except AttributeError:
        pass

    ramp = _pwtt_tstatistic_color_ramp()
    if hasattr(renderer, "createShader"):
        try:
            renderer.createShader(
                ramp,
                Qgis.ShaderInterpolationMethod.Linear,
                Qgis.ShaderClassificationMethod.Continuous,
                0,
                False,
            )
        except (AttributeError, TypeError):
            renderer.setShader(_pwtt_manual_color_ramp_shader())
    else:
        renderer.setShader(_pwtt_manual_color_ramp_shader())

    # Sync shader after createShader (covers edge cases where ramp steps diverge).
    renderer.setClassificationMin(T_STATISTIC_VIZ_MIN)
    renderer.setClassificationMax(T_STATISTIC_VIZ_MAX)
    renderer.setOpacity(1.0)

    return renderer


def style_pwtt_raster_layer(layer, damage_threshold: float = 3.3) -> None:
    """Band 1 pseudocolor matching code/pwtt.py GEE viz; abstract; opacity."""
    if not layer or not layer.isValid():
        return
    layer.setAbstract(pwtt_raster_abstract(damage_threshold))
    if layer.bandCount() >= 1:
        renderer = _pwtt_pseudocolor_renderer(layer)
        layer.setRenderer(renderer)
    # Apply after renderer so QGIS does not reset it; symbology uses full-strength
    # colors and this matches the global layer opacity control.
    layer.setOpacity(T_STATISTIC_VIZ_OPACITY)
    layer.triggerRepaint()


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
