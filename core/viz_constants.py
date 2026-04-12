# -*- coding: utf-8 -*-
"""T_statistic display range for GEE tiles and QGIS raster styling — matches code/pwtt.py."""

T_STATISTIC_VIZ_MIN = 3.0
T_STATISTIC_VIZ_MAX = 5.0
T_STATISTIC_VIZ_OPACITY = 0.5

# Interpolated stops at min / midpoint / max (same hues as EE palette yellow, red, purple).
T_STATISTIC_VIZ_HEX_LOW = "#ffff00"
T_STATISTIC_VIZ_HEX_MID = "#ff0000"
T_STATISTIC_VIZ_HEX_HIGH = "#800080"

# Per-image orbit-normalized z-score time series (sidecar + chart dialog).
# Two-tailed 99% CI for a standard normal — matches the EE Code Editor chart export.
TIMESERIES_Z_THRESHOLD = 2.576
# Series colors echo the attached reference chart (blue VV, orange VH).
TIMESERIES_VV_COLOR = "#1f77b4"
TIMESERIES_VH_COLOR = "#ff7f0e"
TIMESERIES_THRESHOLD_COLOR = "#555555"
TIMESERIES_WAR_START_COLOR = "#b71c1c"
