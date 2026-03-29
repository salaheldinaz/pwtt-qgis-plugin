# -*- coding: utf-8 -*-
"""Building footprints (OSM or user vector) + rasterstats zonal stats, output GeoPackage."""

import os
import json
import tempfile
import time
import requests
from typing import Optional

from .utils import wkt_to_bbox

_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def _run_overpass_query(query: str, limit: int = 50000) -> str:
    """Execute an Overpass QL query against known mirrors. Returns path to temp GeoJSON.

    Retries up to 3 times with exponential back-off and falls back to
    alternative Overpass mirrors when the primary endpoint times out.
    """
    last_err = None
    for endpoint in _OVERPASS_ENDPOINTS:
        for attempt in range(3):
            try:
                r = requests.post(endpoint, data={"data": query}, timeout=180)
                if r.status_code == 429:
                    time.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                features = []
                for el in data.get("elements", []):
                    if el.get("type") != "way" or "geometry" not in el:
                        continue
                    coords = [[c["lon"], c["lat"]] for c in el["geometry"]]
                    if len(coords) < 3:
                        continue
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [coords]},
                        "properties": {"id": el.get("id", "")},
                    })
                    if len(features) >= limit:
                        break
                geojson = {"type": "FeatureCollection", "features": features}
                fd, path = tempfile.mkstemp(suffix=".geojson")
                os.close(fd)
                with open(path, "w") as f:
                    json.dump(geojson, f)
                return path
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                time.sleep(5 * (attempt + 1))
                continue
            except requests.exceptions.HTTPError as e:
                last_err = e
                if e.response is not None and e.response.status_code in (502, 503, 504):
                    time.sleep(10 * (attempt + 1))
                    continue
                break
    raise RuntimeError(
        f"All Overpass API endpoints failed after retries.\n"
        f"Last error: {last_err}\n"
        f"The server may be overloaded — try again in a few minutes, "
        f"or provide your own building footprints vector file."
    )


def _fetch_osm_buildings(west: float, south: float, east: float, north: float, limit: int = 50000) -> Optional[str]:
    """Query Overpass API for current building polygons in bbox. Returns path to temp GeoJSON."""
    query = f"""
    [out:json][timeout:180];
    (
      way["building"]({south},{west},{north},{east});
    );
    out body geom;
    """
    return _run_overpass_query(query, limit)


def _fetch_historical_osm_buildings(
    west: float, south: float, east: float, north: float,
    date_iso: str, limit: int = 50000,
) -> str:
    """Query Overpass API for building polygons as they existed on date_iso (YYYY-MM-DD).

    Uses the Overpass [date:"..."] filter to retrieve a historical snapshot of OSM.
    Returns path to temp GeoJSON.
    """
    query = f"""
    [out:json][timeout:180][date:"{date_iso}T00:00:00Z"];
    (
      way["building"]({south},{west},{north},{east});
    );
    out body geom;
    """
    return _run_overpass_query(query, limit)


def compute_footprints(
    raster_path: str,
    aoi_wkt: str,
    output_gpkg_path: str,
    footprints_vector_path: Optional[str] = None,
    date_iso: Optional[str] = None,
    progress_callback=None,
) -> str:
    """
    Compute per-building mean T-statistic from the PWTT raster. Write GeoPackage.

    If footprints_vector_path is set, use that vector (GeoJSON/GPKG) clipped to AOI.
    Otherwise fetch buildings from OSM (Overpass) for the AOI bbox.
    If date_iso (YYYY-MM-DD) is provided, fetch the historical OSM snapshot for that date
    instead of the current OSM data.
    """
    try:
        import geopandas as gpd
    except ImportError as e:
        raise RuntimeError(
            "Building footprints require geopandas and rasterstats: pip install geopandas rasterstats"
        ) from e

    # The real 'rasterstats' package may be shadowed by a QGIS plugin of the
    # same name.  Use deps._rasterstats_probe which handles the shadow.
    zonal_stats = None
    try:
        from rasterstats import zonal_stats
    except ImportError:
        pass

    if zonal_stats is None or not callable(zonal_stats):
        import importlib, sys
        from .deps import (
            _deps_dir, _find_real_rasterstats_dir,
            _purge_rasterstats_modules, ensure_on_path,
        )
        ensure_on_path()
        real_dir = _find_real_rasterstats_dir()
        extra_dirs = [d for d in [real_dir, _deps_dir()] if d and os.path.isdir(d)]
        if not extra_dirs:
            raise RuntimeError(
                "rasterstats is not installed.  Use the Install Dependencies button or run:\n"
                "  pip install rasterstats"
            )
        _saved = sys.path[:]
        _saved_mods = {k: sys.modules[k] for k in list(sys.modules)
                       if k == "rasterstats" or k.startswith("rasterstats.")}
        loaded = False
        try:
            for d in extra_dirs:
                _purge_rasterstats_modules()
                importlib.invalidate_caches()
                sys.path[:] = [d] + [p for p in _saved if p != d]
                try:
                    from rasterstats import zonal_stats
                    if callable(zonal_stats):
                        loaded = True
                        break
                except ImportError:
                    continue
        finally:
            sys.path[:] = _saved
            if not loaded:
                _purge_rasterstats_modules()
                sys.modules.update(_saved_mods)
        if not loaded:
            raise RuntimeError(
                "rasterstats is not installed or is shadowed by a QGIS plugin.\n"
                "Use the Install Dependencies button or run:\n"
                "  pip install rasterstats"
            )

    bbox = wkt_to_bbox(aoi_wkt)
    if not bbox:
        raise ValueError("Invalid AOI WKT")
    west, south, east, north = bbox

    if progress_callback:
        progress_callback(0, "Loading building footprints…")
    if footprints_vector_path and os.path.isfile(footprints_vector_path):
        gdf = gpd.read_file(footprints_vector_path, bbox=(west, south, east, north))
    elif date_iso:
        if progress_callback:
            progress_callback(0, f"Fetching historical OSM buildings ({date_iso})…")
        geojson_path = _fetch_historical_osm_buildings(west, south, east, north, date_iso)
        if not geojson_path:
            raise RuntimeError(f"Could not fetch historical building footprints for {date_iso} (Overpass failed).")
        gdf = gpd.read_file(geojson_path)
        try:
            os.remove(geojson_path)
        except OSError:
            pass
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        gdf = gdf.to_crs("EPSG:4326")
    else:
        geojson_path = _fetch_osm_buildings(west, south, east, north)
        if not geojson_path:
            raise RuntimeError("Could not fetch building footprints (Overpass failed).")
        gdf = gpd.read_file(geojson_path)
        try:
            os.remove(geojson_path)
        except OSError:
            pass
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        gdf = gdf.to_crs("EPSG:4326")

    if gdf.empty or len(gdf) == 0:
        raise RuntimeError("No building footprints in the AOI.")

    import rasterio
    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
    if gdf.crs != raster_crs:
        gdf = gdf.to_crs(raster_crs)

    if progress_callback:
        progress_callback(30, "Computing zonal statistics…")
    stats = zonal_stats(gdf, raster_path, stats=["mean"], nodata=-9999, band=1)
    gdf["T_statistic"] = [s["mean"] if s and s.get("mean") is not None else float("nan") for s in stats]

    if progress_callback:
        progress_callback(80, "Writing GeoPackage…")
    os.makedirs(os.path.dirname(output_gpkg_path) or ".", exist_ok=True)
    gdf.to_file(output_gpkg_path, driver="GPKG")
    if progress_callback:
        progress_callback(100, "Done.")
    return output_gpkg_path
