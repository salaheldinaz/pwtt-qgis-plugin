# How the PWTT QGIS plugin works

This document describes what the plugin does end-to-end: user inputs, job handling, and how each **backend** implements the analysis. For install and UI steps, see [README.md](README.md).

## Overview

The plugin estimates **building-related damage** from **Sentinel-1 GRD** backscatter (VV and VH) by comparing a **pre-war (baseline)** period to a **post-war** period over your **area of interest (AOI)**. Output is a GeoTIFF (`pwtt_result.tif` or `pwtt_<job_id>.tif`) with **three bands** in normal use: continuous **`T_statistic`**, binary **`damage`**, and **`p_value`**. The **damage** mask uses your **T-statistic threshold** in the controls (default **3.3**). **GEE thresholds an intermediate smoothed surface (`t_smooth`); openEO thresholds the final averaged `T_statistic`** — see [GEE vs openEO](#gee-vs-openeo-why-results-differ-for-the-same-aoi).

**GEE**, **openEO**, and **Local** all use a **pooled t-test–style** comparison, but **radiometry, orbit logic, masking, smoothing, and where the threshold is applied differ**. Expect **similar patterns**, not **pixel-identical** rasters, for the same AOI and dates.

**Reference:** [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

### Map colors (QGIS)

Default **multiband RGB** (R=band 1, G=band 2, B=band 3) does **not** give a literal “yellow = damaged” legend — it blends three different products. For an intuitive **heat map** of change strength, use **singleband pseudocolor on band 1** (`T_statistic`) with a cool-to-hot ramp; then colors align with the rough guide in [README.md — Reading colors on the map](README.md#reading-colors-on-the-map) (yellow ≈ strongest change signal, blue/purple ≈ little change). Binary **damage** is band 2; use singleband/classified symbology there for a strict mask.

**After the result is on the map:** Edits under **Layer Properties → Symbology** (min/max stretch, opacity, ramp) affect **display only** — `T_statistic` and `damage` pixel values in the GeoTIFF do not change. A tighter max on band 1 can make moderate scores look more “damaged” on screen without changing band 2. To change **which** pixels are 1 in the damage mask, **re-run** with a different threshold or derive a new mask from band 1 (e.g. Raster Calculator).

---

## What you configure

| Input | Meaning |
|--------|--------|
| **AOI** | Rectangle drawn on the map (stored as WKT). |
| **War start date** | Calendar date (`yyyy-MM-dd`). End of the pre baseline window and anchor for "pre" extent. |
| **Inference start date** | Calendar date. Start of the post window. |
| **Pre-war interval** | **Months** (integer). Baseline begins roughly `pre_interval` months **before** war start. |
| **Post-war interval** | **Months** (integer). Post window extends roughly that many months **after** inference start. |
| **Output directory** | Folder where `pwtt_<job_id>.tif` (or `pwtt_result.tif` without a job id) is written (and optional `pwtt_<job_id>_footprints.gpkg`). |

**Day vs month:** War start and inference start are **full dates**. Only the **length** of the baseline and post collections is set in **whole months** in the UI—there is no separate "N days" control.

**Backend** choice determines **where** computation runs and **exactly** how the change score is built.

---

## Plugin flow (orchestration)

1. **Controls dock** — You set parameters and click **Run**.
2. **Job record** — A job is created and appended to the persistent job list (`jobs.json` under the QGIS user profile, folder `PWTT`—**not** inside the `.qgz` project file).
3. **`PWTTRunTask` (QgsTask)** — Runs in the background: creates the output directory if needed, calls `backend.authenticate()` then `backend.run(...)` with `output_path = <output_dir>/pwtt_<job_id>.tif` when a job id exists, otherwise `pwtt_result.tif`.
4. **On success** — Job status is set to completed; `output_tif` is stored on the job; the raster (and footprints layer if any) is **added to the current QGIS project**.
5. **Jobs dock** — Lists jobs, **Resume** / **Rerun** / **Delete**, progress and log. **Rerun** clones parameters into a **new** job id.

**openEO batch jobs:** While running, the log shows a server **batch job id** (`j-…`). The plugin **persists** that id on the job record in `jobs.json` when the backend reports it (for **Resume**). To re-download results from the API you still need that id (or list jobs via the openEO client) within the provider's result retention policy (Copernicus Data Space: **90 days after job completion** as of 2025-05-06; see their announcements).

**Local cache:** Downloads go to **`<output_dir>/.pwtt_cache`**, not a global folder. If the backend is not Local, that cache is unused.

---

## Temporal windows (conceptual)

For all backends the intent is:

- **Pre (baseline):** imagery from roughly **war start minus `pre_interval` months** through **war start**.
- **Post:** imagery from **inference start** through roughly **inference start plus `post_interval` months**.

**Implementation detail:** The **Local** backend computes the pre-window **start** as the **first day of the month** after subtracting months; **openEO** and **GEE** use calendar month arithmetic on the full anchor dates. Edge dates can therefore differ slightly for the same numbers.

---

## Sentinel-1 GRD: what data you actually get

The plugin does **not** use “one SAR image per calendar day.” **Sentinel-1 GRD** products are **individual acquisitions** (satellite overpasses). Over your AOI, revisits follow S1’s **repeat cycle** (often on the order of days, depending on mode, area, and whether you combine ascending/descending)—so within your pre/post **date ranges** you only get **the passes that actually exist** in the catalogue, not a full daily time series.

**What the windows mean:** All backends restrict data to GRD scenes whose acquisition time falls in the **pre** interval (baseline, ending at war start) and the **post** interval (starting at inference start). That is “all SAR images in those ranges” in principle, but each backend then **aggregates or subsamples** differently:

| Backend | How acquisitions in the window are used |
|--------|----------------------------------------|
| **openEO** | Every observation in the cube over each window feeds temporal **mean**, **variance**, and **count** per band → pooled t-style composites per pixel (not a per-date stack in the output file). |
| **Local** | Catalogue search returns candidate IW GRD products; the plugin downloads and uses **at most 3 pre and 3 post** scenes (to limit disk and runtime), not every acquisition in the window. |
| **GEE** | Image collections include **all** GRD images in the filtered date range that match AOI and mode; processing is **per relative orbit**, then combined (see below). |

So: you are always working from **real GRD granules in your chosen months**, not synthetic “all days before/after,” and **Local** deliberately uses a **small subset** of those granules.

---

## Backend: openEO (Copernicus Data Space)

**Connection:** `https://openeo.dataspace.copernicus.eu`  
**Auth:** OIDC (browser) or client id + secret.

**Processing (graph):**

1. Load **SENTINEL1_GRD** (VV or VH per step) over pre/post **spatial bbox** and **temporal** windows; **SAR backscatter** σ⁰ ellipsoid (no Lee filter, no `log()` in this path).
2. Per polarisation: temporal **mean**, **variance**, and **count** → **pooled standard error** → **t = |post_mean − pre_mean| / SE** (pooled t-test style on the composites).
3. **max(t_VV, t_VH)**; **p_value** from a **normal approximation** (not the same formula as GEE’s CDF-based p-value; **no** orbit-wise Bonferroni).
4. **No** per-orbit split: **all** acquisitions in each window feed one composite per band (contrast GEE).
5. **No** Dynamic World urban mask.
6. **No** focal-median step: circular **mean** kernels (discrete disks at ~50 / 100 / 150 m on a 10 m grid) on **`max_change`**.
7. **`T_statistic` = (max_change + k50 + k100 + k150) / 4**; **`damage` = 1** where **`T_statistic` > threshold** (same surface as band 1 for thresholding).
8. Batch job → download GeoTIFF (**3 bands**: `T_statistic`, `damage`, `p_value`).

---

## Backend: Local (CDSE download + NumPy/SciPy/rasterio)

**Auth:** CDSE username / password.

**Processing:**

1. **Search** Sentinel-1 IW GRD for pre and post windows; **download** products into `<output_dir>/.pwtt_cache` (skip/wait logic for offline products).
2. Use up to **3** pre and **3** post scenes; **Lee** speckle filter; **log** σ⁰; reproject to a common grid.
3. **Per-pixel** comparison of pre vs post stacks: **Welch-style t**-type statistic for **VV** and **VH**; take **element-wise max** of the two.
4. **Post-processing:** Gaussian-style smoothing, circular-kernel means at 50 / 100 / 150 m; combined **T_statistic**; **damage** = 1 where **`T_statistic` > threshold** (UI default 3.3).
5. Write **GeoTIFF** (**3 bands**: `T_statistic`, `damage`, `p_value`). Optional **footprints** step can aggregate to OSM buildings (`pwtt_footprints.gpkg`).

---

## Backend: Google Earth Engine

**Auth:** Earth Engine credentials; optional GEE project name.

**Processing (bundled `gee_pwtt`):**

1. Filter **COPERNICUS/S1_GRD_FLOAT** (IW, VV+VH) by AOI and post window; get **distinct relative orbits** in that window.
2. **Per orbit:** Lee filter, **log**, then **t-test–style** map: pre vs post **means** and **pooled** variability (sample size from distinct orbit passes in the collections).
3. **Max** across orbits; **Dynamic World** built-up layer used as an **urban mask** (built mean > 0.1 in the pre window).
4. **Focal median** (10 m) and circular convolutions at 50 / 100 / 150 m → **`t_smooth`** then **`T_statistic` = (t_smooth + k50 + k100 + k150) / 4**.
5. **Threshold detail:** **`damage`** is **`t_smooth > threshold`** (not **`T_statistic` > threshold**). So band 2 is **not** “band 1 > threshold” on GEE; band 1 is the four-way average, band 2 uses the pre-average smoothed t-surface.
6. **getDownloadURL** streams a GeoTIFF (**3 bands**: `T_statistic`, `damage`, `p_value`) to disk.

Very large AOIs may hit GEE download limits; export to Drive may be needed outside this plugin.

---

## GEE vs openEO: why results differ for the same AOI

Using the **same rectangle and dates** in the UI does **not** guarantee matching rasters. Main reasons in **this** plugin:

| Topic | GEE (`gee_pwtt`) | openEO (`openeo_backend`) |
|--------|------------------|---------------------------|
| **Product / radiometry** | `COPERNICUS/S1_GRD_FLOAT`, **Lee** filter, **`log()`** σ⁰ | `SENTINEL1_GRD`, **σ⁰ ellipsoid**, **no** Lee / log |
| **Time stack** | **Per relative orbit** t-maps, then **max** over orbits | **One** composite per window (all passes together) |
| **Urban mask** | **Dynamic World** “built” > 0.1 | **None** |
| **Smoothing** | **Focal median** (10 m) then circle **convolutions** | **No** focal median; discrete **apply_kernel** disks |
| **Binary `damage`** | Threshold on **`t_smooth`** (before averaging in 50/100/150 m kernels) | Threshold on **`T_statistic`** (after the four-way average) |
| **`p_value`** | Normal CDF approximation + **Bonferroni** × orbit count | Different normal-style bound; **no** Bonferroni |

**Takeaway:** **Qualitative** agreement in strong change areas is plausible; **numerical identity** is not expected. Use **one** backend per analysis if you need a single consistent map.

---

## Output files (all backends)

| File | Content |
|------|--------|
| `pwtt_*.tif` | Band 1: **`T_statistic`**. Band 2: **`damage`** (rule depends on backend — see [GEE vs openEO](#gee-vs-openeo-why-results-differ-for-the-same-aoi)). Band 3: **`p_value`**. |
| `pwtt_footprints.gpkg` | Optional; building polygons with mean score per polygon (when enabled). |

**Threshold:** Band 2 uses the **T-statistic threshold** in the plugin UI (default 3.3). The **exact image** that is thresholded differs on **GEE** vs **openEO** (see table above).

---

## Jobs and projects

- Jobs are **global to the QGIS profile**, shared across all projects opened in that profile.
- **Rerun** creates a **new** job with the same parameters (new id).
- **Resume** continues the **same** job when status allows (e.g. stopped, failed, or waiting for offline products on Local).

---

## Code map

| Area | Location |
|------|----------|
| UI, jobs dock | `ui/main_dialog.py` |
| Background task | `core/pwtt_task.py` |
| Job persistence | `core/job_store.py` |
| openEO | `core/openeo_backend.py` |
| Local | `core/local_backend.py`, `core/downloader.py` |
| GEE | `core/gee_backend.py`, `core/gee_pwtt.py` |
