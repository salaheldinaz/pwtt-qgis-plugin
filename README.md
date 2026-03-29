# PWTT QGIS Plugin — Battle Damage Detection

QGIS plugin implementing the **Pixel-Wise T-Test (PWTT)** algorithm for building damage detection from Sentinel-1 SAR imagery. Choose among three processing backends: [openEO](https://openeo.org/) , [Google Earth Engine](https://earthengine.google.com/), or full-local processing.

## Links

- Project repo: [PWTT](https://github.com/oballinger/PWTT)
- Issue tracker: [GitHub Issues](https://github.com/oballinger/PWTT/issues)
- QGIS plugin docs: [QGIS Python Plugins](https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/)
- QGIS plugin manager docs: [Manage and Install Plugins](https://docs.qgis.org/latest/en/docs/user_manual/plugins/plugins.html)

## Requirements

- **[QGIS](https://qgis.org/)** 3.22 or later
- **Python packages** (depending on backend and features):
  - openEO backend: [openeo](https://pypi.org/project/openeo/)
  - GEE backend: [earthengine-api](https://pypi.org/project/earthengine-api/) (GEE logic is bundled; no repo `code/` folder needed)
  - Local backend: [numpy](https://pypi.org/project/numpy/), [scipy](https://pypi.org/project/scipy/), [rasterio](https://pypi.org/project/rasterio/), [requests](https://pypi.org/project/requests/)
  - Building footprints (optional): [geopandas](https://pypi.org/project/geopandas/), [rasterstats](https://pypi.org/project/rasterstats/)

Use the same Python that QGIS uses (e.g. from QGIS’s Python environment or OS package manager).

## Installation

### Option A: Install from ZIP (recommended)

1. Build a release (from the project root):
   ```bash
   ./scripts/build-release.sh
   ```
   This creates `releases/pwtt_qgis-<version>.zip`.

2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP** → select the ZIP file.

3. Enable the plugin in the list and install any backend-specific packages (see Requirements).

### Option B: Install from folder

1. Copy the `pwtt_qgis/` folder into your QGIS plugins directory:
   - **Linux/macOS:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
2. Restart QGIS, then **Plugins → Manage and Install Plugins** → enable **PWTT - Battle Damage Detection**.
3. Install backend-specific packages as needed (see Requirements).

## Backends

| Backend | Where it runs | Auth | Packages |
|--------|----------------|------|----------|
| **openEO**  | [Copernicus Data Space](https://dataspace.copernicus.eu/) (cloud) | OIDC browser or client ID/secret | `openeo` |
| **Google Earth Engine** | [GEE](https://earthengine.google.com/) (cloud) | `ee.Authenticate()` + optional project name | `earthengine-api` |
| **Local** | Your machine | CDSE username/password | `numpy`, `scipy`, `rasterio`, `requests` |

- **openEO:** No data download; result GeoTIFF is downloaded when the batch job finishes.
- **GEE:** Uses bundled PWTT logic; download is streamed to disk. Very large AOIs may require GEE Export to Drive instead of getDownloadURL.
- **Local:** Downloads [Sentinel-1 GRD](https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1) products from CDSE into a `.pwtt_cache` folder next to the output directory; processes with scipy/rasterio. Disk and RAM usage depend on AOI size and number of scenes.

## Usage

1. Open the tool: **PWTT** toolbar button or **Plugins → PWTT → PWTT - Battle Damage Detection**.
2. Select a **processing backend** and enter its credentials.
3. Click **Draw rectangle on map** and drag a rectangle on the map for the area of interest (AOI). The rectangle must have non-zero area.
4. Set **War start date** and **Inference start date** (inference start must be on or after war start).
5. Set **Pre-war interval** and **Post-war interval** (months).
6. Optionally check **Include building footprints (OSM)** to compute per-building mean damage scores (buildings from [OpenStreetMap](https://www.openstreetmap.org/) via [Overpass API](https://overpass-api.de/)).
7. Choose an **output directory**.
8. Click **Run**. Progress is shown in the bar and log. Results are added to the project when finished.

## Output files

- **pwtt_*.tif** — GeoTIFF (typically **three** bands):
  - Band 1: `T_statistic` — continuous score (higher = stronger change signal).
  - Band 2: `damage` — binary mask where the backend applies your **T-statistic threshold** (default 3.3). **GEE and openEO do not threshold the same intermediate surface** — see [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-why-results-differ-for-the-same-aoi).
  - Band 3: `p_value` — approximate significance (formula differs by backend).  
  For the **Local** backend, nodata is set to -9999 where applicable.

- **pwtt_footprints.gpkg** (optional) — GeoPackage with building polygons and a `T_statistic` column (mean damage score per building). Created only if **Include building footprints (OSM)** is checked.

### Reading colors on the map

QGIS often opens the GeoTIFF as **multiband color** (band 1 → red, band 2 → green, band 3 → blue). That **RGB blend is not** a single “damage heat map”: it mixes **T-statistic**, **binary damage (0/1)**, and **p-value**, so a given hue does **not** map one-to-one to “how damaged.”

For an intuitive heat-map reading, style **band 1** (`T_statistic`) as **singleband pseudocolor** with a ramp from **cool** (blue / purple) to **hot** (yellow / green), similar to the figures in the [PWTT project README](https://github.com/oballinger/PWTT). **Min/max** (or percentile stretch) in symbology controls how strong a value must be before it looks “yellow”; adjust those if the map looks all one color.

**After the layer is added:** Tweaking symbology changes **only the picture**, not the raster values. The plugin’s default pseudocolor on band 1 uses a fixed stretch (about **3–5** on `T_statistic`, same idea as the bundled Earth Engine preview). Narrowing **max** (e.g. 5 → 4) pushes more pixels toward the “hot” colors so change can **look** stronger without editing the file. **Band 2** (`damage`) is fixed for that export at the **threshold used when the job ran**; for a different binary mask, **re-run** with another threshold or use **Raster Calculator** (or similar) on band 1.

Then you can read the map in simple terms (still a **model of backscatter change**, not a survey of destroyed buildings — see the paper):

| Color | What it means |
|-------|----------------|
| 🟡 **Yellow** | **High confidence of damage** — radar backscatter changed a lot. Strongest change signal; often interpreted as severe or certain structural change in PWTT validation settings. |
| 🟢 **Green** | **Probable damage** — clear change in signal; likely affected. |
| 🟤 **Dark red / maroon** | **Some change detected**, but weaker — uncertain or partial effect. |
| 🔵 **Blue / purple** | **Little to no change** — backscatter stayed stable; likely **undamaged** in the sense of “no large pre/post shift.” |

Think of it as a **heat map of destruction (as inferred from SAR change)**: hotter (yellower) pixels mean the statistic is more extreme; cooler (bluer) pixels mean little change. Use **band 2** with **singleband** / two-class symbology if you only want the binary **above/below threshold** mask.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| War start date | 2022-02-22 | Start of the conflict (start of pre/post split). |
| Inference start date | 2024-07-01 | Start of the post-event window; must be ≥ war start. |
| Pre-war interval | 12 months | Length of the pre-event reference period before war start. |
| Post-war interval | 2 months | Length of the post-event assessment window after inference start. |

## How it works

**Full pipeline (all backends, jobs, outputs):** [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

Pre/post **months** define **date ranges**, not a dense “one image per day” series. Sentinel-1 **revisits** your AOI on a **repeat cycle**; only **actual GRD acquisitions** in those ranges are used. **openEO** builds temporal composites (mean / variance / count) for a pooled t-style statistic; **Local** uses at most **three** pre and **three** post scenes; **GEE** uses all matching passes **per orbit**. See [HOW_IT_WORKS.md](HOW_IT_WORKS.md#sentinel-1-grd-what-data-you-actually-get).

Conceptually, PWTT compares Sentinel-1 VV/VH **before** and **after** conflict using a **pooled t-test–style** signal plus **spatial smoothing**. **openEO**, **GEE**, and **Local** follow that idea with **different** preprocessing (e.g. Lee + log on GEE/Local vs σ⁰ ellipsoid on openEO), **orbit handling** (GEE per-orbit max vs openEO all-pass composites), **urban masking** (GEE only in this plugin), and **where the damage threshold is applied** (differs between GEE and openEO). See [HOW_IT_WORKS.md](HOW_IT_WORKS.md#gee-vs-openeo-why-results-differ-for-the-same-aoi).

Paper and method background: [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

## Building a release

From the project root:

```bash
./scripts/build-release.sh              # build ZIP from current version in metadata.txt
./scripts/build-release.sh --bump       # bump version from last commit, then build
./scripts/build-release.sh --bump minor # bump minor version, then build
```

Version is read from `pwtt_qgis/metadata.txt`. Output: `releases/pwtt_qgis-<version>.zip`.

## License

See the project root for license information.
