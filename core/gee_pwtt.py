# -*- coding: utf-8 -*-
"""Bundled GEE PWTT logic (lee_filter, ttest, detect_damage) so the plugin works when installed from ZIP."""

import math
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


def normal_cdf_approx(x_image):
    """Approximate standard normal CDF for positive x using Abramowitz & Stegun 26.2.17.
    Max error < 7.5e-8. Operates entirely on ee.Image objects (server-side).
    """
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.882575977
    b5 = 1.330274429

    t = ee.Image.constant(1).divide(
        ee.Image.constant(1).add(ee.Image.constant(0.2316419).multiply(x_image))
    )
    phi = x_image.pow(2).multiply(-0.5).exp().divide(math.sqrt(2 * math.pi))

    # Horner's method: poly = t*(b1 + t*(b2 + t*(b3 + t*(b4 + t*b5))))
    poly = t.multiply(
        ee.Image.constant(b1).add(t.multiply(
            ee.Image.constant(b2).add(t.multiply(
                ee.Image.constant(b3).add(t.multiply(
                    ee.Image.constant(b4).add(t.multiply(b5))
                ))
            ))
        ))
    )
    return ee.Image.constant(1).subtract(phi.multiply(poly))


def two_tailed_pvalue(t_image):
    """Compute two-tailed p-value from absolute t-values using normal approximation.
    Valid for large degrees of freedom (df > 30).
    """
    cdf = normal_cdf_approx(t_image)
    return ee.Image.constant(2).multiply(
        ee.Image.constant(1).subtract(cdf)
    ).max(ee.Image.constant(1e-10))


def lee_filter(image):
    KERNEL_SIZE = 2
    band_names = image.bandNames().remove("angle")
    enl = 5
    eta = 1.0 / enl ** 0.5
    eta = ee.Image.constant(eta)
    one_img = ee.Image.constant(1)
    reducers = ee.Reducer.mean().combine(
        reducer2=ee.Reducer.variance(),
        sharedInputs=True,
    )
    stats = image.select(band_names).reduceNeighborhood(
        reducer=reducers,
        kernel=ee.Kernel.square(KERNEL_SIZE / 2, "pixels"),
        optimization="window",
    )
    mean_band = band_names.map(lambda b: ee.String(b).cat("_mean"))
    var_band = band_names.map(lambda b: ee.String(b).cat("_variance"))
    z_bar = stats.select(mean_band)
    varz = stats.select(var_band)
    varx = (varz.subtract(z_bar.pow(2).multiply(eta.pow(2)))).divide(one_img.add(eta.pow(2)))
    b = varx.divide(varz)
    new_b = b.where(b.lt(0), 0)
    output = one_img.subtract(new_b).multiply(z_bar.abs()).add(new_b.multiply(image.select(band_names)))
    output = output.rename(band_names)
    return image.addBands(output, None, True)


def ttest(s1, inference_start, war_start, pre_interval, post_interval):
    inference_start = ee.Date(inference_start)
    pre = s1.filterDate(
        war_start.advance(ee.Number(pre_interval).multiply(-1), "month"),
        war_start,
    )
    post = s1.filterDate(inference_start, inference_start.advance(post_interval, "month"))

    pre_mean = pre.mean()
    pre_sd = pre.reduce(ee.Reducer.stdDev())
    pre_n = pre.select("VV").count()

    post_mean = post.mean()
    post_sd = post.reduce(ee.Reducer.stdDev())
    post_n = post.select("VV").count()

    pooled_sd = (
        pre_sd.pow(2)
        .multiply(pre_n.subtract(1))
        .add(post_sd.pow(2).multiply(post_n.subtract(1)))
        .divide(pre_n.add(post_n).subtract(2))
        .sqrt()
    )
    denom = pooled_sd.multiply(
        ee.Image(1).divide(pre_n).add(ee.Image(1).divide(post_n)).sqrt()
    )
    change = post_mean.subtract(pre_mean).divide(denom).abs()

    # Compute two-tailed p-values (normal approx, valid for df > 30)
    p_values = two_tailed_pvalue(change).rename(["VV_pvalue", "VH_pvalue"])

    # Return t-values, p-values, and sample sizes
    return (
        change
        .addBands(p_values)
        .addBands(pre_n.toFloat().rename("n_pre"))
        .addBands(post_n.toFloat().rename("n_post"))
    )


def open_geemap_preview(
    aoi,
    image,
    output_dir: str = None,
) -> None:
    """Build a standalone Leaflet map and open it in the default browser (QGIS / desktop use).

    Uses Leaflet.js from CDN so the page works as a file:// URL without Jupyter
    widget dependencies.  CartoDB Positron tiles are used as the basemap to avoid
    the Referer requirement imposed by OpenStreetMap's volunteer-run tile servers.

    T_statistic stretch (min/max/opacity) matches ``code/pwtt.py`` Earth Engine viz.

    If *output_dir* is given the HTML is saved there as ``pwtt_gee_preview.html``
    (in addition to being opened in the browser); otherwise a system temp file is used.
    """
    vis_params = {
        "min": T_STATISTIC_VIZ_MIN,
        "max": T_STATISTIC_VIZ_MAX,
        "palette": ["yellow", "red", "purple"],
    }

    # Fetch EE tile URL and AOI geometry (both are cheap server-side calls).
    map_id_dict = image.select("T_statistic").getMapId(vis_params)
    ee_tile_url = map_id_dict["tile_fetcher"].url_format

    # AOI centroid and bounding box for initial map view.
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

  // CartoDB Positron - does not require a Referer header (unlike OSM tiles).
  L.tileLayer(
    'https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      subdomains: 'abcd',
      maxZoom: 20
    }}
  ).addTo(map);

  // Earth Engine T-statistic overlay. EE only serves tiles through a native zoom
  // (~10 m S1 → ~z14 WebMercator); without maxNativeZoom, Leaflet requests
  // higher z and tiles are empty so the layer looks to "disappear" on zoom-in.
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


