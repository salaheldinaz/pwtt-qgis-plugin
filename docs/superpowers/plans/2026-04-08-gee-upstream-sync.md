# GEE Upstream Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync `core/gee_pwtt.py` to upstream PWTT (`oballinger/PWTT` commit 408f28f) and wire five new detection-method parameters through the backend, task, job-store, and UI layers.

**Architecture:** Replace `gee_pwtt.py` wholesale with the upstream `pwtt/__init__.py`, applying six targeted plugin-specific patches. Thread the five new params (`method`, `ttest_type`, `smoothing`, `mask_before_smooth`, `lee_mode`) bottom-up through `gee_backend.py` → `pwtt_task.py` → `job_store.py` → `jobs_dock.py` → `main_dialog.py`. The UI gains a prominent method dropdown and a collapsed advanced-options section, both GEE-only.

**Tech Stack:** Python 3, PyQt5/QGIS PyQt, Google Earth Engine Python API, `ee.Image` server-side operations.

---

## File Map

| File | Change |
|------|--------|
| `core/gee_pwtt.py` | Full replacement (706-line upstream + 6 patches) |
| `core/gee_backend.py` | Add 5 params to `run()` |
| `core/pwtt_task.py` | Add 5 params to `__init__`, `run_kwargs`, metadata dict |
| `core/job_store.py` | Add 5 params to `create_job()` |
| `ui/jobs_dock.py` | Pass 5 params from job dict to `PWTTRunTask` |
| `ui/main_dialog.py` | Method group + advanced group + settings + replay + `_run_job` |

---

## Task 1: Replace core/gee_pwtt.py

**Files:**
- Modify: `core/gee_pwtt.py`
- Reference: `/tmp/pwtt-upstream/pwtt/__init__.py` (cloned upstream, 706 lines)

The upstream file is the authoritative source. Copy it, then apply six targeted patches listed below.

- [ ] **Step 1.1 — Copy upstream file as starting point**

```bash
cp /tmp/pwtt-upstream/pwtt/__init__.py \
   /Volumes/A2/Dev/Projects/pwtt-qgis-plugin/core/gee_pwtt.py
```

- [ ] **Step 1.2 — Patch: replace header (imports + module constants)**

Replace the upstream header block:

```python
"""
Pixel-Wise T-Test (PWTT) - Battle damage detection using Sentinel-1 SAR imagery.
...
"""

import math
import datetime

import ee
import geemap


__version__ = "0.1.0"
__all__ = ['detect_damage', 'lee_filter', 'ttest', 'ztest', 'hotelling_t2', 'terrain_flattening', '__version__']
```

With:

```python
# -*- coding: utf-8 -*-
"""Bundled GEE PWTT logic — synced from oballinger/PWTT pwtt/__init__.py.

Adds plugin-specific adaptations:
  - open_geemap_preview() for desktop (non-Jupyter) map preview
  - viz_return_map flag in detect_damage()
  - damage_threshold param name (maps to upstream 'threshold')
  - filter_s1 backward-compat alias
"""

import math
import datetime
import os
import tempfile
import webbrowser

import ee

from .viz_constants import (
    T_STATISTIC_VIZ_MAX,
    T_STATISTIC_VIZ_MIN,
    T_STATISTIC_VIZ_OPACITY,
)

DEFAULT_DAMAGE_THRESHOLD = 3.3
```

- [ ] **Step 1.3 — Patch: add open_geemap_preview() after the normal_cdf_approx / two_tailed_pvalue / lee_filter block, before ttest()**

Insert the following function between `lee_filter` and `ttest` (i.e., after line ~103 of the upstream file, now after the `lee_filter` definition):

