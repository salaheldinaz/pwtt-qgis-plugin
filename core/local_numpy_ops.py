# -*- coding: utf-8 -*-
"""NumPy-only replacements for scipy.ndimage / scipy.stats used by LocalBackend.

QGIS ships its own numpy + scipy; they often disagree (ImportError inside scipy).
These helpers avoid scipy entirely so local processing runs on QGIS's numpy only.
"""

from __future__ import annotations

import numpy as np


def uniform_filter2d_edge(a: np.ndarray, size: int) -> np.ndarray:
    """Separable moving average, edge padding. *size* must be odd (e.g. Lee filter)."""
    if size < 1 or size % 2 == 0:
        raise ValueError("uniform_filter2d_edge: size must be a positive odd int")
    r = size // 2
    h, w = a.shape
    x = np.asarray(a, dtype=np.float64)
    # horizontal
    p = np.pad(x, ((0, 0), (r, r)), mode="edge")
    c = np.cumsum(p, axis=1)
    if w == 1:
        left = np.zeros((h, 1), dtype=np.float64)
    else:
        left = np.hstack([np.zeros((h, 1), dtype=np.float64), c[:, : w - 1]])
    tmp = (c[:, size - 1 : size - 1 + w] - left) / float(size)
    # vertical
    p2 = np.pad(tmp, ((r, r), (0, 0)), mode="edge")
    c2 = np.cumsum(p2, axis=0)
    if h == 1:
        left2 = np.zeros((1, w), dtype=np.float64)
    else:
        left2 = np.vstack([np.zeros((1, w), dtype=np.float64), c2[: h - 1, :]])
    return (c2[size - 1 : size - 1 + h, :] - left2) / float(size)


def _gaussian_kernel_1d(sigma: float, truncate: float = 4.0) -> np.ndarray:
    sigma = max(float(sigma), 1e-12)
    r = max(0, int(truncate * sigma + 0.5))
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum() + 1e-30
    return k


def _sep_convolve_axis0(a: np.ndarray, k: np.ndarray) -> np.ndarray:
    r = len(k) // 2
    h, w = a.shape
    p = np.pad(a, ((r, r), (0, 0)), mode="edge")
    out = np.zeros((h, w), dtype=np.float64)
    for t, kt in enumerate(k):
        out += float(kt) * p[t : t + h, :]
    return out


def _sep_convolve_axis1(a: np.ndarray, k: np.ndarray) -> np.ndarray:
    r = len(k) // 2
    h, w = a.shape
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    out = np.zeros((h, w), dtype=np.float64)
    for t, kt in enumerate(k):
        out += float(kt) * p[:, t : t + w]
    return out


def gaussian_filter2d_edge(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian, edge padding (same idea as scipy ndimage nearest edge)."""
    k = _gaussian_kernel_1d(sigma)
    x = np.asarray(a, dtype=np.float64)
    t = _sep_convolve_axis0(x, k)
    return _sep_convolve_axis1(t, k)


def convolve2d_edge(img: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Correlation with *kernel* (unflipped), edge-padded input; output shape = *img* shape."""
    k = np.asarray(kernel, dtype=np.float64)
    kh, kw = k.shape
    r0, c0 = kh // 2, kw // 2
    padded = np.pad(np.asarray(img, dtype=np.float64), ((r0, r0), (c0, c0)), mode="edge")
    h, w = img.shape
    out = np.zeros((h, w), dtype=np.float64)
    ui, vi = np.nonzero(k != 0)
    for t in range(len(ui)):
        u, vi_ = int(ui[t]), int(vi[t])
        out += float(k[u, vi_]) * padded[u : u + h, vi_ : vi_ + w]
    return out


def two_sided_normal_p_value(t_abs: np.ndarray) -> np.ndarray:
    """Two-tailed normal p-value matching 2 * scipy.stats.norm.sf(t) for standard normal, t >= 0."""
    z = np.asarray(t_abs, dtype=np.float64)
    # erfc(x) = 1 - erf(x); 2*SF(t) = erfc(t/sqrt(2)) for standard normal
    if hasattr(np, "erf"):
        return np.clip(1.0 - np.erf(z / np.sqrt(2.0)), 1e-300, 1.0)
    # very old numpy fallback (unlikely in QGIS 3)
    from math import erf

    flat = z.ravel()
    out = np.empty_like(flat)
    for i in range(flat.size):
        out[i] = 1.0 - erf(float(flat[i]) / np.sqrt(2.0))
    return np.clip(out.reshape(z.shape), 1e-300, 1.0)
