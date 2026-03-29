# -*- coding: utf-8 -*-
"""QgsTask that runs a PWTT backend and loads results into the project."""

import json
import traceback
from datetime import datetime
from qgis.core import QgsTask, QgsRasterLayer, QgsVectorLayer, QgsProject
import os


class PWTTRunTask(QgsTask):
    """Background task: run backend.run(), then add raster (and optional footprints) to project."""

    status_message_changed = None  # will be wired as a signal-like list of callbacks

    def __init__(self, backend, aoi_wkt, war_start, inference_start, pre_interval, post_interval, output_dir, include_footprints=False, job_id=None, remote_job_id=None):
        super().__init__("PWTT processing", QgsTask.CanCancel)
        self.backend = backend
        self.aoi_wkt = aoi_wkt
        self.war_start = war_start
        self.inference_start = inference_start
        self.pre_interval = pre_interval
        self.post_interval = post_interval
        self.output_dir = output_dir
        self.include_footprints = include_footprints
        self.job_id = job_id
        self.remote_job_id = remote_job_id  # openEO job id for resume
        self.output_tif = None
        self.footprints_gpkg = None
        self.exception = None
        self.error_detail = ""
        self.products_offline = False
        self.offline_product_ids = []
        self._status_msg = ""
        self._msg_callbacks = []

    def _capture_remote_job_id(self):
        """Copy the remote job id from the backend (if it set one during run)."""
        rid = getattr(self.backend, "remote_job_id", None)
        if rid:
            self.remote_job_id = rid

    def on_status_message(self, callback):
        """Register a callback(str) to receive progress messages from the worker thread."""
        self._msg_callbacks.append(callback)

    def _emit_msg(self, msg: str):
        self._status_msg = msg
        for cb in self._msg_callbacks:
            try:
                cb(msg)
            except Exception:
                pass

    def run(self):
        from .utils import ensure_output_dir
        from .base_backend import ProductsOfflineError
        tif_name = f"pwtt_{self.job_id}.tif" if self.job_id else "pwtt_result.tif"
        out_tif = os.path.join(self.output_dir, tif_name)
        ensure_output_dir(out_tif)
        fp_name = f"pwtt_{self.job_id}_footprints.gpkg" if self.job_id else "pwtt_footprints.gpkg"
        footprints_path = os.path.join(self.output_dir, fp_name) if self.include_footprints else None

        def progress(percent, msg):
            self.setProgress(percent)
            self._emit_msg(msg)
            if self.isCanceled():
                raise Exception("Canceled")

        try:
            run_kwargs = dict(
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
            # Pass remote_job_id for backends that support resuming (openEO)
            if self.remote_job_id:
                run_kwargs["remote_job_id"] = self.remote_job_id
            result_path = self.backend.run(**run_kwargs)
        except ProductsOfflineError as e:
            self._capture_remote_job_id()
            self.products_offline = True
            self.offline_product_ids = list(e.product_ids)
            self._emit_msg(str(e))
            return False
        except Exception as e:
            self._capture_remote_job_id()
            self.exception = e
            self.error_detail = traceback.format_exc()
            return False
        self._capture_remote_job_id()
        self.output_tif = result_path

        # Write job metadata JSON
        try:
            meta = {
                "job_id": self.job_id,
                "remote_job_id": self.remote_job_id,
                "backend": getattr(self.backend, "id", None),
                "aoi_wkt": self.aoi_wkt,
                "war_start": self.war_start,
                "inference_start": self.inference_start,
                "pre_interval": self.pre_interval,
                "post_interval": self.post_interval,
                "include_footprints": self.include_footprints,
                "output_tif": self.output_tif,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            meta_path = os.path.join(self.output_dir, "job_info.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # metadata is best-effort

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
                self.error_detail = traceback.format_exc()
                return False
        return True

    def finished(self, success):
        if success and self.output_tif and os.path.isfile(self.output_tif):
            label = f"PWTT damage ({self.job_id})" if self.job_id else "PWTT damage"
            layer = QgsRasterLayer(self.output_tif, label, "gdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            if self.footprints_gpkg and os.path.isfile(self.footprints_gpkg):
                fp_label = f"PWTT footprints ({self.job_id})" if self.job_id else "PWTT footprints"
                vl = QgsVectorLayer(self.footprints_gpkg, fp_label, "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
        if not success and self.exception:
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"{self.exception}\n{self.error_detail}",
                "PWTT",
                level=Qgis.Critical,
            )