```python
def open_geemap_preview(
    aoi,
    image,
    output_dir: str = None,
) -> None:
    """Build a standalone Leaflet map and open it in the default browser.

    Uses Leaflet.js from CDN so the page works as a file:// URL without
    Jupyter widget dependencies. CartoDB Positron tiles are used as the basemap.
    """
    vis_params = {
        "min": T_STATISTIC_VIZ_MIN,
        "max": T_STATISTIC_VIZ_MAX,
        "palette": ["yellow", "red", "purple"],
    }

    map_id_dict = image.select("T_statistic").getMapId(vis_params)
    ee_tile_url = map_id_dict["tile_fetcher"].url_format

    bbox_coords = aoi.bounds(maxError=1).coordinates().getInfo()[0]
    min_lon = min(c[0] for c in bbox_coords)
    max_lon = max(c[0] for c in bbox_coords)
    min_lat = min(c[1] for c in bbox_coords)
    max_lat = max(c[1] for c in bbox_coords)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>PWTT Earth Engine Preview</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    html, body {{ margin: 0; padding: 0; height: 100%; }}
    #map {{ height: 100%; }}
  </style>
</head>
<body>
<div id="map"></div>
<script>
  var map = L.map('map', {{ maxZoom: 20 }});
  L.tileLayer(
    'https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
      subdomains: 'abcd',
      maxZoom: 20
    }}
  ).addTo(map);
  L.tileLayer(
    '{ee_tile_url}',
    {{
      attribution: 'Google Earth Engine',
      maxZoom: 20,
      maxNativeZoom: 14,
      opacity: {T_STATISTIC_VIZ_OPACITY}
    }}
  ).addTo(map);
  map.fitBounds([[{min_lat}, {min_lon}], [{max_lat}, {max_lon}]]);
</script>
</body>
</html>"""

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "pwtt_gee_preview.html")
    else:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="pwtt_gee_preview_")
        os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    webbrowser.open(f"file://{path}")
```

- [ ] **Step 1.4 — Patch: update detect_damage() signature**

Find the upstream signature:
```python
def detect_damage(aoi, inference_start, war_start, pre_interval=12, post_interval=2, footprints=None, viz=False, export=False, export_dir='PWTT_Export', export_name=None, export_scale=10, grid_scale=500, export_grid=False, clip=True, method='stouffer', threshold=3.3, ttest_type='welch', smoothing='default', mask_before_smooth=True, lee_mode='per_image'):
```

Replace with:
```python
def detect_damage(aoi, inference_start, war_start, pre_interval=12, post_interval=2, footprints=None, viz=False, viz_return_map=False, export=False, export_dir='PWTT_Export', export_name=None, export_scale=10, grid_scale=500, export_grid=False, clip=True, method='stouffer', damage_threshold=DEFAULT_DAMAGE_THRESHOLD, ttest_type='welch', smoothing='default', mask_before_smooth=True, lee_mode='per_image'):
```

- [ ] **Step 1.5 — Patch: rename threshold inside detect_damage() body**

In the body of `detect_damage`, find:
```python
    damage = T_statistic.gt(threshold).rename('damage')
```

Replace with:
```python
    damage = T_statistic.gt(damage_threshold).rename('damage')
```

- [ ] **Step 1.6 — Patch: replace the viz block and add footer**

Find the upstream viz block at the end of `detect_damage`:
```python
    if viz:
        Map = geemap.Map()
        Map.add_basemap('SATELLITE')
        Map.addLayer(image.select('T_statistic'), {'min': 3, 'max': 5, 'opacity': 0.5, 'palette': ["yellow", "red", "purple"]}, "T-test")
        Map.centerObject(aoi)
        return Map
```

Replace with:
```python
    if viz:
        if viz_return_map:
            import geemap
            Map = geemap.Map()
            Map.add_basemap('SATELLITE')
            Map.addLayer(
                image.select('T_statistic'),
                {
                    'min': T_STATISTIC_VIZ_MIN,
                    'max': T_STATISTIC_VIZ_MAX,
                    'opacity': T_STATISTIC_VIZ_OPACITY,
                    'palette': ['yellow', 'red', 'purple'],
                },
                'T-test',
            )
            Map.centerObject(aoi)
            return Map
        open_geemap_preview(aoi, image)
```

Then at the very end of the file (after `terrain_flattening`), append:
```python

# Backward-compatible alias
filter_s1 = detect_damage
```

