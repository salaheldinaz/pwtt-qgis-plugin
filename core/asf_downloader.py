# -*- coding: utf-8 -*-
"""ASF / Earthdata: search and download Sentinel-1 IW GRD (hot archive)."""

import os
import time
import zipfile
from typing import List, Optional

_DOWNLOAD_RETRIES = 3
_RETRY_BACKOFF_S = 5


def authenticate_asf(username: str, password: str):
    """Return an ``ASFSession`` logged in with Earthdata credentials."""
    import asf_search as asf

    if not username or not password:
        raise ValueError("Earthdata username and password are required for ASF.")
    session = asf.ASFSession()
    session.auth_with_creds(username, password)
    return session


def _normalize_wkt_for_asf(aoi_wkt: str) -> str:
    """ASF expects WKT without SRID prefix; ensure POLYGON form."""
    w = aoi_wkt.strip()
    if w.upper().startswith("SRID="):
        w = w.split(";", 1)[-1].strip()
    return w


def search_s1_grd_asf(
    session,
    aoi_wkt: str,
    start_date: str,
    end_date: str,
    max_results: int = 50,
) -> List[dict]:
    """Search ASF for Sentinel-1 IW GRD-HD products intersecting AOI and dates.

    Returns dicts compatible with local_backend loaders:
    ``Id``, ``Name``, ``Online`` (True), ``DownloadUrl`` (first HTTPS URL), ``_asf_product`` (ASFProduct).
    """
    import asf_search as asf

    wkt = _normalize_wkt_for_asf(aoi_wkt)
    start = start_date[:10]
    end = end_date[:10]

    # Both S1A and S1B IW GRD-HD (matches CDSE IW_GRDH_1S use case)
    platform = getattr(asf.PLATFORM, "SENTINEL1", None)
    if platform is None:
        platform = [asf.PLATFORM.SENTINEL1A, asf.PLATFORM.SENTINEL1B]

    opts = asf.ASFSearchOptions(session=session)
    results = asf.geo_search(
        platform=platform,
        intersectsWith=wkt,
        beamSwath="IW",
        processingLevel=asf.PRODUCT_TYPE.GRD_HD,
        start=start,
        end=end,
        maxResults=max_results,
        opts=opts,
    )

    out: List[dict] = []
    for p in results:
        props = getattr(p, "properties", None) or {}
        name = props.get("fileName") or props.get("sceneName") or ""
        if not name:
            name = str(p)
        pid = props.get("granuleName") or props.get("sceneName") or name
        urls = []
        try:
            urls = list(p.get_urls() or [])
        except Exception:
            pass
        download_url = ""
        for u in urls:
            if u and str(u).lower().startswith("https"):
                download_url = str(u)
                break
        if not download_url and urls:
            download_url = str(urls[0])

        out.append(
            {
                "Id": pid,
                "Name": name.replace(".zip", "").replace(".SAFE", ""),
                "Online": True,
                "DownloadUrl": download_url,
                "ContentDate": {
                    "Start": str(props.get("startTime") or "")[:19].replace(" ", "T")
                },
                "_asf_product": p,
            }
        )

    def _sort_key(d):
        po = d.get("_asf_product")
        props = (getattr(po, "properties", None) or {}) if po is not None else {}
        return str(props.get("startTime", props.get("processingDate", "")))

    out.sort(key=_sort_key, reverse=True)
    return out


def download_product_asf(
    session,
    product: dict,
    out_dir: str,
) -> Optional[str]:
    """Download granule ZIP via ASFProduct.download, extract, return .SAFE directory path."""
    os.makedirs(out_dir, exist_ok=True)
    pobj = product.get("_asf_product")
    if pobj is None:
        return None

    props = getattr(pobj, "properties", None) or {}
    granule = props.get("granuleName") or props.get("fileName") or product.get("Name") or ""
    safe_stem = granule.replace(".zip", "").replace(".SAFE", "")
    extract_dir = os.path.join(out_dir, safe_stem + ".SAFE")
    if os.path.isdir(extract_dir):
        return extract_dir

    before = set(os.listdir(out_dir))
    last_err = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            pobj.download(path=out_dir, session=session)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < _DOWNLOAD_RETRIES:
                time.sleep(_RETRY_BACKOFF_S * attempt)
    if last_err is not None:
        raise RuntimeError(
            f"ASF download failed after {_DOWNLOAD_RETRIES} attempts: {last_err}"
        ) from last_err
    after = set(os.listdir(out_dir))
    new_files = sorted(after - before, key=lambda fn: os.path.getmtime(os.path.join(out_dir, fn)), reverse=True)

    zip_path = None
    for fn in new_files:
        if fn.endswith(".zip"):
            zip_path = os.path.join(out_dir, fn)
            break
    if zip_path is None:
        for fn in os.listdir(out_dir):
            if fn.endswith(".zip") and safe_stem and safe_stem[:20] in fn:
                zip_path = os.path.join(out_dir, fn)
                break

    if zip_path is None or not os.path.isfile(zip_path):
        return None

    if not os.path.isdir(extract_dir):
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(out_dir)

    if os.path.isdir(extract_dir):
        return extract_dir
    base_no_ext = zip_path[:-4]
    if os.path.isdir(base_no_ext):
        return base_no_ext
    # Top-level folder inside archive
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        if names:
            root = names[0].split("/")[0]
            candidate = os.path.join(out_dir, root)
            if os.path.isdir(candidate):
                return candidate
    return None
