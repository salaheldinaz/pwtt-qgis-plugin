# -*- coding: utf-8 -*-
"""Microsoft Planetary Computer: STAC search + signed download for Sentinel-1 GRD IW."""

import os
from typing import List, Optional, Tuple

import requests

from .utils import wkt_to_bbox


def authenticate_pc(subscription_key: Optional[str] = None):
    """Return a signed pystac-client ``Client`` for Planetary Computer STAC API."""
    from . import deps

    with deps.deps_priority():
        import planetary_computer
        import pystac_client

    if subscription_key and str(subscription_key).strip():
        planetary_computer.set_subscription_key(str(subscription_key).strip())

    def _sign(item):
        return planetary_computer.sign_inplace(item)

    return pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=_sign,
    )


def search_s1_grd_pc(
    catalog,
    aoi_wkt: str,
    start_date: str,
    end_date: str,
    max_results: int = 50,
) -> List[dict]:
    """STAC search ``sentinel-1-grd`` IW over AOI and time range.

    Returns dicts with ``Id``, ``Name``, ``Online``, ``_pc_item`` (pystac Item).
    """
    bbox = wkt_to_bbox(aoi_wkt)
    if not bbox:
        raise ValueError("Invalid AOI WKT for Planetary Computer search.")
    west, south, east, north = bbox
    start = start_date[:10]
    end = end_date[:10]
    dt = f"{start}/{end}"

    # Fetch slightly over limit then keep IW only (avoids STAC query dialect mismatches).
    search = catalog.search(
        collections=["sentinel-1-grd"],
        bbox=[west, south, east, north],
        datetime=dt,
        limit=min(500, max(50, max_results * 4)),
    )
    items = []
    for it in search.items():
        mode = str(
            it.properties.get("sar:instrument_mode")
            or it.properties.get("s1:instrument_mode")
            or ""
        ).upper()
        if mode == "IW":
            items.append(it)
        if len(items) >= max_results:
            break

    def _acq_time(it):
        return str(it.datetime or it.properties.get("start_datetime", ""))

    items.sort(key=_acq_time, reverse=True)

    out: List[dict] = []
    for it in items:
        pid = it.id
        name = it.properties.get("title") or pid
        out.append(
            {
                "Id": pid,
                "Name": name,
                "Online": True,
                "ContentDate": {"Start": _acq_time(it)},
                "_pc_item": it,
            }
        )
    return out


def _vv_vh_hrefs(item) -> Tuple[Optional[str], Optional[str]]:
    """Resolve signed hrefs for VV and VH assets (PC naming varies slightly)."""
    vv_h = vh_h = None
    for key, asset in item.assets.items():
        kl = key.lower()
        href = asset.href
        if not href:
            continue
        if kl in ("vv", "vv-cog", "gamma0_vv", "measurement-vv"):
            vv_h = href
        elif kl in ("vh", "vh-cog", "gamma0_vh", "measurement-vh"):
            vh_h = href
    if vv_h is None or vh_h is None:
        for key, asset in item.assets.items():
            kl = key.lower()
            href = asset.href
            if not href:
                continue
            if vv_h is None and ("-vv-" in kl or kl.endswith("_vv") or kl == "vv"):
                vv_h = href
            if vh_h is None and ("-vh-" in kl or kl.endswith("_vh") or kl == "vh"):
                vh_h = href
    return vv_h, vh_h


def download_pc_vv_vh(product: dict, out_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Download VV/VH GeoTIFFs for a STAC item into *out_dir*. Returns (vv_path, vh_path)."""
    item = product.get("_pc_item")
    if item is None:
        return None, None
    os.makedirs(out_dir, exist_ok=True)
    stem = product.get("Id") or product.get("Name") or "granule"
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in str(stem))[:200]
    vv_h, vh_h = _vv_vh_hrefs(item)
    if not vv_h or not vh_h:
        return None, None

    vv_path = os.path.join(out_dir, f"{safe}_vv.tif")
    vh_path = os.path.join(out_dir, f"{safe}_vh.tif")

    if os.path.isfile(vv_path) and os.path.isfile(vh_path):
        return vv_path, vh_path

    def _stream_download(session, url, dest):
        """Download *url* to *dest*, removing partial file on failure."""
        try:
            r = session.get(url, stream=True, timeout=(30, 600))
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        except Exception:
            if os.path.isfile(dest):
                os.remove(dest)
            raise

    with requests.Session() as s:
        _stream_download(s, vv_h, vv_path)
        _stream_download(s, vh_h, vh_path)

    return vv_path, vh_path