- [ ] **Step 1.7 — Verify the file looks correct**

```bash
python3 -c "
import ast, sys
with open('core/gee_pwtt.py') as f:
    src = f.read()
tree = ast.parse(src)
fns = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
print('Functions:', fns)
" 
```

Expected output includes: `normal_cdf_approx`, `two_tailed_pvalue`, `lee_filter`, `open_geemap_preview`, `ttest`, `ztest`, `hotelling_t2`, `detect_damage`, `terrain_flattening` and the inner functions inside them.

- [ ] **Step 1.8 — Verify detect_damage signature**

```bash
python3 -c "
import ast
with open('core/gee_pwtt.py') as f:
    src = f.read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == 'detect_damage':
        args = [a.arg for a in node.args.args]
        defaults = [ast.literal_eval(d) for d in node.args.defaults if isinstance(d, (ast.Constant, ast.Num, ast.Str))]
        print('args:', args)
"
```

Expected: `args` list contains `damage_threshold` (not `threshold`) and `viz_return_map`.

- [ ] **Step 1.9 — Commit**

```bash
cd /Volumes/A2/Dev/Projects/pwtt-qgis-plugin
git add core/gee_pwtt.py
git commit -m "feat: sync gee_pwtt.py to upstream PWTT (Welch t-test, stouffer/ztest/hotelling/mahalanobis methods)"
```

---

## Task 2: Update core/gee_backend.py

**Files:**
- Modify: `core/gee_backend.py` (lines ~199–236)

- [ ] **Step 2.1 — Add 5 params to run() signature**

Find the current `run()` signature in `core/gee_backend.py`:
```python
    def run(
        self,
        aoi_wkt: str,
        war_start: str,
        inference_start: str,
        pre_interval: int,
        post_interval: int,
        output_path: str,
        progress_callback=None,
        include_footprints: bool = False,
        footprints_path: Optional[str] = None,
        remote_job_id: Optional[str] = None,
        damage_threshold: float = 3.3,
        gee_viz: bool = False,
    ) -> str:
```

Replace with:
```python
    def run(
        self,
        aoi_wkt: str,
        war_start: str,
        inference_start: str,
        pre_interval: int,
        post_interval: int,
        output_path: str,
        progress_callback=None,
        include_footprints: bool = False,
        footprints_path: Optional[str] = None,
        remote_job_id: Optional[str] = None,
        damage_threshold: float = 3.3,
        gee_viz: bool = False,
        method: str = 'stouffer',
        ttest_type: str = 'welch',
        smoothing: str = 'default',
        mask_before_smooth: bool = True,
        lee_mode: str = 'per_image',
    ) -> str:
```

- [ ] **Step 2.2 — Pass new params to detect_damage()**

Find the `gee_pwtt.detect_damage(...)` call:
```python
        image = gee_pwtt.detect_damage(
            aoi,
            inference_start=inference_start,
            war_start=war_start,
            pre_interval=pre_interval,
            post_interval=post_interval,
            viz=False,
            export=False,
            damage_threshold=damage_threshold,
        )
```

Replace with:
```python
        image = gee_pwtt.detect_damage(
            aoi,
            inference_start=inference_start,
            war_start=war_start,
            pre_interval=pre_interval,
            post_interval=post_interval,
            viz=False,
            export=False,
            damage_threshold=damage_threshold,
            method=method,
            ttest_type=ttest_type,
            smoothing=smoothing,
            mask_before_smooth=mask_before_smooth,
            lee_mode=lee_mode,
        )
```

- [ ] **Step 2.3 — Commit**

```bash
git add core/gee_backend.py
git commit -m "feat: pass method/ttest_type/smoothing/mask_before_smooth/lee_mode through GEEBackend.run()"
```

---

## Task 3: Update core/pwtt_task.py

**Files:**
- Modify: `core/pwtt_task.py`

- [ ] **Step 3.1 — Add 5 params to PWTTRunTask.__init__()**

