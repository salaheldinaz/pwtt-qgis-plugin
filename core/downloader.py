# -*- coding: utf-8 -*-
"""CDSE OData search and download for Sentinel-1 GRD products."""

import json
import os
import re
import time
import zipfile
import requests
from typing import Callable, List, Optional, Tuple
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
    log: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Search for Sentinel-1 IW GRDH products. Returns list sorted with online products first."""
    if log:
        log(
            "CDSE API: OData GET catalogue.dataspace.copernicus.eu/odata/v1/Products "
            f"(SENTINEL-1 IW_GRDH_1S, AOI intersects, ContentDate "
            f"{start_date[:10]} … {end_date[:10]}, top={max_results})"
        )
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
    if log:
        online_n = sum(1 for p in products if p.get("Online", True))
        log(
            f"CDSE API: response {len(products)} product(s) "
            f"({online_n} marked online, {len(products) - online_n} offline)"
        )
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
    log: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Download full product (zip) and extract to out_dir. Returns path to extracted .SAFE directory.
    Handles offline products: triggers order and polls, or returns None if wait_for_offline=False
    and product is not available.
    """
    os.makedirs(out_dir, exist_ok=True)
    safe_name = product_name + ".SAFE" if not product_name.endswith(".SAFE") else product_name
    extract_dir = os.path.join(out_dir, safe_name)
    if os.path.isdir(extract_dir):
        if log:
            log(f"CDSE: reuse cached SAFE (no download) — {extract_dir}")
        return extract_dir

    zip_path = os.path.join(out_dir, product_name + ".zip")
    if os.path.isfile(zip_path):
        if not os.path.isdir(extract_dir):
            if log:
                log(f"CDSE: extracting existing zip → {out_dir}")
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(out_dir)
        if os.path.isdir(extract_dir):
            if log:
                log(f"CDSE: SAFE ready at {extract_dir}")
            return extract_dir

    if log:
        log(
            f"CDSE download: product «{product_name}» id={product_id} "
            f"→ GET download.dataspace.copernicus.eu/odata/v1/Products('$value')"
        )
        log(f"CDSE: zip target {zip_path}")

    with requests.Session() as session:
        session.headers["Authorization"] = f"Bearer {access_token}"
        url = f"{DOWNLOAD_URL}/Products('{product_id}')/$value"

        # Follow redirects manually to handle auth on each hop
        r = session.get(url, allow_redirects=False, timeout=60)
        while r.status_code in (301, 302, 303, 307):
            loc = r.headers.get("Location", "")
            if log and loc:
                log(f"CDSE: redirect HTTP {r.status_code} → {loc[:120]}{'…' if len(loc) > 120 else ''}")
            url = r.headers["Location"]
            r = session.get(url, allow_redirects=False, timeout=60)

        # 202 = order accepted (product being staged from cold storage)
        # 422 = product offline, not yet ordered or still staging
        if r.status_code in (202, 422):
            if log:
                log(
                    f"CDSE: HTTP {r.status_code} (offline / staging) for «{product_name}» "
                    f"— triggering order & catalogue poll"
                )
            # Always trigger the order so CDSE starts staging it for future attempts
            _trigger_order(session, product_id)
            if not wait_for_offline:
                return None
            # Poll until online
            waited = 0
            if log:
                log(
                    f"CDSE: polling Products metadata every {ORDER_POLL_INTERVAL_S}s "
                    f"(max {MAX_ORDER_WAIT_S}s)…"
                )
            while waited < MAX_ORDER_WAIT_S:
                time.sleep(ORDER_POLL_INTERVAL_S)
                waited += ORDER_POLL_INTERVAL_S
                if _is_product_online(access_token, product_id):
                    if log:
                        log(f"CDSE: product online after ~{waited}s — retrying download")
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

        # Re-request as a stream (the redirect-following request above wasn't streamed)
        r.close()
        if log:
            log(f"CDSE: streaming body to {zip_path} …")
        r = session.get(url, stream=True, timeout=(30, 600))
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

    safe_name = product_name + ".SAFE" if not product_name.endswith(".SAFE") else product_name
    extract_dir = os.path.join(out_dir, safe_name)
    if not os.path.isdir(extract_dir):
        if log:
            log(f"CDSE: extracting zip → {out_dir}")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(out_dir)
    if log:
        log(f"CDSE: SAFE ready at {extract_dir}")
    return extract_dir


def remove_product_zip(product_name: str, out_dir: str, log: Optional[Callable[[str], None]] = None) -> None:
    """Delete the product .zip under out_dir after SAFE has been used (saves disk)."""
    zip_path = os.path.join(out_dir, product_name + ".zip")
    if not os.path.isfile(zip_path):
        return
    try:
        os.remove(zip_path)
        if log:
            log(f"CDSE: removed archive to save space — {os.path.basename(zip_path)}")
    except OSError:
        pass


def _is_s1_grd_geotiff(filename: str) -> bool:
    """True for ESA-style GRD measurement rasters (.tif or .tiff, any common case)."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in (".tif", ".tiff")


def find_vv_vh_in_safe(safe_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Find paths to VV and VH GeoTIFFs in a S1 GRD .SAFE directory. Returns (path_vv, path_vh)."""
    if not os.path.isdir(safe_dir):
        return None, None
    measurement = None
    for name in os.listdir(safe_dir):
        if name.lower() == "measurement":
            candidate = os.path.join(safe_dir, name)
            if os.path.isdir(candidate):
                measurement = candidate
                break
    if measurement is None:
        return None, None
    vv_path = vh_path = None
    for f in os.listdir(measurement):
        if not _is_s1_grd_geotiff(f):
            continue
        fl = f.lower()
        if "-vv-" in fl:
            vv_path = os.path.join(measurement, f)
        if "-vh-" in fl:
            vh_path = os.path.join(measurement, f)
    return vv_path, vh_path
