# -*- coding: utf-8 -*-
"""Microsoft Planetary Computer: STAC search + signed download for Sentinel-1 GRD IW."""

import os
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

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
    log: Optional[Callable[[str], None]] = None,
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

    if log:
        log(
            "PC API: STAC search planetarycomputer.microsoft.com/api/stac/v1 "
            f"(collection=sentinel-1-grd, bbox=[{west:.4f},{south:.4f},{east:.4f},{north:.4f}], "
            f"datetime={dt})"
        )

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
    if log:
        log(f"PC API: STAC returned {len(out)} IW scene(s) (after filter)")
    return out


def _asset_href_ci(item, key_lower: str) -> Optional[str]:
    """Return href for first asset whose key matches *key_lower* (case-insensitive)."""
    for k, asset in item.assets.items():
        if k.lower() == key_lower and asset.href:
            return asset.href
    return None


def _vv_vh_href_pairs(item) -> List[Tuple[str, str]]:
    """Ordered (VV URL, VH URL) pairs — prefer plain ``vv``/``vh`` before ``*-cog`` (often ZSTD-only in GDAL).

    Planetary Computer may expose several assets; iteration order used to pick the last match,
    which tended to be COG/ZSTD. GDAL in some QGIS builds lacks ZSTD — non-COG keys may use Deflate.
    """
    # (sort_priority, vv_key, vh_key) — lower priority tried first
    key_pairs = [
        (0, "vv", "vh"),
        (1, "gamma0_vv", "gamma0_vh"),
        (2, "measurement-vv", "measurement-vh"),
        (5, "vv-cog", "vh-cog"),
    ]
    out: List[Tuple[str, str]] = []
    seen = set()
    for _pri, vk, hk in key_pairs:
        vv = _asset_href_ci(item, vk.lower())
        vh = _asset_href_ci(item, hk.lower())
        if vv and vh:
            sig = (vv, vh)
            if sig not in seen:
                seen.add(sig)
                out.append(sig)
    if out:
        return out

    # Legacy single-pass (one vv + one vh href, best-effort)
    vv_h, vh_h = _vv_vh_hrefs_legacy(item)
    if vv_h and vh_h:
        return [(vv_h, vh_h)]
    return []


def _vv_vh_hrefs_legacy(item) -> Tuple[Optional[str], Optional[str]]:
    """Single VV/VH href pair — prefer lowest-rank asset key, not last dict iteration."""
    best_vv = (99, None)
    best_vh = (99, None)

    def vv_rank(kl: str) -> int:
        if kl == "vv":
            return 0
        if kl == "gamma0_vv":
            return 1
        if kl == "measurement-vv":
            return 2
        if kl == "vv-cog":
            return 10
        if "-vv-" in kl or kl.endswith("_vv"):
            return 5
        return 99

    def vh_rank(kl: str) -> int:
        if kl == "vh":
            return 0
        if kl == "gamma0_vh":
            return 1
        if kl == "measurement-vh":
            return 2
        if kl == "vh-cog":
            return 10
        if "-vh-" in kl or kl.endswith("_vh"):
            return 5
        return 99

    for key, asset in item.assets.items():
        kl = key.lower()
        href = asset.href
        if not href:
            continue
        rv, rh = vv_rank(kl), vh_rank(kl)
        if rv < 99 and rv < best_vv[0]:
            best_vv = (rv, href)
        if rh < 99 and rh < best_vh[0]:
            best_vh = (rh, href)

    vv_h, vh_h = best_vv[1], best_vh[1]
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


def _vv_vh_hrefs(item) -> Tuple[Optional[str], Optional[str]]:
    """First-choice VV/VH hrefs (highest-priority pair)."""
    pairs = _vv_vh_href_pairs(item)
    if not pairs:
        return None, None
    return pairs[0][0], pairs[0][1]