Find the end of the `__init__` parameter list:
```python
        damage_threshold=3.3,
        gee_viz=False,
        data_source=None,
```

Replace with:
```python
        damage_threshold=3.3,
        gee_viz=False,
        data_source=None,
        gee_method='stouffer',
        gee_ttest_type='welch',
        gee_smoothing='default',
        gee_mask_before_smooth=True,
        gee_lee_mode='per_image',
```

- [ ] **Step 3.2 — Store new params as instance attributes**

Find where existing params are stored (after `self.gee_viz = bool(gee_viz)`):
```python
        self.damage_threshold = float(damage_threshold)
        self.gee_viz = bool(gee_viz)
        # Local GRD catalog (cdse/asf/pc); used in layer tree names.
```

Replace with:
```python
        self.damage_threshold = float(damage_threshold)
        self.gee_viz = bool(gee_viz)
        self.gee_method = str(gee_method)
        self.gee_ttest_type = str(gee_ttest_type)
        self.gee_smoothing = str(gee_smoothing)
        self.gee_mask_before_smooth = bool(gee_mask_before_smooth)
        self.gee_lee_mode = str(gee_lee_mode)
        # Local GRD catalog (cdse/asf/pc); used in layer tree names.
```

- [ ] **Step 3.3 — Add new params to run_kwargs**

Find:
```python
            run_kwargs = dict(
                aoi_wkt=self.aoi_wkt,
                war_start=self.war_start,
                inference_start=self.inference_start,
                pre_interval=self.pre_interval,
                post_interval=self.post_interval,
                output_path=out_tif,
                progress_callback=progress,
                include_footprints=False,
                footprints_path=None,
                damage_threshold=self.damage_threshold,
                gee_viz=self.gee_viz,
            )
```

Replace with:
```python
            run_kwargs = dict(
                aoi_wkt=self.aoi_wkt,
                war_start=self.war_start,
                inference_start=self.inference_start,
                pre_interval=self.pre_interval,
                post_interval=self.post_interval,
                output_path=out_tif,
                progress_callback=progress,
                include_footprints=False,
                footprints_path=None,
                damage_threshold=self.damage_threshold,
                gee_viz=self.gee_viz,
                method=self.gee_method,
                ttest_type=self.gee_ttest_type,
                smoothing=self.gee_smoothing,
                mask_before_smooth=self.gee_mask_before_smooth,
                lee_mode=self.gee_lee_mode,
            )
```

- [ ] **Step 3.4 — Add new params to job metadata dict**

Find the metadata dict built in `finished()` (contains `"damage_threshold"` and `"gee_viz"`):
```python
                "damage_threshold": self.damage_threshold,
                "gee_viz": self.gee_viz,
```

Replace with:
```python
                "damage_threshold": self.damage_threshold,
                "gee_viz": self.gee_viz,
                "gee_method": self.gee_method,
                "gee_ttest_type": self.gee_ttest_type,
                "gee_smoothing": self.gee_smoothing,
                "gee_mask_before_smooth": self.gee_mask_before_smooth,
                "gee_lee_mode": self.gee_lee_mode,
```

- [ ] **Step 3.5 — Commit**

```bash
git add core/pwtt_task.py
git commit -m "feat: thread GEE method params through PWTTRunTask"
```

---

## Task 4: Update core/job_store.py

**Files:**
- Modify: `core/job_store.py` (lines ~53–100)

- [ ] **Step 4.1 — Add 5 new params to create_job() signature**

Find:
```python
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
```

Replace with:
```python
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
    gee_method: str = "stouffer",
    gee_ttest_type: str = "welch",
    gee_smoothing: str = "default",
    gee_mask_before_smooth: bool = True,
    gee_lee_mode: str = "per_image",
) -> dict:
```

- [ ] **Step 4.2 — Add new fields to the returned dict**

Find the return dict, after the `"gee_viz"` entry:
```python
        "gee_viz": bool(gee_viz),
        # Added in v0.1.44; older jobs.json entries lack this field.
```

