# How the PWTT QGIS plugin works

This document describes what the plugin does end-to-end: user inputs, job handling, and how each **backend** implements the analysis. For install and UI steps, see [README.md](README.md).

## Overview

The plugin estimates **building-related damage** from **Sentinel-1 GRD** backscatter (VV and VH) by comparing a **pre-war (baseline)** period to a **post-war** period over your **area of interest (AOI)**. Output is a two-band GeoTIFF (`pwtt_result.tif`): a continuous change score and a binary damage mask (threshold **> 3** on the score used for that backend).

The **Pixel-Wise T-Test (PWTT)** name matches the paper and the **GEE** / **Local** style pipelines most closely. The **openEO** backend uses a **simpler mean-difference** change map (see below).

**Reference:** [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

---

## What you configure

| Input | Meaning |
|--------|--------|
| **AOI** | Rectangle drawn on the map (stored as WKT). |
| **War start date** | Calendar date (`yyyy-MM-dd`). End of the pre baseline window and anchor for "pre" extent. |
| **Inference start date** | Calendar date. Start of the post window. |
| **Pre-war interval** | **Months** (integer). Baseline begins roughly `pre_interval` months **before** war start. |
| **Post-war interval** | **Months** (integer). Post window extends roughly that many months **after** inference start. |
| **Output directory** | Folder where `pwtt_result.tif` is written (and optional `pwtt_footprints.gpkg`). |

**Day vs month:** War start and inference start are **full dates**. Only the **length** of the baseline and post collections is set in **whole months** in the UI—there is no separate "N days" control.

**Backend** choice determines **where** computation runs and **exactly** how the change score is built.

---

## Plugin flow (orchestration)

1. **Controls dock** — You set parameters and click **Run**.
2. **Job record** — A job is created and appended to the persistent job list (`jobs.json` under the QGIS user profile, folder `PWTT`—**not** inside the `.qgz` project file).
3. **`PWTTRunTask` (QgsTask)** — Runs in the background: creates the output directory if needed, calls `backend.authenticate()` then `backend.run(...)` with `output_path = <output_dir>/pwtt_result.tif`.
4. **On success** — Job status is set to completed; `output_tif` is stored on the job; the raster (and footprints layer if any) is **added to the current QGIS project**.
5. **Jobs dock** — Lists jobs, **Resume** / **Rerun** / **Delete**, progress and log. **Rerun** clones parameters into a **new** job id.

**openEO batch jobs:** While running, the log shows a server **batch job id** (`j-…`). That id is **not** saved in `jobs.json`. To re-download results from the API you need that id (or list jobs via the openEO client) within the provider's result retention policy (Copernicus Data Space: **90 days after job completion** as of 2025-05-06; see their announcements).

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
| **openEO** | Every observation in the cube over each window is included in a **temporal mean** → one **pre** composite and one **post** composite per pixel. You do not get a per-date stack in the output file—only those means and their difference. |
| **Local** | Catalogue search returns candidate IW GRD products; the plugin downloads and uses **at most 3 pre and 3 post** scenes (to limit disk and runtime), not every acquisition in the window. |
| **GEE** | Image collections include **all** GRD images in the filtered date range that match AOI and mode; processing is **per relative orbit**, then combined (see below). |

So: you are always working from **real GRD granules in your chosen months**, not synthetic “all days before/after,” and **Local** deliberately uses a **small subset** of those granules.

---

## Backend: openEO (Copernicus Data Space)

**Connection:** `https://openeo.dataspace.copernicus.eu`  
**Auth:** OIDC (browser) or client id + secret.

**Processing (graph):**

1. Load collection **SENTINEL1_GRD** with VV and VH over the pre spatial/temporal extent; **SAR backscatter** (σ⁰ ellipsoid); **reduce_dimension** over time with **mean** → pre composite.
2. Same for the post extent → post composite.
3. **Change:** `abs(post − pre)` per pixel; **reduce_dimension** over bands with **max** → single band (max of VV/VH absolute change).

This is **not** the full pooled t-test pipeline from the paper; it is a **temporal mean difference** change map, run as a **batch job**, then **downloaded** as GeoTIFF.

---

## Backend: Local (CDSE download + NumPy/SciPy/rasterio)

**Auth:** CDSE username / password.

**Processing:**

1. **Search** Sentinel-1 IW GRD for pre and post windows; **download** products into `<output_dir>/.pwtt_cache` (skip/wait logic for offline products).
2. Use up to **3** pre and **3** post scenes; **Lee** speckle filter; **log** σ⁰; reproject to a common grid.
3. **Per-pixel** comparison of pre vs post stacks: **Welch-style t**-type statistic for **VV** and **VH**; take **element-wise max** of the two.
4. **Post-processing:** Gaussian-style smoothing, circular-kernel means at 50 / 100 / 150 m; combined **T_statistic**; **damage** = 1 where statistic **> 3**.
5. Write **GeoTIFF** (2 bands). Optional **footprints** step can aggregate to OSM buildings (`pwtt_footprints.gpkg`).

---

## Backend: Google Earth Engine

**Auth:** Earth Engine credentials; optional GEE project name.

**Processing (bundled `gee_pwtt`):**

1. Filter **COPERNICUS/S1_GRD_FLOAT** (IW, VV+VH) by AOI and post window; get **distinct relative orbits** in that window.
2. **Per orbit:** Lee filter, **log**, then **t-test–style** map: pre vs post **means** and **pooled** variability (sample size from distinct orbit passes in the collections).
3. **Max** across orbits; **Dynamic World** built-up layer used as an **urban mask** (built mean > 0.1 in the pre window).
4. **Focal median** and circular convolutions → **T_statistic** and **damage** (> 3).
5. **getDownloadURL** streams a GeoTIFF to disk.

Very large AOIs may hit GEE download limits; export to Drive may be needed outside this plugin.

---

## Output files (all backends)

| File | Content |
|------|--------|
| `pwtt_result.tif` | Band 1: continuous score (`T_statistic` or equivalent change strength). Band 2: binary `damage` (1 where score > 3). |
| `pwtt_footprints.gpkg` | Optional; building polygons with mean score per polygon (when enabled). |

**Semantic note:** The **openEO** band 1 is a **mean absolute backscatter change** (max of VV/VH), not the same statistic as GEE/Local **T_statistic**, but band 2 still uses the **> 3** rule for a binary mask in the plugin output structure.

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