def detect_damage(
    aoi,
    inference_start,
    war_start,
    pre_interval=12,
    post_interval=2,
    footprints=None,
    viz=False,
    viz_return_map=False,
    damage_threshold=DEFAULT_DAMAGE_THRESHOLD,
    export=False,
    export_dir="PWTT_Export",
    export_name=None,
    export_scale=10,
    grid_scale=500,
    export_grid=False,
    clip=True,
):
    inference_start = ee.Date(inference_start)
    war_start = ee.Date(war_start)

    orbits = (
        ee.ImageCollection("COPERNICUS/S1_GRD_FLOAT")
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filterBounds(aoi)
        .filterDate(
            inference_start,
            inference_start.advance(post_interval, "months"),
        )
        .aggregate_array("relativeOrbitNumber_start")
        .distinct()
    )

    def map_orbit(orbit):
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD_FLOAT")
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.eq("relativeOrbitNumber_start", orbit))
            .map(lee_filter)
            .select(["VV", "VH"])
            .map(lambda image: image.log())
            .filterBounds(aoi)
        )
        image = ttest(s1, inference_start, war_start, pre_interval, post_interval)
        return image

    urban = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterDate(
            war_start.advance(-1 * pre_interval, "months"),
            war_start,
        )
        .select("built")
        .mean()
    )

    orbit_images = ee.ImageCollection(orbits.map(map_orbit))
    # Max t-value across orbits; min p-value across orbits; max sample sizes
    t_max = orbit_images.select(["VV", "VH"]).max()
    p_min = orbit_images.select(["VV_pvalue", "VH_pvalue"]).min()
    n_pre = orbit_images.select("n_pre").max()
    n_post = orbit_images.select("n_post").max()
    image = t_max.addBands(p_min)

    # Combine polarizations: max t-value, min p-value
    max_change = image.select("VV").max(image.select("VH")).rename("max_change")
    p_value = image.select("VV_pvalue").min(image.select("VH_pvalue")).rename("p_value")
    image = max_change.addBands(p_value)

    # Bonferroni correction: multiply p-value by number of orbits, cap at 1
    n_orbits = orbits.size()
    p_value = p_value.multiply(n_orbits).min(ee.Image.constant(1)).rename("p_value")

    # Spatial smoothing applies only to t-values
    t_smooth = max_change.focalMedian(10, "gaussian", "meters")
    if clip:
        t_smooth = t_smooth.clip(aoi)
    t_smooth = t_smooth.updateMask(urban.gt(0.1))

    k50 = t_smooth.convolve(ee.Kernel.circle(50, "meters", True)).rename("k50")
    k100 = t_smooth.convolve(ee.Kernel.circle(100, "meters", True)).rename("k100")
    k150 = t_smooth.convolve(ee.Kernel.circle(150, "meters", True)).rename("k150")

    thr = ee.Number(float(damage_threshold))
    damage = t_smooth.gt(thr).rename("damage")
    T_statistic = (t_smooth.add(k50).add(k100).add(k150)).divide(4).rename("T_statistic")

    # Mask p-values with urban mask
    p_value = p_value.updateMask(urban.gt(0.1))
    if clip:
        p_value = p_value.clip(aoi)

    image = (
        T_statistic
        .addBands(damage)
        .addBands(p_value)
        .addBands(n_pre)
        .addBands(n_post)
        .toFloat()
    )
    if clip:
        image = image.clip(aoi)

    if export_grid and export_name:
        grid = aoi.geometry().bounds().coveringGrid("EPSG:3857", grid_scale)
        grid = image.reduceRegions(
            collection=grid,
            reducer=ee.Reducer.mean(),
            scale=10,
            tileScale=8,
        )
        task_grid = ee.batch.Export.table.toDrive(
            collection=grid,
            description=export_name + "_grid",
            folder=export_dir,
            fileFormat="CSV",
        )

    if viz:
        if viz_return_map:
            import geemap

            Map = geemap.Map()
            Map.add_basemap("SATELLITE")
            Map.addLayer(
                image.select("T_statistic"),
                {
                    "min": T_STATISTIC_VIZ_MIN,
                    "max": T_STATISTIC_VIZ_MAX,
                    "opacity": T_STATISTIC_VIZ_OPACITY,
                    "palette": ["yellow", "red", "purple"],
                },
                "T-test",
            )
            Map.centerObject(aoi)
            return Map
        open_geemap_preview(aoi, image)

    if footprints is not None:
        fc = ee.FeatureCollection(footprints).filterBounds(aoi)
        fp = image.reduceRegions(
            collection=fc,
            reducer=ee.Reducer.mean(),
            scale=10,
            tileScale=8,
        )
        task_fp = ee.batch.Export.table.toDrive(
            collection=fp,
            description=export_name,
            folder=export_dir,
            fileFormat="GEOJSON",
        )
        task_fp.start()

    if export and export_name:
        task = ee.batch.Export.image.toDrive(
            image=image,
            description=export_name,
            folder=export_dir,
            scale=export_scale,
            maxPixels=1e13,
        )
        task.start()
    return image


# Backward-compatible alias
filter_s1 = detect_damage