Replace with:
```python
        "gee_viz": bool(gee_viz),
        "gee_method": str(gee_method),
        "gee_ttest_type": str(gee_ttest_type),
        "gee_smoothing": str(gee_smoothing),
        "gee_mask_before_smooth": bool(gee_mask_before_smooth),
        "gee_lee_mode": str(gee_lee_mode),
        # Added in v0.1.44; older jobs.json entries lack this field.
```

- [ ] **Step 4.3 — Commit**

```bash
git add core/job_store.py
git commit -m "feat: add GEE method params to job_store.create_job()"
```

---

## Task 5: Update ui/jobs_dock.py

**Files:**
- Modify: `ui/jobs_dock.py` (lines ~983–1000, the `PWTTRunTask(...)` constructor call)

- [ ] **Step 5.1 — Pass new params from job dict to PWTTRunTask**

Find:
```python
        task = PWTTRunTask(
            backend=backend,
            aoi_wkt=job["aoi_wkt"],
            war_start=job["war_start"],
            inference_start=job["inference_start"],
            pre_interval=job["pre_interval"],
            post_interval=job["post_interval"],
            output_dir=job["output_dir"],
            include_footprints=bool(fp_sources),
            footprints_sources=fp_sources,
            job_id=job["id"],
            remote_job_id=job.get("remote_job_id"),
            damage_threshold=job.get("damage_threshold", 3.3),
            gee_viz=job.get("gee_viz", False),
            data_source=job.get("data_source")
            if job.get("backend_id") == "local"
            else None,
        )
```

Replace with:
```python
        task = PWTTRunTask(
            backend=backend,
            aoi_wkt=job["aoi_wkt"],
            war_start=job["war_start"],
            inference_start=job["inference_start"],
            pre_interval=job["pre_interval"],
            post_interval=job["post_interval"],
            output_dir=job["output_dir"],
            include_footprints=bool(fp_sources),
            footprints_sources=fp_sources,
            job_id=job["id"],
            remote_job_id=job.get("remote_job_id"),
            damage_threshold=job.get("damage_threshold", 3.3),
            gee_viz=job.get("gee_viz", False),
            data_source=job.get("data_source")
            if job.get("backend_id") == "local"
            else None,
            gee_method=job.get("gee_method", "stouffer"),
            gee_ttest_type=job.get("gee_ttest_type", "welch"),
            gee_smoothing=job.get("gee_smoothing", "default"),
            gee_mask_before_smooth=job.get("gee_mask_before_smooth", True),
            gee_lee_mode=job.get("gee_lee_mode", "per_image"),
        )
```

- [ ] **Step 5.2 — Commit**

```bash
git add ui/jobs_dock.py
git commit -m "feat: pass GEE method params from job dict to PWTTRunTask in jobs_dock"
```

---

## Task 6: Update ui/main_dialog.py

**Files:**
- Modify: `ui/main_dialog.py`

This task has 6 sub-steps: build method group, build advanced group, wire `_on_backend_changed`, update `_load_settings`/`_save_settings`, update `load_job_parameters`, update `_run_job`.

- [ ] **Step 6.1 — Add method group to _build_ui()**

Find this block in `_build_ui()` (params_group section, just before `self.damage_mask_group`):
```python
        params_layout.addRow(self.damage_mask_group)

        self.gee_preview_group = QGroupBox("Earth Engine preview")
```

Insert the following BEFORE `params_layout.addRow(self.damage_mask_group)`:

