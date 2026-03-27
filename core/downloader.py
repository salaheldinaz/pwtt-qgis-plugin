# -*- coding: utf-8 -*-
"""CDSE OData search and download for Sentinel-1 GRD products."""

import json
import os
import re
import time
import zipfile
import requests
from typing import List, Tuple, Optional
from urllib.parse import quote


CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1"
AUTH_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

MAX_ORDER_WAIT_S = 600
ORDER_POLL_INTERVAL_S = 30


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
    """Convert WKT to OData geography literal for Intersects.
    QGIS outputs 'Polygon ((...))' but CDSE OData requires 'POLYGON((...))' (uppercase, no space before parens)."""
    normalized = wkt.strip()
    normalized = re.sub(
        r'(?i)(polygon|multipolygon|point|linestring)\s*\(',
        lambda m: m.group(1).upper() + '(',
        normalized,
    )
    return "SRID=4326;" + normalized


def search_s1_grd(
    access_token: str,
    aoi_wkt: str,
    start_date: str,
    end_date: str,
    max_results: int = 50,
) -> List[dict]:
    """Search for Sentinel-1 IW GRDH products. Returns list sorted with online products first."""
    geom = _wkt_to_odata_geom(aoi_wkt)
    start_odata = start_date.replace(" ", "T") + "T00:00:00.000Z" if "T" not in start_date else start_date
    end_odata = end_date.replace(" ", "T") + "T23:59:59.999Z" if "T" not in end_date else end_date
    filt = (
        f"Collection/Name eq 'SENTINEL-1' "
        f"and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq 'IW_GRDH_1S') "
        f"and OData.CSC.Intersects(area=geography'{geom}') "
        f"and ContentDate/Start gt {start_odata} and ContentDate/Start lt {end_odata}"
    )
    url = f"{CATALOGUE_URL}/Products?$filter={quote(filt)}&$top={max_results}&$orderby=ContentDate/Start desc"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=60)
    r.raise_for_status()
    products = r.json().get("value", [])
    # Sort online products first so we prefer immediately available data
    products.sort(key=lambda p: (not p.get("Online", True),))
    return products


def _is_product_online(access_token: str, product_id: str) -> bool:
    """Check if a product is online via catalogue metadata."""
    url = f"{CATALOGUE_URL}/Products('{product_id}')"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        r.raise_for_status()
        return r.json().get("Online", False)
    except Exception:
        return False


def _trigger_order(session: requests.Session, product_id: str) -> bool:
    """Trigger an order for an offline product by requesting $value (CDSE stages it).
    Returns True if order was accepted (202) or product became available."""
    url = f"{DOWNLOAD_URL}/Products('{product_id}')/$value"
    try:
        r = session.get(url, allow_redirects=False, timeout=30)
        return r.status_code in (200, 202, 301, 302, 303, 307)
    except Exception:
        return False


def download_product(
    access_token: str,
    product_id: str,
    product_name: str,
    out_dir: str,
    wait_for_offline: bool = True,
) -> Optional[str]:
    """
    Download full product (zip) and extract to out_dir. Returns path to extracted .SAFE directory.
    Handles offline products: triggers order and polls, or returns None if wait_for_offline=False
    and product is not available.
    """
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, product_name + ".zip")
    if os.path.isfile(zip_path):
        safe_name = product_name + ".SAFE" if not product_name.endswith(".SAFE") else product_name
        extract_dir = os.path.join(out_dir, safe_name)
        if os.path.isdir(extract_dir):
            return extract_dir

    url = f"{DOWNLOAD_URL}/Products('{product_id}')/$value"

    # Follow redirects manually to handle auth on each hop
    r = session.get(url, allow_redirects=False, timeout=60)
    while r.status_code in (301, 302, 303, 307):
        url = r.headers["Location"]
        r = session.get(url, allow_redirects=False, timeout=60)

    # 202 = order accepted (product being staged from cold storage)
    # 422 = product offline, not yet ordered or still staging
    if r.status_code in (202, 422):
        if not wait_for_offline:
            return None
        # Trigger the order and poll until online
        _trigger_order(session, product_id)
        waited = 0
        while waited < MAX_ORDER_WAIT_S:
            time.sleep(ORDER_POLL_INTERVAL_S)
            waited += ORDER_POLL_INTERVAL_S
            if _is_product_online(access_token, product_id):
                break
        else:
            raise RuntimeError(
                f"Product '{product_name}' is offline and did not become available "
                f"after {MAX_ORDER_WAIT_S}s. Try again later or use a different date range."
            )
        # Re-request after product is online
        url = f"{DOWNLOAD_URL}/Products('{product_id}')/$value"
        r = session.get(url, allow_redirects=False, timeout=60)
        while r.status_code in (301, 302, 303, 307):
            url = r.headers["Location"]
            r = session.get(url, allow_redirects=False, timeout=60)

    # Stream download
    if not r.ok:
        r = session.get(url, stream=True, timeout=600)
    else:
        # Re-request as stream (first request wasn't streamed)
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
