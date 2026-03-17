# -*- coding: utf-8 -*-
"""CDSE OData search and download for Sentinel-1 GRD products."""

import json
import os
import re
import zipfile
import tempfile
import requests
from typing import List, Tuple, Optional
from urllib.parse import quote


CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
AUTH_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"


def get_token(username: str, password: str) -> str:
    """Get CDSE access token using password grant."""
    r = requests.post(
        AUTH_URL,
        data={
            "client_id": "cdse-public",
            "grant_type": "password",
            "username": username,
            "password": password,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    return json.loads(r.text)["access_token"]


def _wkt_to_odata_geom(wkt: str) -> str:
    """Convert WKT to OData geography literal for Intersects."""
    return "SRID=4326;" + wkt.replace(" ", "%20")


def search_s1_grd(
    access_token: str,
    aoi_wkt: str,
    start_date: str,
    end_date: str,
    max_results: int = 50,
) -> List[dict]:
    """Search for Sentinel-1 GRD products in AOI and date range. Returns list of product dicts (Id, Name, ...)."""
    geom = _wkt_to_odata_geom(aoi_wkt)
    start_odata = start_date.replace(" ", "T") + "T00:00:00.000Z" if "T" not in start_date else start_date
    end_odata = end_date.replace(" ", "T") + "T23:59:59.999Z" if "T" not in end_date else end_date
    filt = (
        f"Collection/Name eq 'SENTINEL-1' "
        f"and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq 'GRD') "
        f"and OData.CSC.Intersects(area=geography'{geom}') "
        f"and ContentDate/Start ge {start_odata} and ContentDate/Start le {end_odata}"
    )
    url = f"{CATALOGUE_URL}/Products?$filter={quote(filt)}&$top={max_results}"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60)
    r.raise_for_status()
    return r.json().get("value", [])


def download_product(access_token: str, product_id: str, product_name: str, out_dir: str) -> str:
    """
    Download full product (zip) and extract to out_dir. Returns path to extracted .SAFE directory.
    Product download is via $value on the product node.
    """
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, product_name + ".zip")
    if os.path.isfile(zip_path):
        # Already downloaded — skip
        safe_name = product_name + ".SAFE" if not product_name.endswith(".SAFE") else product_name
        extract_dir = os.path.join(out_dir, safe_name)
        if os.path.isdir(extract_dir):
            return extract_dir
    url = f"{CATALOGUE_URL}/Products('{product_id}')/$value"
    r = session.get(url, allow_redirects=False, timeout=30)
    while r.status_code in (301, 302, 303, 307):
        url = r.headers["Location"]
        r = session.get(url, allow_redirects=False, timeout=30)
    # Final request with streaming for large files
    r = session.get(url, stream=True, timeout=600)
    r.raise_for_status()
    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    safe_name = product_name + ".SAFE" if not product_name.endswith(".SAFE") else product_name
    extract_dir = os.path.join(out_dir, safe_name)
    if not os.path.isdir(extract_dir):
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(out_dir)
    return extract_dir


def find_vv_vh_in_safe(safe_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Find paths to VV and VH GeoTIFFs in a S1 GRD .SAFE directory. Returns (path_vv, path_vh)."""
    measurement = os.path.join(safe_dir, "measurement")
    if not os.path.isdir(measurement):
        return None, None
    vv_path = vh_path = None
    for f in os.listdir(measurement):
        if "-vv-" in f.lower() and f.endswith(".tif"):
            vv_path = os.path.join(measurement, f)
        if "-vh-" in f.lower() and f.endswith(".tif"):
            vh_path = os.path.join(measurement, f)
    return vv_path, vh_path