```python
        # ── GEE: Detection method ──────────────────────────────────────────
        self.gee_method_group = QGroupBox("Detection method (GEE only)")
        gm_layout = QVBoxLayout(self.gee_method_group)
        self.gee_method_combo = QComboBox()
        _method_items = [
            ("stouffer",     "Stouffer weighted Z  (default — recommended)"),
            ("max",          "Max t-value across orbits"),
            ("ztest",        "Z-test: latest image vs baseline"),
            ("hotelling",    "Hotelling T²  (joint VV+VH)"),
            ("mahalanobis",  "Mahalanobis effect size  (n-invariant)"),
        ]
        for value, label in _method_items:
            self.gee_method_combo.addItem(label, value)
        gm_layout.addWidget(self.gee_method_combo)
        self._gee_method_hint = self._hint("")
        gm_layout.addWidget(self._gee_method_hint)
        self.gee_method_combo.currentIndexChanged.connect(self._on_gee_method_changed)
        params_layout.addRow(self.gee_method_group)

        # ── GEE: Advanced options (collapsed) ─────────────────────────────
        self.gee_advanced_group = QGroupBox("Advanced GEE options")
        ga_outer = QVBoxLayout(self.gee_advanced_group)

        self._gee_advanced_toggle_btn = QPushButton("▶  Advanced options")
        self._gee_advanced_toggle_btn.setCheckable(True)
        self._gee_advanced_toggle_btn.setChecked(False)
        self._gee_advanced_toggle_btn.setFlat(True)
        ga_outer.addWidget(self._gee_advanced_toggle_btn)

        self._gee_advanced_widget = QWidget()
        ga_adv = QFormLayout(self._gee_advanced_widget)
        ga_adv.setVerticalSpacing(4)

        self.gee_ttest_type_combo = QComboBox()
        self.gee_ttest_type_combo.addItem("welch  (default — unequal variance)", "welch")
        self.gee_ttest_type_combo.addItem("pooled  (assumes equal variance)", "pooled")
        ga_adv.addRow("T-test type:", self.gee_ttest_type_combo)

        self.gee_smoothing_combo = QComboBox()
        self.gee_smoothing_combo.addItem("default  (focal median + 50/100/150 m kernels)", "default")
        self.gee_smoothing_combo.addItem("focal_only  (focal median only, no convolution)", "focal_only")
        ga_adv.addRow("Smoothing:", self.gee_smoothing_combo)

        self.gee_mask_before_smooth_cb = QCheckBox("Mask urban pixels before focal median")
        self.gee_mask_before_smooth_cb.setChecked(True)
        ga_adv.addRow(self.gee_mask_before_smooth_cb)

        self.gee_lee_mode_combo = QComboBox()
        self.gee_lee_mode_combo.addItem("per_image  (default — filter each scene)", "per_image")
        self.gee_lee_mode_combo.addItem("composite  (filter composites only, ~37% less cost)", "composite")
        ga_adv.addRow("Lee filter mode:", self.gee_lee_mode_combo)

        ga_adv.addRow(self._hint(
            "T-test type: Welch does not assume equal variance (more robust). "
            "Smoothing: 'default' applies multi-scale convolutions after focal median. "
            "Lee mode: 'composite' saves EE compute units on large AOIs."
        ))

        self._gee_advanced_widget.setVisible(False)
        ga_outer.addWidget(self._gee_advanced_widget)
        self._gee_advanced_toggle_btn.toggled.connect(self._on_gee_advanced_toggled)
        params_layout.addRow(self.gee_advanced_group)

```

- [ ] **Step 6.2 — Add _on_gee_method_changed and _on_gee_advanced_toggled helpers**

Find `def _build_ui(self):` and look for the first method that is defined after it (e.g. `def showEvent`). Add these two new methods anywhere in the class before `_on_backend_changed`:

```python
    _GEE_METHOD_HINTS = {
        "stouffer": (
            "Stouffer's weighted Z-score: combines orbits by √df. "
            "Statistically principled default."
        ),
        "max": (
            "Takes the maximum t-value across orbits and Bonferroni-corrects. "
            "Original PWTT behavior."
        ),
        "ztest": (
            "Compares the single most-recent post-war image to the pre-war baseline. "
            "Useful for near-real-time monitoring."
        ),
        "hotelling": (
            "Hotelling T²: joint multivariate test on VV and VH simultaneously. "
            "More powerful when both polarizations change together."
        ),
        "mahalanobis": (
            "Mahalanobis effect size: n-invariant, useful for comparing areas with "
            "different image counts."
        ),
    }

    def _on_gee_method_changed(self, _index):
        method = self.gee_method_combo.currentData()
        self._gee_method_hint.setText(self._GEE_METHOD_HINTS.get(method, ""))

    def _on_gee_advanced_toggled(self, checked: bool):
        self._gee_advanced_widget.setVisible(checked)
        self._gee_advanced_toggle_btn.setText(
            "▼  Advanced options" if checked else "▶  Advanced options"
        )
```

