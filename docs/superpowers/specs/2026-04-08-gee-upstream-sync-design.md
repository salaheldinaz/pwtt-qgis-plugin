# GEE Upstream Sync — Design Spec

**Date:** 2026-04-08  
**Scope:** Sync `core/gee_pwtt.py` to upstream `oballinger/PWTT` `pwtt/__init__.py` (commit 408f28f, Apr 7 2026), wire new parameters through the backend and UI.

---

## Goal

Full parity with upstream PWTT algorithm in the plugin's GEE backend. Adds five detection methods, Welch's t-test, configurable smoothing, and robustness improvements. New GEE-only options exposed in the Controls dock via a prominent method dropdown and a collapsed advanced section.

---

## Files Changed

| File | Change |
|------|--------|
| `core/gee_pwtt.py` | Full replacement (706-line upstream `__init__.py`, adapted) |
| `core/gee_backend.py` | Add 5 new params to `run()`, pass to `detect_damage()` |
| `core/pwtt_task.py` | Add 5 new params to `__init__` and `run_kwargs` |
| `ui/main_dialog.py` | Add method dropdown + collapsed advanced section (GEE-only) |

---

## 1. core/gee_pwtt.py — Full Replacement

**Strategy:** Replace wholesale with upstream `pwtt/__init__.py`. Adapt for plugin conventions.

### Kept from plugin (not in upstream)
- `import` block: add `os`, `tempfile`, `webbrowser`, and `viz_constants` imports
- `DEFAULT_DAMAGE_THRESHOLD = 3.3` constant
- `open_geemap_preview(aoi, image, output_dir=None)` function — unchanged
- `viz_return_map` parameter in `detect_damage()`: when `viz=True` and `viz_return_map=True`, use `geemap.Map()` (upstream behavior); when `viz=True` and `viz_return_map=False`, call `open_geemap_preview()` (desktop behavior)
- `filter_s1 = detect_damage` backward-compat alias

### Parameter name: `threshold` vs `damage_threshold`
Upstream uses `threshold=3.3`. Plugin currently uses `damage_threshold`. **Keep `damage_threshold`** in `detect_damage()` signature so `gee_backend.py` call site doesn't break. Map it to upstream's `threshold` internally.

### New functions (from upstream, verbatim)
- `ztest(s1, inference_start, war_start, pre_interval)` — z-score of latest post image vs pre-war baseline
- `hotelling_t2(s1, inference_start, war_start, pre_interval, post_interval, ttest_type='welch')` — multivariate T² on VV+VH
- `terrain_flattening(collection, TERRAIN_FLATTENING_MODEL, DEM, ...)` — radiometric slope correction (not wired into detect_damage, available for future use)

### Updated functions
**`ttest()`** gains `ttest_type='welch'` parameter:
- `'welch'` (default): Welch's t-test, unequal variance, Welch-Satterthwaite df
- `'pooled'`: original pooled t-test
- Returns `df_VV`/`df_VH` bands (needed by Stouffer method)

**`detect_damage()`** gains:
- `method='stouffer'` — `'stouffer'` | `'max'` | `'ztest'` | `'hotelling'` | `'mahalanobis'`
- `ttest_type='welch'` — `'welch'` | `'pooled'`
- `smoothing='default'` — `'default'` | `'focal_only'` | dict
- `mask_before_smooth=True` — bool
- `lee_mode='per_image'` — `'per_image'` | `'composite'`

Also adds from upstream:
- `make_orbit_s1()` inner function (factored out, reused by all methods)
- Empty orbit fallback (`empty_orbit` image) to handle orbits with no coverage
- `raw_data_mask` applied after smoothing to prevent edge artifacts
- No-coverage fallback (`orbits.size().gt(0)` guard)
- Inference date validation warning

---

## 2. core/gee_backend.py — New Params

`run()` signature gains (all keyword-only with upstream defaults):

```python
method: str = 'stouffer'
ttest_type: str = 'welch'
smoothing: str = 'default'
mask_before_smooth: bool = True
lee_mode: str = 'per_image'
```

Passed straight through to `gee_pwtt.detect_damage()`. No logic in the backend.

Download bands stay as `["T_statistic", "damage", "p_value"]` for all methods (consistent output regardless of method).

---

## 3. core/pwtt_task.py — New Params

`PWTTRunTask.__init__()` gains the same 5 params with identical defaults. They are stored as instance attrs and added to `run_kwargs` before `backend.run()` is called. They are also saved to the job metadata dict for replay/logging.

---

## 4. ui/main_dialog.py — UI Changes

New UI elements are **GEE-only**: shown when backend == `gee`, hidden otherwise (via `_on_backend_changed`).

### 4a. Detection method group (always expanded)
A new `QGroupBox("Detection method")` placed after the Parameters group, before the Damage mask group. Contains:
- `QComboBox` (self.gee_method_combo) with items:
  - `stouffer` — Stouffer weighted Z (default, recommended)
  - `max` — Max t-value across orbits
  - `ztest` — Single latest image vs baseline
  - `hotelling` — Multivariate T² (VV+VH joint)
  - `mahalanobis` — Mahalanobis effect size
- One-line hint that updates dynamically when selection changes (per-method description)

### 4b. Advanced GEE options (collapsed by default)
A `QPushButton("▶ Advanced GEE options")` acting as a toggle that shows/hides a `QWidget` containing a `QFormLayout` with:

| Widget | Label | Options |
|--------|-------|---------|
| `self.gee_ttest_type_combo` (QComboBox) | T-test type | `welch (default)`, `pooled` |
| `self.gee_smoothing_combo` (QComboBox) | Smoothing | `default`, `focal_only` |
| `self.gee_mask_before_smooth_cb` (QCheckBox) | Mask before smooth | checked by default |
| `self.gee_lee_mode_combo` (QComboBox) | Lee filter mode | `per_image (default)`, `composite` |

Hint text below the advanced widget briefly explains each option.

### Settings persistence
All 5 new values read/written in `_load_settings()` / `_save_settings()` under `QgsSettings` group `PWTT`:

| Key | Default |
|-----|---------|
| `gee_method` | `stouffer` |
| `gee_ttest_type` | `welch` |
| `gee_smoothing` | `default` |
| `gee_mask_before_smooth` | `True` |
| `gee_lee_mode` | `per_image` |

### Job replay
`load_job_parameters()` restores all 5 values from the job dict (with fallback to defaults for old jobs).

### _run_job() collection
Adds to the params dict passed to `PWTTRunTask`:
```python
gee_method=self.gee_method_combo.currentData(),
gee_ttest_type=self.gee_ttest_type_combo.currentData(),
gee_smoothing=self.gee_smoothing_combo.currentData(),
gee_mask_before_smooth=self.gee_mask_before_smooth_cb.isChecked(),
gee_lee_mode=self.gee_lee_mode_combo.currentData(),
```

---

## Testing

- Run `detect_damage()` with `method='stouffer'` (new default) on a known AOI and verify it returns an image with the expected bands
- Run with `method='max'` and confirm results match pre-update behavior
- Toggle advanced options in the UI and confirm values are passed through to `detect_damage()`
- Confirm settings are saved and restored on QGIS restart
- Confirm old jobs (missing new keys) load without error (default fallback)
