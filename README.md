# PWTT QGIS Plugin — Battle Damage Detection

A QGIS plugin that implements the **Pixel-Wise T-Test (PWTT)** algorithm for
building damage detection from Sentinel-1 SAR imagery.  Three interchangeable
processing backends let you choose between cloud and local execution.

## Backends

| Backend | Processing | Auth | Extra packages |
|---------|-----------|------|----------------|
| **openEO / CDSE** (recommended) | Server-side on Copernicus Data Space | OIDC (browser) or client credentials | `openeo` |
| **Google Earth Engine** | Server-side on GEE | `ee.Authenticate()` + project name | `earthengine-api` |
| **Local** | Downloads S1 GRD scenes, processes with scipy/numpy | CDSE username/password | numpy, scipy, rasterio (bundled with QGIS) |

## Installation

1. Copy (or symlink) the `pwtt_qgis/` folder into your QGIS plugins directory:
   - **Linux/macOS:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
2. Restart QGIS.
3. Go to **Plugins → Manage and Install Plugins**, find "PWTT - Battle Damage
   Detection", and enable it.
4. Install backend-specific Python packages as needed:
   ```
   pip install openeo          # for openEO backend
   pip install earthengine-api # for GEE backend
   pip install geopandas rasterstats   # for building footprints (optional)
   ```

## Usage

1. Click the **PWTT** toolbar button (or **Plugins → PWTT → PWTT - Battle
   Damage Detection**).
2. Select a **processing backend** from the dropdown.
3. Enter credentials for the chosen backend.
4. Click **Draw rectangle on map** and drag a rectangle over the area of
   interest on the QGIS canvas.
5. Set the **war start date**, **inference start date**, and pre/post
   intervals.
6. Optionally check **Include building footprints (OSM)** to compute per-building
   damage scores (fetched from OpenStreetMap via Overpass).
7. Choose an **output directory**.
8. Click **Run**.  Processing runs in the background; the progress bar and log
   show status.
9. On completion, the damage raster (and optional footprints vector) are
   automatically loaded into the QGIS project.

## Output

- **pwtt_result.tif** — Two-band GeoTIFF:
  - Band 1: `T_statistic` — continuous damage score (higher = more likely damaged)
  - Band 2: `damage` — binary mask (1 where T_statistic > 3)
- **pwtt_footprints.gpkg** (optional) — GeoPackage with building polygons and
  a `T_statistic` column containing the mean damage score per building.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| War start date | 2022-02-22 | Date the conflict began |
| Inference start date | 2024-07-01 | Start of the post-event assessment window |
| Pre-war interval | 12 months | Reference period before war_start |
| Post-war interval | 2 months | Assessment window after inference_start |

## How It Works

The PWTT compares Sentinel-1 SAR backscatter amplitude before and after a
conflict using a pixel-wise pooled t-test.  A large t-statistic indicates a
statistically significant change in backscatter — typically caused by building
destruction.  The algorithm applies:

1. Lee speckle filter on raw GRD imagery
2. Log transform
3. Per-orbit pixel-wise t-test (pre vs post)
4. Max across orbits and VV/VH polarisations
5. Focal median smoothing + multi-scale circular kernel averaging
6. Urban mask (Dynamic World / WorldCover built-up fraction > 0.1)
7. Damage threshold at T > 3

For full details see the [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

## License

See the project root for license information.