- [ ] **Step 6.3 — Show/hide new groups in _on_backend_changed()**

Find:
```python
        self.damage_mask_group.setVisible(True)
        self.gee_preview_group.setVisible(backend_id == "gee")
```

Replace with:
```python
        self.damage_mask_group.setVisible(True)
        self.gee_preview_group.setVisible(backend_id == "gee")
        self.gee_method_group.setVisible(backend_id == "gee")
        self.gee_advanced_group.setVisible(backend_id == "gee")
```

- [ ] **Step 6.4 — Add new values to _load_settings()**

Find the end of `_load_settings()`, just before `s.endGroup()`:
```python
        self.gee_map_preview_cb.setChecked(
            s.value("gee_map_preview", False, type=bool)
        )
        s.endGroup()
```

Replace with:
```python
        self.gee_map_preview_cb.setChecked(
            s.value("gee_map_preview", False, type=bool)
        )
        _method_val = s.value("gee_method", "stouffer")
        _method_idx = next(
            (i for i in range(self.gee_method_combo.count())
             if self.gee_method_combo.itemData(i) == _method_val),
            0,
        )
        self.gee_method_combo.setCurrentIndex(_method_idx)
        _ttest_val = s.value("gee_ttest_type", "welch")
        self.gee_ttest_type_combo.setCurrentIndex(
            0 if _ttest_val == "welch" else 1
        )
        _smoothing_val = s.value("gee_smoothing", "default")
        self.gee_smoothing_combo.setCurrentIndex(
            0 if _smoothing_val == "default" else 1
        )
        self.gee_mask_before_smooth_cb.setChecked(
            s.value("gee_mask_before_smooth", True, type=bool)
        )
        _lee_val = s.value("gee_lee_mode", "per_image")
        self.gee_lee_mode_combo.setCurrentIndex(
            0 if _lee_val == "per_image" else 1
        )
        s.endGroup()
```

- [ ] **Step 6.5 — Add new values to _save_settings()**

Find:
```python
        s.setValue("gee_map_preview", self.gee_map_preview_cb.isChecked())
        s.endGroup()
```

Replace with:
```python
        s.setValue("gee_map_preview", self.gee_map_preview_cb.isChecked())
        s.setValue("gee_method", self.gee_method_combo.currentData())
        s.setValue("gee_ttest_type", self.gee_ttest_type_combo.currentData())
        s.setValue("gee_smoothing", self.gee_smoothing_combo.currentData())
        s.setValue("gee_mask_before_smooth", self.gee_mask_before_smooth_cb.isChecked())
        s.setValue("gee_lee_mode", self.gee_lee_mode_combo.currentData())
        s.endGroup()
```

- [ ] **Step 6.6 — Update load_job_parameters() for job replay**

Find (in `load_job_parameters`):
```python
        self.damage_threshold_spin.setValue(float(job.get("damage_threshold", 3.3)))
        self.gee_map_preview_cb.setChecked(job.get("gee_viz", False))
```

Replace with:
```python
        self.damage_threshold_spin.setValue(float(job.get("damage_threshold", 3.3)))
        self.gee_map_preview_cb.setChecked(job.get("gee_viz", False))

        _method_val = job.get("gee_method", "stouffer")
        _method_idx = next(
            (i for i in range(self.gee_method_combo.count())
             if self.gee_method_combo.itemData(i) == _method_val),
            0,
        )
        self.gee_method_combo.setCurrentIndex(_method_idx)
        _ttest_val = job.get("gee_ttest_type", "welch")
        self.gee_ttest_type_combo.setCurrentIndex(0 if _ttest_val == "welch" else 1)
        _smoothing_val = job.get("gee_smoothing", "default")
        self.gee_smoothing_combo.setCurrentIndex(0 if _smoothing_val == "default" else 1)
        self.gee_mask_before_smooth_cb.setChecked(job.get("gee_mask_before_smooth", True))
        _lee_val = job.get("gee_lee_mode", "per_image")
        self.gee_lee_mode_combo.setCurrentIndex(0 if _lee_val == "per_image" else 1)
```

