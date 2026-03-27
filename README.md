# PWTT QGIS Plugin — Battle Damage Detection

QGIS plugin implementing the **Pixel-Wise T-Test (PWTT)** algorithm for building damage detection from Sentinel-1 SAR imagery. Choose among three processing backends: [openEO](https://openeo.org/) (recommended), [Google Earth Engine](https://earthengine.google.com/), or full-local processing.

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
| **openEO** (recommended) | [Copernicus Data Space](https://dataspace.copernicus.eu/) (cloud) | OIDC browser or client ID/secret | `openeo` |
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

- **pwtt_result.tif** — Two-band GeoTIFF:
  - Band 1: `T_statistic` — continuous damage score (higher = more likely damaged)
  - Band 2: `damage` — binary mask (1 where T_statistic > 3)  
  For the local backend, nodata is set to -9999 where applicable.

- **pwtt_footprints.gpkg** (optional) — GeoPackage with building polygons and a `T_statistic` column (mean damage score per building). Created only if **Include building footprints (OSM)** is checked.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| War start date | 2022-02-22 | Start of the conflict (start of pre/post split). |
| Inference start date | 2024-07-01 | Start of the post-event window; must be ≥ war start. |
| Pre-war interval | 12 months | Length of the pre-event reference period before war start. |
| Post-war interval | 2 months | Length of the post-event assessment window after inference start. |

## How it works

PWTT compares Sentinel-1 SAR backscatter (VV/VH) before and after a conflict using a pixel-wise pooled t-test. A high t-statistic indicates statistically significant backscatter change, often due to building damage. The pipeline:

1. Lee speckle filter on GRD imagery  
2. Log transform  
3. Pixel-wise t-test (pre vs post), per orbit  
4. Max across orbits and VV/VH  
5. Focal median smoothing and multi-scale circular kernel averaging  
6. Urban mask (e.g. built-up fraction > 0.1)  
7. Damage threshold at T > 3  

Details: [PWTT paper (arXiv:2405.06323)](https://arxiv.org/pdf/2405.06323).

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
