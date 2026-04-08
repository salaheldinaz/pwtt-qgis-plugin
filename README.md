# PWTT QGIS Plugin — Battle Damage Detection

QGIS plugin implementing the **Pixel-Wise T-Test (PWTT)** algorithm for building damage detection from Sentinel-1 SAR imagery. Choose among three processing backends: [openEO](https://openeo.org/) , [Google Earth Engine](https://earthengine.google.com/), or full-local processing.

## Links

- **This plugin (source, releases, issues):** [PWTT-QGIS-Plugin](https://github.com/Salaheldinaz/PWTT-QGIS-Plugin)
- **PWTT method / reference implementation:** [oballinger/PWTT](https://github.com/oballinger/PWTT)
- QGIS plugin docs: [QGIS Python Plugins](https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/)
- QGIS plugin manager: [Manage and Install Plugins](https://docs.qgis.org/latest/en/docs/user_manual/plugins/plugins.html)

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

## Backends

| Backend | Where it runs | Auth | Packages |
|--------|----------------|------|----------|
| **openEO**  | [Copernicus Data Space](https://dataspace.copernicus.eu/) (cloud) | OIDC browser or client ID/secret | `openeo` |
| **Google Earth Engine** | [GEE](https://earthengine.google.com/) (cloud) | `ee.Authenticate()` + optional project name | `earthengine-api` |
| **Local** | Your machine | Source-specific: CDSE creds, Earthdata creds (ASF), optional PC key | `numpy`, `rasterio`, `requests` (+ source-specific packages) |

- **openEO:** No data download; result GeoTIFF is downloaded when the batch job finishes.
- **GEE:** Uses bundled **`gee_pwtt`** (synced with upstream PWTT): configurable **detection method** (Stouffer default), Welch/pooled *t*, smoothing and Lee modes in **Advanced GEE options**. Download is streamed to disk (**3 bands** only). Very large AOIs may require GEE Export to Drive instead of getDownloadURL.
- **Local:** Select source in UI: CDSE, ASF, or Microsoft Planetary Computer. Downloads [Sentinel-1 GRD](https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1) into **`<output_dir>/.pwtt_cache`**, then runs an **openEO-aligned** NumPy pipeline (σ⁰ linear, no Lee/log; pooled *t*-style statistic; same kernel idea as CDSE openEO — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md)). By default uses up to **24** pre and **24** post scenes per job (cap **80**, setting `PWTT/local_max_scenes_per_period`). Disk and RAM depend on AOI size and that cap.

## Usage

1. Open **PWTT — Damage Detection** from the **PWTT** toolbar or **Plugins → PWTT**. Other PWTT docks (toggle from the toolbar): **Jobs**, **Job log**, **openEO Jobs**, **GRD staging** (CDSE offline ordering).
2. Select a **processing backend** and enter its credentials. If imports fail, use **Install Dependencies** in this panel.
3. Define the **AOI**: **Draw rectangle on map**, or enter bounds and **Set AOI from coordinates**. **Hide on map** / **Show on map** only toggles the orange overlay; the stored rectangle is unchanged.
4. Set **War start date** and **Inference start date** (inference ≥ war start).
5. Set **Pre-war interval** and **Post-war interval** (months).
6. Optionally enable **Include building footprints (OSM)** and choose one or more snapshot types: current OSM, historical at war start, and/or historical at inference start ([Overpass API](https://overpass-api.de/)). Each selection becomes a separate GeoPackage layer.
7. Set **Damage mask (T-statistic threshold)** if you want something other than the default **3.3**. Binary **damage** (band 2) is **`T_statistic` > threshold** on the exported raster for every backend; **GEE** still builds **`T_statistic`** differently from openEO/Local — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-vs-local-why-results-differ-for-the-same-aoi).
8. For **Google Earth Engine** only: choose **Detection method (GEE only)** (default **Stouffer**); expand **Advanced options** for **T-test type**, **Smoothing**, **Mask urban pixels before focal median**, and **Lee filter mode**.
9. For **Google Earth Engine** only, you can check **Open interactive map in browser** (needs **geemap** in the QGIS Python environment) for a quick HTML preview before the GeoTIFF downloads.
10. Choose an **output directory**.
11. Confirm the summary dialog, then **Run**. Progress appears in the task bar and **PWTT — Job log** / Jobs dock. The raster (and footprint layers if any) are added to the project when finished.

## Output files

- **pwtt_*.tif** — GeoTIFF (typically **three** bands):
  - Band 1: `T_statistic` — continuous score (higher = stronger change signal).
  - Band 2: `damage` — binary mask where **`T_statistic` > threshold** (default 3.3). **GEE**, **openEO**, and **Local** each compute **`T_statistic`** differently — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-vs-local-why-results-differ-for-the-same-aoi).
  - Band 3: `p_value` — approximate significance (formula differs by backend).  
  For the **Local** backend, nodata is set to -9999 where applicable.

- **`job_info.json`** (next to the GeoTIFF) — run metadata: parameters, optional `processing_details` from the backend, `damage_threshold`, timestamps.

- **`pwtt_job.json`** in the output folder — copy of the job record envelope (export/import friendly; written when the job is saved).

- **Footprints GeoPackages** (optional) — building polygons with a **`T_statistic`** column (mean of band 1 over each polygon). Names:
  - With job id: `pwtt_<job_id>_footprints_current.gpkg`, `_war_start.gpkg`, `_infer_start.gpkg` (depending on which footprint sources you selected).
  - Without job id: `pwtt_footprints_current.gpkg`, etc.

### Reading colors on the map

QGIS often opens the GeoTIFF as **multiband color** (band 1 → red, band 2 → green, band 3 → blue). That **RGB blend is not** a single “damage heat map”: it mixes **T-statistic**, **binary damage (0/1)**, and **p-value**, so a given hue does **not** map one-to-one to “how damaged.”

For an intuitive reading of **band 1**, use **singleband pseudocolor**. The plugin’s default ramp matches the reference PWTT Earth Engine preview (**`core/viz_constants.py`**, **`core/qgis_output_style.py`**): **yellow** at the stretch **minimum**, **red** at the midpoint, **purple** at the **maximum** (default min **3.0** / max **5.0** on `T_statistic`). That is **not** a “blue = cold, yellow = hot” weather map: **higher** `T_statistic` in that window is drawn **more purple**; **lower** in the window is **more yellow**. **Min/max** (or percentile stretch) in symbology controls which numeric range maps to those hues; change the ramp there if you want a different metaphor.

**After the layer is added:** Tweaking symbology changes **only the picture**, not the raster values. Narrowing **max** (e.g. 5 → 4) maps the same high values further toward the **red–purple** end of the ramp so strong change can **look** more vivid without editing the file. **Band 2** (`damage`) is fixed for that export at the **threshold used when the job ran**; for a different binary mask, **re-run** with another threshold or use **Raster Calculator** (or similar) on band 1.

With the **default** yellow → red → purple ramp (still a **model of backscatter change**, not a survey of destroyed buildings — see the paper):

| Color | What it means |
|-------|----------------|
| 🟣 **Purple** | **`T_statistic` near the stretch max** (default 5.0) — strongest change signal **within the 3–5 display window**. |
| 🔴 **Red** | **Mid stretch** (~4) — strong change between min and max. |
| 🟡 **Yellow** | **Near the stretch min** (default 3.0) — elevated signal in the band, but the **lowest** end of this ramp (not the strongest hue). |

Pixels with `T_statistic` **well below** your symbology minimum may render as transparent or a flat color depending on QGIS settings — that is **not** “purple means undamaged.” For a strict above/below mask, use **band 2** with **singleband** / two-class symbology. If you choose **another** ramp, read colors from that ramp’s legend, not this table.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| War start date | 2022-02-22 | Start of the conflict (baseline ends at this date). |
| Inference start date | 2024-07-01 | Start of the post-event window; must be ≥ war start. |
| Pre-war interval | 12 months | Length of the pre-event reference period before war start. |
| Post-war interval | 2 months | Length of the post-event assessment window after inference start. |
| T-statistic threshold | 3.3 | Cutoff for binary **damage** (band 2): **`T_statistic` > threshold** on all backends. |
| GEE detection method | Stouffer | **GEE only.** Stouffer (default), Max, Z-test, Hotelling T², or Mahalanobis — how per-orbit tests are combined. |
| GEE advanced options | (see UI) | **GEE only.** Welch vs pooled *t*-test; default vs focal-only smoothing; urban mask before/after focal median; Lee per-image vs composite. Stored on jobs and **Rerun**. |

## How it works

**Full pipeline (all backends, jobs, outputs):** [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

Pre/post **months** define **date ranges**, not a dense “one image per day” series. Sentinel-1 **revisits** your AOI on a **repeat cycle**; only **actual GRD acquisitions** in those ranges are used. **openEO** builds temporal composites (mean / variance / count) for a pooled *t*-style statistic; **Local** uses up to **N** pre and **N** post scenes (default **N = 24**, max **80**, via `PWTT/local_max_scenes_per_period`); **GEE** uses all matching passes **per orbit**. See [HOW_IT_WORKS.md](HOW_IT_WORKS.md#sentinel-1-grd-what-data-you-actually-get).

Conceptually, PWTT compares Sentinel-1 VV/VH **before** and **after** conflict using a **pooled *t*-test–style** signal plus **spatial smoothing**. **openEO** and **Local** (this plugin) share **σ⁰ linear** radiometry and similar composite/kernel logic; **GEE** uses **Lee + log** on `COPERNICUS/S1_GRD_FLOAT`, **per-orbit** tests merged by a selectable **method**, **Dynamic World** urban masking, and **focal median** (optional multi-scale follow-up). All backends export **`damage`** as **`T_statistic` > threshold**, but **GEE**’s **`T_statistic`** is built on that pipeline — so **GEE** can diverge more from **openEO/Local** than those two diverge from each other. See [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-vs-local-why-results-differ-for-the-same-aoi).

Paper and method background: [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

## Building a release

From the project root:

```bash
./scripts/build-release.sh              # build ZIP from current version in metadata.txt
./scripts/build-release.sh --bump       # bump version from last commit, then build
./scripts/build-release.sh --bump minor # bump minor version, then build
```

Version is read from `metadata.txt` at the repo root (same file inside the `pwtt_qgis` folder in the ZIP). Output: **`build/pwtt_qgis-<version>.zip`**.

## License

This repository does not include a `LICENSE` file; check the [plugin homepage](https://github.com/Salaheldinaz/PWTT-QGIS-Plugin) and upstream [PWTT](https://github.com/oballinger/PWTT) for terms.
