# -*- coding: utf-8 -*-
"""ASF / Earthdata: search and download Sentinel-1 IW GRD (hot archive)."""

import os
import time
import zipfile
from typing import Callable, List, Optional
from urllib.parse import urlparse

_DOWNLOAD_RETRIES = 3
_RETRY_BACKOFF_S = 5


def _asf_product_cache_dir(parent_cache: str, safe_stem: str, product_id: str) -> str:
    """Isolated folder per granule so concurrent Pre/Post downloads do not steal each other's ZIPs."""
    key = "".join(c if c.isalnum() else "_" for c in (safe_stem or product_id or "granule"))[:120]
    if not key.strip("_"):
        key = "granule"
    d = os.path.join(parent_cache, "asf_" + key)
    os.makedirs(d, exist_ok=True)
    return d


def _purge_non_zips(product_dir: str, log: Optional[Callable[[str], None]]) -> None:
    """Remove .zip paths that are not valid archives (partial download, HTML error body, etc.)."""
    try:
        names = os.listdir(product_dir)
    except OSError:
        return
    for fn in names:
        if not fn.endswith(".zip"):
            continue
        path = os.path.join(product_dir, fn)
        if os.path.isfile(path) and not zipfile.is_zipfile(path):
            if log:
                log(f"ASF: removing invalid/corrupt archive (not a zip) — {fn}")
            try:
                os.remove(path)
            except OSError:
                pass


def authenticate_asf(username: str, password: str):
    """Return an ``ASFSession`` logged in with Earthdata credentials."""
    from . import deps

    with deps.deps_priority():
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
    log: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Search ASF for Sentinel-1 IW GRD-HD products intersecting AOI and dates.

    Returns dicts compatible with local_backend loaders:
    ``Id``, ``Name``, ``Online`` (True), ``DownloadUrl`` (first HTTPS URL), ``_asf_product`` (ASFProduct).
    """
    from . import deps

    with deps.deps_priority():
        import asf_search as asf

    wkt = _normalize_wkt_for_asf(aoi_wkt)
    start = start_date[:10]
    end = end_date[:10]

    if log:
        log(
            "ASF API: asf_search.geo_search (IW GRD_HD, "
            f"{start} … {end}, maxResults={max_results})"
        )

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
    if log:
        log(f"ASF API: geo_search returned {len(out)} granule(s)")
    return out


def download_product_asf(
    session,
    product: dict,
    out_dir: str,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Download granule ZIP via ASFProduct.download, extract, return .SAFE directory path."""
    os.makedirs(out_dir, exist_ok=True)
    pobj = product.get("_asf_product")
    if pobj is None:
        return None

    props = getattr(pobj, "properties", None) or {}
    granule = props.get("granuleName") or props.get("fileName") or product.get("Name") or ""
    safe_stem = granule.replace(".zip", "").replace(".SAFE", "")
    pid = product.get("Id") or safe_stem or "granule"
    product_dir = _asf_product_cache_dir(out_dir, safe_stem, pid)
    extract_dir = os.path.join(product_dir, safe_stem + ".SAFE")
    # Older PWTT used a flat cache; reuse if still present.
    legacy_extract = os.path.join(out_dir, safe_stem + ".SAFE")
    for candidate in (extract_dir, legacy_extract):
        if os.path.isdir(candidate):
            if log:
                log(f"ASF: reuse cached SAFE — {candidate}")
            return candidate

    du = (product.get("DownloadUrl") or "").strip()
    if log:
        log(
            f"ASF download: granule «{safe_stem}» → ASFProduct.download "
            f"(product_dir={product_dir})"
        )
        if du:
            host = urlparse(du).netloc or "ASF"
            log(f"ASF: primary HTTPS host (from metadata): {host}")

    _purge_non_zips(product_dir, log)

    last_err = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            if log:
                log(f"ASF: download attempt {attempt}/{_DOWNLOAD_RETRIES} …")
            pobj.download(path=product_dir, session=session)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if log:
                log(f"ASF: attempt {attempt} error: {e}")
            if attempt < _DOWNLOAD_RETRIES:
                time.sleep(_RETRY_BACKOFF_S * attempt)
    if last_err is not None:
        raise RuntimeError(
            f"ASF download failed after {_DOWNLOAD_RETRIES} attempts: {last_err}"
        ) from last_err

    _purge_non_zips(product_dir, log)

    zip_names = sorted(
        f for f in os.listdir(product_dir) if f.endswith(".zip")
    )
    # Prefer the zip whose name matches this granule (not a stray file).
    zip_path = None
    if safe_stem:
        preferred = [f for f in zip_names if safe_stem[:20] in f or f.startswith(safe_stem[:16])]
        if len(preferred) == 1:
            zip_path = os.path.join(product_dir, preferred[0])
        elif len(preferred) > 1:
            preferred.sort(key=lambda fn: os.path.getmtime(os.path.join(product_dir, fn)), reverse=True)
            zip_path = os.path.join(product_dir, preferred[0])
    if zip_path is None and zip_names:
        zip_names_full = sorted(
            zip_names,
            key=lambda fn: os.path.getmtime(os.path.join(product_dir, fn)),
            reverse=True,
        )
        zip_path = os.path.join(product_dir, zip_names_full[0])

    if zip_path is None or not os.path.isfile(zip_path):
        return None

    if not zipfile.is_zipfile(zip_path):
        if log:
            log(f"ASF: archive failed zip validation — {os.path.basename(zip_path)}")
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return None

    if log:
        log(f"ASF: zip file {zip_path}")

    if not os.path.isdir(extract_dir):
        if log:
            log(f"ASF: extracting {os.path.basename(zip_path)} → {product_dir}")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(product_dir)

    if os.path.isdir(extract_dir):
        if log:
            log(f"ASF: SAFE directory {extract_dir}")
        return extract_dir
    base_no_ext = zip_path[:-4]
    if os.path.isdir(base_no_ext):
        if log:
            log(f"ASF: using extracted folder {base_no_ext}")
        return base_no_ext
    # Top-level folder inside archive
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        if names:
            root = names[0].split("/")[0]
            candidate = os.path.join(product_dir, root)
            if os.path.isdir(candidate):
                if log:
                    log(f"ASF: SAFE root from archive {candidate}")
                return candidate
    return None
