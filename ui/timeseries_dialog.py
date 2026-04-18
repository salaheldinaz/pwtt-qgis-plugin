# -*- coding: utf-8 -*-
"""Per-image SAR orbit-normalized z-score chart dialog.

Reads the `pwtt_<job_id>_timeseries.json` sidecar written by the GEE or Local
backend and renders a QPainter-based scatter plot with dashed ±z thresholds.
Pure PyQt5 — no matplotlib, no QtCharts, no extra dependencies.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import shutil
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QPoint, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPixmap,
)
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..core.timeseries_sidecar import read_sidecar, sidecar_csv_path
from ..core.viz_constants import (
    TIMESERIES_THRESHOLD_COLOR,
    TIMESERIES_VH_COLOR,
    TIMESERIES_VV_COLOR,
    TIMESERIES_WAR_START_COLOR,
    TIMESERIES_Z_THRESHOLD,
)


def _parse_iso(ts: str) -> Optional[_dt.datetime]:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return _dt.datetime.strptime(ts[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _fmt_date_label(dt: _dt.datetime) -> str:
    return dt.strftime("%b %Y")


def _nice_ticks(lo: float, hi: float, target: int = 6) -> List[float]:
    """Return 'nice' tick values spanning [lo, hi]."""
    if lo == hi:
        return [lo]
    raw = (hi - lo) / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if raw / step <= 1:
            break
    start = math.floor(lo / step) * step
    ticks = []
    x = start
    while x <= hi + step * 0.5:
        ticks.append(round(x, 10))
        x += step
    return ticks


class _TimeSeriesChart(QWidget):
    """Scatter chart painted with QPainter.

    Series order & styling mirrors the attached EE Code Editor export: VV first,
    VH second, dashed ±2.576 thresholds, optional vertical war_start marker.
    """

    _MARGIN_L = 72
    _MARGIN_R = 24
    _MARGIN_T = 48
    _MARGIN_B = 64
    _POINT_R = 4

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._payload = payload or {}
        self._title = self._build_title()
        self._thresholds = self._payload.get("thresholds") or {
            "z_lower_99": -TIMESERIES_Z_THRESHOLD,
            "z_upper_99": TIMESERIES_Z_THRESHOLD,
        }
        self._war_start = _parse_iso(self._payload.get("war_start") or "")
        # Normalised points: list of (datetime, vv_z, vh_z, orbit, pass, period)
        self._points = self._extract_points()
        self._hit_targets: List[Tuple[int, int, int, str]] = []  # (px, py, radius, tooltip)
        if self._points:
            dts = [p[0] for p in self._points]
            self._x_min = min(dts)
            self._x_max = max(dts)
            if self._x_min == self._x_max:
                self._x_min -= _dt.timedelta(days=1)
                self._x_max += _dt.timedelta(days=1)
        else:
            self._x_min = _dt.datetime.now() - _dt.timedelta(days=30)
            self._x_max = _dt.datetime.now()
        vals = []
        for _dt_v, vv, vh, *_ in self._points:
            if vv is not None:
                vals.append(vv)
            if vh is not None:
                vals.append(vh)
        thr = float(self._thresholds.get("z_upper_99") or TIMESERIES_Z_THRESHOLD)
        vals.extend([-thr * 1.2, thr * 1.2])
        self._y_min = min(vals)
        self._y_max = max(vals)
        pad = (self._y_max - self._y_min) * 0.05 or 1.0
        self._y_min -= pad
        self._y_max += pad

    def _build_title(self) -> str:
        backend = (self._payload.get("backend") or "").upper()
        jid = self._payload.get("job_id") or ""
        suffix = f" ({backend} · {jid[:8]})" if backend or jid else ""
        return f"Orbit-normalized z-scores{suffix}"

    def _extract_points(self):
        out = []
        for entry in self._payload.get("series") or []:
            dt = _parse_iso(entry.get("date") or "")
            if dt is None:
                continue
            out.append(
                (
                    dt,
                    entry.get("VV_z"),
                    entry.get("VH_z"),
                    entry.get("orbit"),
                    entry.get("pass"),
                    entry.get("period") or "",
                )
            )
        out.sort(key=lambda p: p[0])
        return out

    # ── coordinate transforms ────────────────────────────────────────────────
    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._MARGIN_L,
            self._MARGIN_T,
            max(1, self.width() - self._MARGIN_L - self._MARGIN_R),
            max(1, self.height() - self._MARGIN_T - self._MARGIN_B),
        )

    def _x_to_px(self, dt: _dt.datetime, rect: QRectF) -> float:
        span = (self._x_max - self._x_min).total_seconds() or 1.0
        frac = (dt - self._x_min).total_seconds() / span
        return rect.left() + frac * rect.width()

    def _y_to_px(self, y: float, rect: QRectF) -> float:
        span = (self._y_max - self._y_min) or 1.0
        frac = (y - self._y_min) / span
        return rect.bottom() - frac * rect.height()

    # ── painting ─────────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        rect = self._plot_rect()
        self._hit_targets.clear()

        self._draw_title(painter)
        self._draw_axes(painter, rect)
        self._draw_grid_and_ticks(painter, rect)
        self._draw_thresholds(painter, rect)
        self._draw_war_start(painter, rect)
        self._draw_points(painter, rect)
        self._draw_legend(painter, rect)
        painter.end()

    def _draw_title(self, painter: QPainter):
        painter.save()
        font = painter.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 1)
        painter.setFont(font)
        painter.setPen(self.palette().text().color())
        painter.drawText(QPoint(self._MARGIN_L, 24), self._title)
        painter.restore()

    def _draw_axes(self, painter: QPainter, rect: QRectF):
        pen = QPen(self.palette().text().color())
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawLine(QPointF(rect.left(), rect.top()), QPointF(rect.left(), rect.bottom()))
        painter.drawLine(
            QPointF(rect.left(), rect.bottom()), QPointF(rect.right(), rect.bottom())
        )

    def _draw_grid_and_ticks(self, painter: QPainter, rect: QRectF):
        fg = self.palette().text().color()
        grid_pen = QPen(QColor(fg.red(), fg.green(), fg.blue(), 55))
        grid_pen.setStyle(Qt.DotLine)
        tick_pen = QPen(fg)
        tick_pen.setWidthF(1.0)

        fm = QFontMetrics(painter.font())

        # Y ticks
        for y in _nice_ticks(self._y_min, self._y_max, target=6):
            py = self._y_to_px(y, rect)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(rect.left(), py), QPointF(rect.right(), py))
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(rect.left() - 4, py), QPointF(rect.left(), py))
            label = f"{y:g}"
            tw = fm.horizontalAdvance(label)
            painter.drawText(QPointF(rect.left() - tw - 8, py + fm.ascent() / 2 - 2), label)

        # X ticks — month-based
        ticks = self._x_month_ticks()
        for dt in ticks:
            px = self._x_to_px(dt, rect)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(px, rect.top()), QPointF(px, rect.bottom()))
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(px, rect.bottom()), QPointF(px, rect.bottom() + 4))
            label = _fmt_date_label(dt)
            tw = fm.horizontalAdvance(label)
            painter.drawText(QPointF(px - tw / 2, rect.bottom() + fm.ascent() + 6), label)

        # Axis titles
        painter.setPen(fg)
        painter.drawText(
            QPointF(rect.center().x() - fm.horizontalAdvance("Date") / 2, self.height() - 8),
            "Date",
        )
        painter.save()
        painter.translate(18, rect.center().y())
        painter.rotate(-90)
        painter.drawText(QPointF(-fm.horizontalAdvance("z-score") / 2, 0), "z-score")
        painter.restore()

    def _x_month_ticks(self) -> List[_dt.datetime]:
        if self._x_max <= self._x_min:
            return [self._x_min]
        span_days = (self._x_max - self._x_min).days or 1
        # Aim for ~6-8 ticks; choose month stride.
        months_total = max(1, span_days // 30)
        stride = max(1, round(months_total / 7))
        ticks = []
        y, m = self._x_min.year, self._x_min.month
        cursor = _dt.datetime(y, m, 1)
        if cursor < self._x_min:
            pass
        while cursor <= self._x_max:
            if cursor >= self._x_min:
                ticks.append(cursor)
            m += stride
            while m > 12:
                m -= 12
                y += 1
            cursor = _dt.datetime(y, m, 1)
        if not ticks:
            ticks = [self._x_min, self._x_max]
        return ticks

    def _draw_thresholds(self, painter: QPainter, rect: QRectF):
        thr_hi = float(self._thresholds.get("z_upper_99") or TIMESERIES_Z_THRESHOLD)
        thr_lo = float(self._thresholds.get("z_lower_99") or -TIMESERIES_Z_THRESHOLD)
        pen = QPen(QColor(TIMESERIES_THRESHOLD_COLOR))
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.2)
        painter.setPen(pen)
        for y in (thr_hi, thr_lo):
            py = self._y_to_px(y, rect)
            painter.drawLine(QPointF(rect.left(), py), QPointF(rect.right(), py))
        # Zero line
        zero_pen = QPen(self.palette().text().color())
        zero_pen.setWidthF(0.8)
        painter.setPen(zero_pen)
        py0 = self._y_to_px(0.0, rect)
        painter.drawLine(QPointF(rect.left(), py0), QPointF(rect.right(), py0))

    def _draw_war_start(self, painter: QPainter, rect: QRectF):
        if not self._war_start:
            return
        if not (self._x_min <= self._war_start <= self._x_max):
            return
        pen = QPen(QColor(TIMESERIES_WAR_START_COLOR))
        pen.setStyle(Qt.DashDotLine)
        pen.setWidthF(1.2)
        painter.setPen(pen)
        px = self._x_to_px(self._war_start, rect)
        painter.drawLine(QPointF(px, rect.top()), QPointF(px, rect.bottom()))
        fm = QFontMetrics(painter.font())
        label = f"war/event start {self._war_start.strftime('%Y-%m-%d')}"
        painter.drawText(QPointF(px + 4, rect.top() + fm.ascent()), label)

    def _draw_points(self, painter: QPainter, rect: QRectF):
        vv_color = QColor(TIMESERIES_VV_COLOR)
        vh_color = QColor(TIMESERIES_VH_COLOR)
        r = self._POINT_R
        for dt, vv, vh, orbit, pass_dir, period in self._points:
            px = self._x_to_px(dt, rect)
            tip_date = dt.strftime("%Y-%m-%d")
            if vv is not None and math.isfinite(vv):
                py = self._y_to_px(vv, rect)
                painter.setPen(QPen(vv_color.darker(120), 0.8))
                painter.setBrush(QBrush(vv_color))
                painter.drawEllipse(QPointF(px, py), r, r)
                tip = self._tooltip_for(tip_date, "VV", vv, vh, orbit, pass_dir, period)
                self._hit_targets.append((int(px), int(py), r + 3, tip))
            if vh is not None and math.isfinite(vh):
                py = self._y_to_px(vh, rect)
                painter.setPen(QPen(vh_color.darker(120), 0.8))
                painter.setBrush(QBrush(vh_color))
                painter.drawEllipse(QPointF(px, py), r, r)
                tip = self._tooltip_for(tip_date, "VH", vv, vh, orbit, pass_dir, period)
                self._hit_targets.append((int(px), int(py), r + 3, tip))

    @staticmethod
    def _tooltip_for(date: str, which: str, vv, vh, orbit, pass_dir, period) -> str:
        lines = [f"<b>{date}</b> · {period or '?'}"]
        if vv is not None:
            lines.append(f"VV z = {vv:+.3f}")
        if vh is not None:
            lines.append(f"VH z = {vh:+.3f}")
        extra = []
        if orbit is not None:
            extra.append(f"orbit {orbit}")
        if pass_dir:
            extra.append(str(pass_dir).lower())
        if extra:
            lines.append(" · ".join(extra))
        return "<br/>".join(lines)

    def _draw_legend(self, painter: QPainter, rect: QRectF):
        fm = QFontMetrics(painter.font())
        items = [("VV", QColor(TIMESERIES_VV_COLOR)), ("VH", QColor(TIMESERIES_VH_COLOR))]
        x = rect.right() - 90
        y = rect.top() + 4
        box_w = 82
        box_h = fm.height() * len(items) + 10
        bg = self.palette().base().color()
        bg.setAlpha(220)
        painter.setPen(QPen(self.palette().mid().color()))
        painter.setBrush(QBrush(bg))
        painter.drawRect(QRectF(x, y, box_w, box_h))
        painter.setPen(self.palette().text().color())
        for i, (name, color) in enumerate(items):
            cy = y + 8 + i * fm.height()
            painter.setPen(QPen(color, 2))
            painter.drawLine(QPointF(x + 8, cy + fm.ascent() / 2 - 2),
                             QPointF(x + 24, cy + fm.ascent() / 2 - 2))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(x + 16, cy + fm.ascent() / 2 - 2), 3, 3)
            painter.setPen(self.palette().text().color())
            painter.drawText(QPointF(x + 32, cy + fm.ascent()), name)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        for px, py, radius, tip in self._hit_targets:
            if abs(pos.x() - px) <= radius and abs(pos.y() - py) <= radius:
                QToolTip.showText(event.globalPos(), tip, self)
                return
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def save_png(self, path: str) -> bool:
        pix = QPixmap(self.size())
        pix.fill(self.palette().base().color())
        painter = QPainter(pix)
        self.render(painter)
        painter.end()
        return pix.save(path, "PNG")


class PWTTTimeSeriesDialog(QDialog):
    """Modal dialog presenting the per-image orbit-normalized z-score chart."""

    def __init__(self, job: dict, parent=None):
        super().__init__(parent)
        self._job = job or {}
        jid = self._job.get("id") or ""
        self.setWindowTitle(f"PWTT time series — {jid}")
        self.resize(880, 520)

        self._payload = None
        self._output_tif = self._resolve_output_tif()
        if self._output_tif:
            self._payload = read_sidecar(self._output_tif)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        if not self._payload:
            root.addWidget(self._build_placeholder())
        else:
            self._chart = _TimeSeriesChart(self._payload, self)
            root.addWidget(self._chart, 1)
            root.addWidget(self._build_subtitle())

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.export_csv_btn = QPushButton("Export CSV\u2026")
        self.save_png_btn = QPushButton("Save PNG\u2026")
        self.export_csv_btn.setEnabled(bool(self._payload and self._output_tif))
        self.save_png_btn.setEnabled(bool(self._payload))
        self.export_csv_btn.clicked.connect(self._export_csv)
        self.save_png_btn.clicked.connect(self._save_png)
        btn_row.addWidget(self.export_csv_btn)
        btn_row.addWidget(self.save_png_btn)
        btn_row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        btn_row.addWidget(bb)
        root.addLayout(btn_row)

    def _resolve_output_tif(self) -> Optional[str]:
        tif = (self._job.get("output_tif") or "").strip()
        if tif and os.path.isfile(tif):
            return tif
        out_dir = (self._job.get("output_dir") or "").strip()
        jid = self._job.get("id") or ""
        if out_dir and jid:
            candidate = os.path.join(out_dir, f"pwtt_{jid}.tif")
            if os.path.isfile(candidate):
                return candidate
            # sidecar may exist even if the TIF was moved — use the virtual path
            return candidate
        return None

    def _build_placeholder(self) -> QLabel:
        backend = (self._job.get("backend_id") or "").lower()
        if backend == "openeo":
            msg = (
                "Per-image time series is not available for openEO jobs.\n\n"
                "The openEO backend pools pre- and post-war/event images into composites, "
                "so there is no per-acquisition signal to chart."
            )
        else:
            msg = (
                "No time series sidecar found for this job.\n\n"
                "This job was run before the time-series feature was added. "
                "Rerun the job to generate the chart data."
            )
        label = QLabel(msg)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumHeight(220)
        return label

    def _build_subtitle(self) -> QLabel:
        backend = (self._payload.get("backend") or "").upper()
        norm = self._payload.get("normalization") or ""
        n = len(self._payload.get("series") or [])
        text = f"{backend} · {n} images · {norm}"
        lbl = QLabel(text)
        lbl.setStyleSheet("color: palette(mid);")
        return lbl

    def _export_csv(self):
        src = sidecar_csv_path(self._output_tif) if self._output_tif else ""
        if not src or not os.path.isfile(src):
            QMessageBox.information(
                self,
                "PWTT",
                "CSV sidecar not found on disk. Rerun the job to regenerate.",
            )
            return
        default_name = os.path.basename(src)
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export time series CSV", default_name, "CSV (*.csv)"
        )
        if not dest:
            return
        try:
            shutil.copyfile(src, dest)
        except OSError as err:
            QMessageBox.warning(self, "PWTT", f"Could not write CSV:\n{err}")
            return
        QMessageBox.information(self, "PWTT", f"CSV exported to:\n{dest}")

    def _save_png(self):
        if not hasattr(self, "_chart"):
            return
        default_name = f"pwtt_{self._job.get('id', 'job')}_timeseries.png"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save chart as PNG", default_name, "PNG (*.png)"
        )
        if not dest:
            return
        if not self._chart.save_png(dest):
            QMessageBox.warning(self, "PWTT", "Could not save PNG.")
            return
        QMessageBox.information(self, "PWTT", f"Chart saved to:\n{dest}")
