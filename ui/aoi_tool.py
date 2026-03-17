# -*- coding: utf-8 -*-
"""Map tool for drawing AOI rectangle on the canvas."""

from qgis.core import (
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from qgis.gui import QgsMapToolExtent


class PWTTMapToolExtent(QgsMapToolExtent):
    """
    QgsMapToolExtent that emits the drawn extent as WKT (EPSG:4326) when the user finishes drawing.
    """

    def __init__(self, canvas, on_extent_drawn):
        super().__init__(canvas)
        self._on_extent_drawn = on_extent_drawn
        self.extentChanged.connect(self._handle_extent_changed)

    def _handle_extent_changed(self):
        rect = self.extent()
        if rect.isEmpty():
            if self._on_extent_drawn:
                self._on_extent_drawn(None, None)
            return
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != dest_crs:
            transform = QgsCoordinateTransform(canvas_crs, dest_crs, QgsProject.instance())
            rect = transform.transformBoundingBox(rect)
        geom = QgsGeometry.fromRect(rect)
        wkt = geom.asWkt()
        if self._on_extent_drawn:
            self._on_extent_drawn(wkt, rect)