- [ ] **Step 6.7 — Update _run_job() to collect and pass new params**

Find in `_run_job()` (the `job_store.create_job(...)` call):
```python
            damage_threshold=self.damage_threshold_spin.value(),
            gee_viz=self.gee_map_preview_cb.isChecked() if backend_id == "gee" else False,
            data_source=self._local_data_source_id() if backend_id == "local" else "cdse",
```

Replace with:
```python
            damage_threshold=self.damage_threshold_spin.value(),
            gee_viz=self.gee_map_preview_cb.isChecked() if backend_id == "gee" else False,
            data_source=self._local_data_source_id() if backend_id == "local" else "cdse",
            gee_method=self.gee_method_combo.currentData() if backend_id == "gee" else "stouffer",
            gee_ttest_type=self.gee_ttest_type_combo.currentData() if backend_id == "gee" else "welch",
            gee_smoothing=self.gee_smoothing_combo.currentData() if backend_id == "gee" else "default",
            gee_mask_before_smooth=self.gee_mask_before_smooth_cb.isChecked() if backend_id == "gee" else True,
            gee_lee_mode=self.gee_lee_mode_combo.currentData() if backend_id == "gee" else "per_image",
```

- [ ] **Step 6.8 — Verify syntax**

```bash
cd /Volumes/A2/Dev/Projects/pwtt-qgis-plugin
python3 -c "
import ast
for f in ['ui/main_dialog.py', 'core/gee_pwtt.py', 'core/gee_backend.py', 'core/pwtt_task.py', 'core/job_store.py', 'ui/jobs_dock.py']:
    with open(f) as fh:
        src = fh.read()
    ast.parse(src)
    print(f'OK: {f}')
"
```

Expected: 6 `OK:` lines, no errors.

- [ ] **Step 6.9 — Commit**

```bash
git add ui/main_dialog.py
git commit -m "feat: add GEE method dropdown and advanced options section to Controls dock"
```

---

## Final Verification Checklist

- [ ] All 6 Python files parse without error (`ast.parse` check above)
- [ ] `core/gee_pwtt.py` defines: `normal_cdf_approx`, `two_tailed_pvalue`, `lee_filter`, `open_geemap_preview`, `ttest`, `ztest`, `hotelling_t2`, `detect_damage`, `terrain_flattening`, `filter_s1`
- [ ] `detect_damage` signature has `damage_threshold` (not `threshold`) and `viz_return_map`
- [ ] `detect_damage` body line `damage = T_statistic.gt(damage_threshold)` (grep to confirm)
- [ ] `gee_backend.run()` signature includes all 5 new params
- [ ] `PWTTRunTask.__init__()` stores all 5 new params as `self.gee_*` attrs
- [ ] `job_store.create_job()` returns dict with keys `gee_method`, `gee_ttest_type`, `gee_smoothing`, `gee_mask_before_smooth`, `gee_lee_mode`
- [ ] `jobs_dock.py` passes all 5 new params with `.get(..., default)` fallback from job dict
- [ ] Opening QGIS loads the Controls dock without Python errors
- [ ] Switching to GEE backend shows the method dropdown and advanced group
- [ ] Switching to openEO or local hides both new groups
- [ ] Method dropdown shows 5 items; selecting each updates the hint text
- [ ] Clicking "▶ Advanced options" expands the widget; clicking again collapses it
- [ ] Settings persist across dock close/reopen (QgsSettings round-trip)
- [ ] Loading an old job (missing `gee_method` key) loads without error (defaults apply)
