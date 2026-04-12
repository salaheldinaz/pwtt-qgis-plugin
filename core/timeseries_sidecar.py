# -*- coding: utf-8 -*-
"""Per-image SAR z-score time-series sidecar writer/reader.

Each successful GEE or Local run writes two files next to the result GeoTIFF:

- pwtt_<job_id>_timeseries.json  — canonical structured record
- pwtt_<job_id>_timeseries.csv   — EE Code Editor-compatible CSV

The chart dialog (ui/timeseries_dialog.py) reads the JSON sidecar.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
from typing import Iterable, List, Optional

from .viz_constants import TIMESERIES_Z_THRESHOLD


_EE_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def sidecar_json_path(output_tif: str) -> str:
    base, _ext = os.path.splitext(output_tif)
    return base + "_timeseries.json"


def sidecar_csv_path(output_tif: str) -> str:
    base, _ext = os.path.splitext(output_tif)
    return base + "_timeseries.csv"


def _ee_date_display(iso: str) -> str:
    """'2025-03-07T04:12:03' → 'Mar 7, 2025' (matches EE Code Editor chart export)."""
    if not iso:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        # Fallback: best-effort truncation for odd formats
        try:
            dt = _dt.datetime.strptime(iso[:10], "%Y-%m-%d")
        except ValueError:
            return iso
    return f"{_EE_MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def _json_safe_num(value) -> Optional[float]:
    """Convert floats with NaN/inf to JSON null (None), leave finite floats alone."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def build_sidecar(
    *,
    job_id: str,
    backend: str,
    aoi_wkt: str,
    war_start: str,
    inference_start: str,
    pre_interval_months: int,
    post_interval_months: int,
    normalization: str,
    series: Iterable[dict],
) -> dict:
    """Assemble the canonical JSON payload from per-image entries.

    Each entry in *series* should carry: date (ISO str), orbit (int or None),
    pass (str or None), VV_z (float or NaN), VH_z (float or NaN), period ('pre'|'post').
    """
    cleaned: List[dict] = []
    for entry in series:
        cleaned.append(
            {
                "date": entry.get("date") or "",
                "orbit": entry.get("orbit"),
                "pass": entry.get("pass"),
                "VV_z": _json_safe_num(entry.get("VV_z")),
                "VH_z": _json_safe_num(entry.get("VH_z")),
                "period": entry.get("period") or "",
            }
        )
    cleaned.sort(key=lambda e: e["date"] or "")
    return {
        "job_id": job_id,
        "backend": backend,
        "aoi_wkt": aoi_wkt,
        "war_start": war_start,
        "inference_start": inference_start,
        "pre_interval_months": int(pre_interval_months),
        "post_interval_months": int(post_interval_months),
        "normalization": normalization,
        "thresholds": {
            "z_lower_99": -TIMESERIES_Z_THRESHOLD,
            "z_upper_99": TIMESERIES_Z_THRESHOLD,
        },
        "series": cleaned,
    }


def write_sidecars(output_tif: str, payload: dict) -> tuple[str, str]:
    """Write JSON + CSV sidecars next to *output_tif*. Returns (json_path, csv_path)."""
    json_path = sidecar_json_path(output_tif)
    csv_path = sidecar_csv_path(output_tif)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    thr = TIMESERIES_Z_THRESHOLD
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["system:time_start", "VH", "VV", "z_lower_99", "z_upper_99"])
        for entry in payload.get("series", []):
            vv = entry.get("VV_z")
            vh = entry.get("VH_z")
            writer.writerow(
                [
                    _ee_date_display(entry.get("date") or ""),
                    "" if vh is None else f"{vh:g}",
                    "" if vv is None else f"{vv:g}",
                    f"{-thr:g}",
                    f"{thr:g}",
                ]
            )
    return json_path, csv_path


def read_sidecar(output_tif: str) -> Optional[dict]:
    """Read the JSON sidecar next to *output_tif*, or None if missing/corrupt."""
    path = sidecar_json_path(output_tif)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
