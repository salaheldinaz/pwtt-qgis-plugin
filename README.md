# PWTT QGIS Plugin — Battle Damage Detection

QGIS plugin implementing the **Pixel-Wise T-Test (PWTT)** algorithm for building damage detection from Sentinel-1 SAR imagery. Choose among three processing backends: [openEO](https://openeo.org/), [Google Earth Engine](https://earthengine.google.com/), or full-local processing.

- **PWTT method / reference implementation:** [oballinger/PWTT](https://github.com/oballinger/PWTT)

## Intro

**SAR (Sentinel‑1)** is radar from space: the satellite measures microwave **backscatter** from the ground. Built structures affect that signal. The plugin uses two polarisations, **VV** and **VH**, as complementary views of the same place. You do not get a tidy daily photo—only **individual overpasses** when Sentinel‑1 covers your area.

**PWTT (Pixel‑Wise T‑Test)** asks: *did backscatter change a lot between a **baseline** period and a **later** period, in a way that fits damage mapping?* In short: (1) summarise SAR over months **before** your **war/event** date, (2) summarise SAR over months **after** your **inference start** date, (3) compare them per pixel and polarisation to get a **change score** (exported mainly as **`T_statistic`** on band 1), (4) mark **damage** where that score is **above** your cutoff (default **3.3**). It is era‑to‑era comparison, not “damage from a single scene.”

**This QGIS plugin** is the front door: draw an **AOI**, set **dates** and **pre/post month spans**, pick a **backend** (openEO, Google Earth Engine, or local download + NumPy), run in the background, then get a **GeoTIFF** on disk and on the map—plus `job_info.json`, optional footprint layers, and (for GEE/Local) per-acquisition **TimeSeries sidecars**. The main raster product has three bands: `T_statistic`, `damage`, `p_value`; the TimeSeries chart in the Jobs dock shows per-acquisition, orbit-normalized z-scores when a sidecar was written.

**How the analysis works (same idea on every backend; recipes differ):** restrict Sentinel‑1 GRD to your AOI and pre/post windows → **aggregate** many acquisitions into summaries (means, variances, counts—or orbit‑wise steps on GEE) → run a **statistical comparison** (pooled *t*‑style on openEO/Local; GEE adds options like per‑orbit combination, Lee filter, log scale, urban mask, focal smoothing—see [HOW_IT_WORKS.md](HOW_IT_WORKS.md)) → **smooth** spatially to tame speckle → **damage** = **`T_statistic` > threshold** on the exported band 1.

**Reading the map:** band 1 = strength of change; band 2 = binary above‑cutoff mask; band 3 = approximate *p*‑value (details vary by backend). For symbology and why GEE vs openEO vs Local differ, use [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---
## Requirements

- **[QGIS](https://qgis.org/)** 3.22 or later
- **Python packages** (depending on backend and features):
  - openEO backend: [openeo](https://pypi.org/project/openeo/)
  - GEE backend: [earthengine-api](https://pypi.org/project/earthengine-api/) (GEE logic is bundled; no repo `code/` folder needed)
  - Local backend (CDSE): [numpy](https://pypi.org/project/numpy/), [rasterio](https://pypi.org/project/rasterio/), [requests](https://pypi.org/project/requests/)
  - Local backend (ASF): [asf-search](https://pypi.org/project/asf-search/) (Python 3.10+ upstream requirement), [requests](https://pypi.org/project/requests/)
  - Local backend (Planetary Computer): [planetary-computer](https://pypi.org/project/planetary-computer/), [pystac-client](https://pypi.org/project/pystac-client/), [requests](https://pypi.org/project/requests/)
  - Building footprints (optional): [geopandas](https://pypi.org/project/geopandas/), [rasterstats](https://pypi.org/project/rasterstats/), and small transitive deps (see `core/deps.py`)

The plugin installs missing **pip** packages into **`PWTT/deps/`** under your [QGIS user profile](https://docs.qgis.org/latest/en/docs/user_manual/introduction/qgis_configuration.html#user-profiles) (prefers a bundled **uv** binary, then QGIS’s Python **pip**). In **PWTT — Damage Detection**, use **Install Dependencies** when the panel reports missing imports. Core raster stack (**numpy**, **rasterio**, **requests**) usually comes from QGIS itself.

---
## Installation

### Option A: Install from ZIP (recommended)

1. Build a release (from the project root):
   ```bash
   ./scripts/build-release.sh
   ```
   This writes **`build/pwtt_qgis-<version>.zip`** (gitignored; attach it to a forge/GitHub Release).

2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP** → select the ZIP.

3. Enable the plugin and use **Install Dependencies** in the PWTT panel if anything is still missing (see Requirements).

### Option B: Install from folder

1. The ZIP contains a single folder named **`pwtt_qgis`**. For a git checkout, copy the repo contents into `python/plugins/pwtt_qgis/` so QGIS loads the same layout (folder name must be **`pwtt_qgis`**).
   - **Linux/macOS (default profile):** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/pwtt_qgis/`
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\pwtt_qgis\`
2. Restart QGIS, then **Plugins → Manage and Install Plugins** → enable **PWTT - Battle Damage Detection**.
3. Use **Install Dependencies** in the PWTT panel as needed.

---
## Backends

| Backend | Where it runs | Auth | Packages |
|--------|----------------|------|----------|
| **openEO**  | [Copernicus Data Space](https://dataspace.copernicus.eu/) (cloud) | OIDC browser or client ID/secret | `openeo` |
| **Google Earth Engine** | [GEE](https://earthengine.google.com/) (cloud) | `ee.Authenticate()` + optional project name | `earthengine-api` |
| **Local** | Your machine | Source-specific: CDSE creds, Earthdata creds (ASF), optional PC key | `numpy`, `rasterio`, `requests` (+ source-specific packages) |

- **openEO:** No data download; result GeoTIFF is downloaded when the batch job finishes.
- **GEE:** Uses bundled **`gee_pwtt`** (synced with upstream PWTT): configurable **detection method** (Stouffer default), Welch/pooled *t*, smoothing and Lee modes in **Advanced GEE options**. Download is streamed to disk (**3 bands** only). Very large AOIs may require GEE Export to Drive instead of getDownloadURL.
- **Local:** Select source in UI: CDSE, ASF, or Microsoft Planetary Computer. Downloads [Sentinel-1 GRD](https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1) into **`<output_dir>/.pwtt_cache`**, then runs an **openEO-aligned** NumPy pipeline (σ⁰ linear, no Lee/log; pooled *t*-style statistic; same kernel idea as CDSE openEO — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md)). By default uses up to **24** pre and **24** post scenes per job (cap **80**, setting `PWTT/local_max_scenes_per_period`). Disk and RAM depend on AOI size and that cap.

---
## Usage

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| War/Event start date | *(example: 2023-10-07)* | Start of the conflict or event; the pre-war/event window ends at this date. |
| Inference start date | *(example: 2024-07-01)* | Start of the post-event assessment window; must be ≥ war/event start. |
| Pre-war/event interval | 12 months | Length of the pre-event reference period before war/event start. |
| Post-war/event interval | 2 months | Length of the post-event assessment window after inference start. |
| T-statistic cutoff | 3.3 | Binary **damage** (band 2) where **`T_statistic` > cutoff** on all backends. Higher → stricter (fewer pixels flagged); not a probability. |
| GEE detection method | Stouffer | **GEE only.** Stouffer (default), Max, Z-test, Hotelling T², or Mahalanobis — how per-orbit tests are combined. |
| GEE advanced options | (see UI) | **GEE only.** Welch vs pooled *t*-test; default vs focal-only smoothing; urban mask before/after focal median; Lee per-image vs composite. Stored on jobs and **Rerun**. |


1. Open **PWTT — Damage Detection** from the **PWTT** toolbar or **Plugins → PWTT**. Other PWTT docks (toggle from the toolbar): **Jobs**, **Job log**, **openEO Jobs**, **GRD staging** (CDSE offline ordering).
2. Select a **processing backend** and enter its credentials. If imports fail, use **Install Dependencies** in this panel.
3. Define the **AOI**: **Draw rectangle on map**, or enter bounds and **Set AOI from coordinates**. **Hide on map** / **Show on map** only toggles the orange overlay; the stored rectangle is unchanged.
4. Set **War/Event start date** and **Inference start date** (inference ≥ war/event start).
5. Set **Pre-war/event interval** and **Post-war/event interval** (months).
6. Optionally enable **Include building footprints** and choose one or more snapshot types: current OSM, historical at war/event start, and/or historical at inference start ([Overpass API](https://overpass-api.de/)). Each selection becomes a separate GeoPackage layer.
7. Set **Damage mask (T-statistic cutoff)** if you want something other than the default **3.3**. Higher values flag **fewer** pixels (stricter); this is a **test-statistic** cutoff, not a probability. Binary **damage** (band 2) is **`T_statistic` > cutoff** on the exported raster for every backend; **GEE** still builds **`T_statistic`** differently from openEO/Local — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-vs-local-why-results-differ-for-the-same-aoi).
8. For **Google Earth Engine** only: choose **Detection method (GEE only)** (default **Stouffer**); expand **Advanced options** for **T-test type**, **Smoothing**, **Mask urban pixels before focal median**, and **Lee filter mode**.
9. For **Google Earth Engine** only, you can check **Open interactive map in browser** (needs **geemap** in the QGIS Python environment) for a quick HTML preview before the GeoTIFF downloads.
10. Choose an **output directory**.
11. Confirm the summary dialog, then **Run**. Progress appears in the task bar and **PWTT — Job log** / Jobs dock. The raster (and footprint layers if any) are added to the project when finished.

---
## Output files

- **pwtt_*.tif** — GeoTIFF (typically **three** bands):
  - Band 1: `T_statistic` — continuous score (higher = stronger change signal).
  - Band 2: `damage` — binary mask where **`T_statistic` > cutoff** (default 3.3). **GEE**, **openEO**, and **Local** each compute **`T_statistic`** differently — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-vs-local-why-results-differ-for-the-same-aoi).
  - Band 3: `p_value` — approximate significance (formula differs by backend).  
  For the **Local** backend, nodata is set to -9999 where applicable.

- **`job_info.json`** (next to the GeoTIFF) — run metadata: parameters, optional `processing_details` from the backend, `damage_threshold`, timestamps.

- **`pwtt_job.json`** in the output folder — copy of the job record envelope (export/import friendly; written when the job is saved).

- **Footprints GeoPackages** (optional) — building polygons with a **`T_statistic`** column (mean of band 1 over each polygon). Names:
  - With job id: `pwtt_<job_id>_footprints_current.gpkg`, `_war_start.gpkg`, `_infer_start.gpkg` (depending on which footprint sources you selected).
  - Without job id: `pwtt_footprints_current.gpkg`, etc.

- **TimeSeries sidecars** (GEE and Local only, when available) — written beside the GeoTIFF after a successful run:
  - `pwtt_<job_id>_timeseries.json` — per-acquisition, orbit-normalized z-scores for VV and VH; read by the **TimeSeries chart** dialog in the Jobs dock.
  - `pwtt_<job_id>_timeseries.csv` — same data in CSV format (compatible with Earth Engine Code Editor export).
  - If no sidecar exists (e.g. openEO jobs), the TimeSeries chart cannot be reconstructed from the GeoTIFF alone.

### Reading colors on the map

QGIS often opens the GeoTIFF as **multiband color** (band 1 → red, band 2 → green, band 3 → blue). That **RGB blend is not** a single “damage heat map”: it mixes **T-statistic**, **binary damage (0/1)**, and **p-value**, so a given hue does **not** map one-to-one to “how damaged.”

For an intuitive reading of **band 1**, use **singleband pseudocolor**. The plugin’s default ramp matches the reference PWTT Earth Engine preview (**`core/viz_constants.py`**, **`core/qgis_output_style.py`**): **yellow** at the stretch **minimum**, **red** at the midpoint, **purple** at the **maximum** (default min **3.0** / max **5.0** on `T_statistic`). That is **not** a “blue = cold, yellow = hot” weather map: **higher** `T_statistic` in that window is drawn **more purple**; **lower** in the window is **more yellow**. **Min/max** (or percentile stretch) in symbology controls which numeric range maps to those hues; change the ramp there if you want a different metaphor.

**After the layer is added:** Tweaking symbology changes **only the picture**, not the raster values. Narrowing **max** (e.g. 5 → 4) maps the same high values further toward the **red–purple** end of the ramp so strong change can **look** more vivid without editing the file. **Band 2** (`damage`) is fixed for that export at the **cutoff used when the job ran**; for a different binary mask, **re-run** with another cutoff or use **Raster Calculator** (or similar) on band 1.

With the **default** yellow → red → purple ramp (still a **model of backscatter change**, not a survey of destroyed buildings — see the paper):

| Color | What it means |
|-------|----------------|
| 🟣 **Purple** | **`T_statistic` near the stretch max** (default 5.0) — strongest change signal **within the 3–5 display window**. |
| 🔴 **Red** | **Mid stretch** (~4) — strong change between min and max. |
| 🟡 **Yellow** | **Near the stretch min** (default 3.0) — elevated signal in the band, but the **lowest** end of this ramp (not the strongest hue). |

Pixels with `T_statistic` **well below** your symbology minimum may render as transparent or a flat color depending on QGIS settings — that is **not** “purple means undamaged.” For a strict above/below mask, use **band 2** with **singleband** / two-class symbology. If you choose **another** ramp, read colors from that ramp’s legend, not this table.

## How it works

Full pipeline details — backends, temporal windows, GEE vs openEO vs Local differences, output bands, jobs, code map: **[HOW_IT_WORKS.md](HOW_IT_WORKS.md)**.

Paper and method background: [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

---
## Building a release

From the project root:

```bash
./scripts/build-release.sh              # build ZIP from current version in metadata.txt
./scripts/build-release.sh --bump       # bump version from last commit, then build
./scripts/build-release.sh --bump minor # bump minor version, then build
```

Version is read from `metadata.txt` at the repo root (same file inside the `pwtt_qgis` folder in the ZIP). Output: **`build/pwtt_qgis-<version>.zip`**.

---
## License

This plugin is licensed under the **GNU General Public License v2.0 or later** — see [`LICENSE`](LICENSE). That matches QGIS (GPL), which this code extends.

The PWTT methodology and GEE reference logic credit **Oliver Ballinger**’s [`oballinger/PWTT`](https://github.com/oballinger/PWTT) (arXiv:2405.06323); see the header in `core/gee_pwtt.py` and the notice at the top of `LICENSE`.
