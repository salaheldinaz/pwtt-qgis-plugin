# -*- coding: utf-8 -*-
"""QgsTask that runs a PWTT backend and loads results into the project."""

from qgis.core import QgsTask, QgsRasterLayer, QgsVectorLayer, QgsProject
import os


class PWTTRunTask(QgsTask):
    """Background task: run backend.run(), then add raster (and optional footprints) to project."""

    def __init__(self, backend, aoi_wkt, war_start, inference_start, pre_interval, post_interval, output_dir, include_footprints=False):
        super().__init__("PWTT processing", QgsTask.CanCancel)
        self.backend = backend
        self.aoi_wkt = aoi_wkt
        self.war_start = war_start
        self.inference_start = inference_start
        self.pre_interval = pre_interval
        self.post_interval = post_interval
        self.output_dir = output_dir
        self.include_footprints = include_footprints
        self.output_tif = None
        self.footprints_gpkg = None
        self._progress_callback = None

    def run(self):
        from .utils import ensure_output_dir
        out_tif = os.path.join(self.output_dir, "pwtt_result.tif")
        ensure_output_dir(out_tif)
        footprints_path = os.path.join(self.output_dir, "pwtt_footprints.gpkg") if self.include_footprints else None

        def progress(percent, msg):
            self.setProgress(percent)
            if self.isCanceled():
                raise Exception("Canceled")

        try:
            result_path = self.backend.run(
                aoi_wkt=self.aoi_wkt,
                war_start=self.war_start,
                inference_start=self.inference_start,
                pre_interval=self.pre_interval,
                post_interval=self.post_interval,
                output_path=out_tif,
                progress_callback=progress,
                include_footprints=False,
                footprints_path=None,
            )
        except Exception as e:
            self.exception = e
            return False
        self.output_tif = result_path
        if self.include_footprints and footprints_path:
            try:
                from .footprints import compute_footprints
                compute_footprints(
                    result_path,
                    self.aoi_wkt,
                    footprints_path,
                    progress_callback=progress,
                )
                if os.path.isfile(footprints_path):
                    self.footprints_gpkg = footprints_path
            except Exception as e:
                self.exception = e
                return False
        return True

    def finished(self, success):
        if success and self.output_tif and os.path.isfile(self.output_tif):
            layer = QgsRasterLayer(self.output_tif, "PWTT damage", "gdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            if self.footprints_gpkg and os.path.isfile(self.footprints_gpkg):
                vl = QgsVectorLayer(self.footprints_gpkg, "PWTT footprints", "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
        if not success and getattr(self, "exception", None):
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(str(self.exception), "PWTT", level=Qgis.Critical)
