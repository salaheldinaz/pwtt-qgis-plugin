# -*- coding: utf-8 -*-
"""Bundled GEE PWTT logic (lee_filter, ttest, filter_s1) so the plugin works when installed from ZIP."""

import ee


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
    pre_n = ee.Number(pre.aggregate_array("orbitNumber_start").distinct().size())
    post_mean = post.mean()
    post_sd = post.reduce(ee.Reducer.stdDev())
    post_n = ee.Number(post.aggregate_array("orbitNumber_start").distinct().size())
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
    return change


def filter_s1(
    aoi,
    inference_start,
    war_start,
    pre_interval=12,
    post_interval=2,
    footprints=None,
    viz=False,
    export=False,
    export_dir="PWTT_Export",
    export_name=None,
    export_scale=10,
    grid_scale=500,
    export_grid=False,
):
    inference_start = ee.Date(inference_start)
    war_start = ee.Date(war_start)

    orbits = (
        ee.ImageCollection("COPERNICUS/S1_GRD_FLOAT")
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filterBounds(aoi)
        .filter(ee.Filter.contains(".geo", ee.FeatureCollection(aoi).geometry()))
        .filterDate(
            ee.Date(inference_start),
            ee.Date(inference_start).advance(post_interval, "months"),
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

    image = ee.ImageCollection(orbits.map(map_orbit)).max()
    image = image.addBands(
        image.select("VV").max(image.select("VH")).rename("max_change")
    ).select("max_change")
    image = image.focalMedian(10, "gaussian", "meters").clip(aoi).updateMask(urban.gt(0.1))

    k50 = image.convolve(ee.Kernel.circle(50, "meters", True)).rename("k50")
    k100 = image.convolve(ee.Kernel.circle(100, "meters", True)).rename("k100")
    k150 = image.convolve(ee.Kernel.circle(150, "meters", True)).rename("k150")
    damage = image.select("max_change").gt(3).rename("damage")
    image = image.addBands(damage)
    image = image.addBands([k50, k100, k150])
    image = image.addBands(
        (
            image.select("max_change")
            .add(image.select("k50"))
            .add(image.select("k100"))
            .add(image.select("k150"))
            .divide(4)
        ).rename("T_statistic")
    )
    image = image.select("T_statistic", "damage").toFloat()
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
        import geemap
        Map = geemap.Map()
        Map.add_basemap("SATELLITE")
        Map.addLayer(
            image.select("T_statistic"),
            {"min": 3, "max": 5, "opacity": 0.5, "palette": ["yellow", "red", "purple"]},
            "T-test",
        )
        Map.centerObject(aoi)
        return Map

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