def _probe_pc_geotiff_pair(vv_path: str, vh_path: str) -> Optional[BaseException]:
    """Return ``None`` if GDAL can read a small window from both files; else first exception."""
    try:
        from . import deps

        with deps.deps_priority():
            import rasterio
    except Exception as e:
        return e
    try:
        for path in (vv_path, vh_path):
            with rasterio.open(path) as s:
                win = rasterio.windows.Window(
                    0, 0, min(8, s.width), min(8, s.height)
                )
                s.read(1, window=win)
        return None
    except Exception as e:
        return e


def _is_zstd_codec_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "zstd" in s or "missing codec" in s


def download_pc_vv_vh(
    product: dict,
    out_dir: str,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Download VV/VH GeoTIFFs for a STAC item into *out_dir*. Returns (vv_path, vh_path)."""
    item = product.get("_pc_item")
    if item is None:
        return None, None
    os.makedirs(out_dir, exist_ok=True)
    stem = product.get("Id") or product.get("Name") or "granule"
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in str(stem))[:200]
    href_pairs = _vv_vh_href_pairs(item)
    if not href_pairs:
        return None, None

    vv_path = os.path.join(out_dir, f"{safe}_vv.tif")
    vh_path = os.path.join(out_dir, f"{safe}_vh.tif")

    if os.path.isfile(vv_path) and os.path.isfile(vh_path):
        if _probe_pc_geotiff_pair(vv_path, vh_path) is None:
            if log:
                log(f"PC: reuse cached COGs — {vv_path} , {vh_path}")
            return vv_path, vh_path
        if log:
            log(
                "PC: cached VV/VH not readable with this GDAL (often ZSTD vs Deflate) — "
                "removing and re-downloading…"
            )
        try:
            os.remove(vv_path)
            os.remove(vh_path)
        except OSError:
            pass

    def _host_hint(url: str) -> str:
        try:
            return urlparse(url).netloc or "(unknown host)"
        except Exception:
            return "(unknown host)"

    def _stream_download(session, url, dest, band_label: str):
        """Download *url* to *dest*, removing partial file on failure."""
        if log:
            log(
                f"PC GET: {_host_hint(url)} — stream {band_label} → {os.path.basename(dest)} "
                "(signed URL; query not logged)"
            )
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

    last_open_err: Optional[BaseException] = None
    for attempt, (vv_h, vh_h) in enumerate(href_pairs):
        if log:
            pair_note = f" (asset pair {attempt + 1}/{len(href_pairs)})" if len(href_pairs) > 1 else ""
            log(f"PC download: item «{safe}» → {out_dir}{pair_note}")

        with requests.Session() as s:
            _stream_download(s, vv_h, vv_path, "VV")
            _stream_download(s, vh_h, vh_path, "VH")

        if log:
            log(f"PC: wrote {vv_path} , {vh_path}")

        probe = _probe_pc_geotiff_pair(vv_path, vh_path)
        if probe is None:
            return vv_path, vh_path

        last_open_err = probe
        if log:
            log(f"PC: GDAL cannot read downloaded tiles — {_short_pc_open_error(probe)}")
        try:
            os.remove(vv_path)
            os.remove(vh_path)
        except OSError:
            pass

        if attempt + 1 < len(href_pairs) and log:
            log("PC: trying alternate STAC VV/VH URLs…")

    if last_open_err is not None and _is_zstd_codec_error(last_open_err):
        raise RuntimeError(
            "Planetary Computer Sentinel-1 GeoTIFFs are ZSTD-compressed; this QGIS build's "
            "GDAL/libtiff cannot decode ZSTD. "
            "Use Local processing with Copernicus (CDSE) or ASF instead, or install QGIS from a "
            "build that includes ZSTD (e.g. many conda-forge / newer OSGeo4W packages). "
            f"Detail: {last_open_err}"
        )
    return None, None


def _short_pc_open_error(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "unknown read error"
    s = str(exc).strip()
    if len(s) > 180:
        s = s[:179] + "…"
    return s
