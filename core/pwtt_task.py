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

    def __init__(
        self,
        backend,
        aoi_wkt,
        war_start,
        inference_start,
        pre_interval,
        post_interval,
        output_dir,
        include_footprints=False,
        footprints_sources=None,
        job_id=None,
        remote_job_id=None,
        damage_threshold=3.3,
        gee_viz=False,
        data_source=None,
        gee_method='stouffer',
        gee_ttest_type='welch',
        gee_smoothing='default',
        gee_mask_before_smooth=True,
        gee_lee_mode='per_image',
    ):
        super().__init__("PWTT processing", QgsTask.CanCancel)
        self.backend = backend
        self.aoi_wkt = aoi_wkt
        self.war_start = war_start
        self.inference_start = inference_start
        self.pre_interval = pre_interval
        self.post_interval = post_interval
        self.output_dir = output_dir
        self.include_footprints = include_footprints
        # footprints_sources: list of "current_osm", "historical_war_start", "historical_inference_start"
        if footprints_sources is not None:
            self.footprints_sources = list(footprints_sources)
        elif include_footprints:
            self.footprints_sources = ["current_osm"]
        else:
            self.footprints_sources = []
        self.job_id = job_id
        self.remote_job_id = remote_job_id  # openEO job id for resume
        self.damage_threshold = float(damage_threshold)
        self.gee_viz = bool(gee_viz)
        self.gee_method = str(gee_method)
        self.gee_ttest_type = str(gee_ttest_type)
        self.gee_smoothing = str(gee_smoothing)
        self.gee_mask_before_smooth = bool(gee_mask_before_smooth)
        self.gee_lee_mode = str(gee_lee_mode)
        # Local GRD catalog (cdse/asf/pc); used in layer tree names.
        self.data_source = (
            (data_source or "").strip().lower() if data_source else None
        )
        self.output_tif = None
        self.footprints_gpkg = None  # kept for backwards compat (first source)
        self.footprints_gpkgs = {}   # source -> gpkg path
        self.exception = None
        self.error_detail = ""
        self.products_offline = False
        self.offline_product_ids = []
        self.offline_products = []
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
                damage_threshold=self.damage_threshold,
                gee_viz=self.gee_viz,
            )
            if getattr(self.backend, 'id', None) == 'gee':
                run_kwargs.update(
                    method=self.gee_method,
                    ttest_type=self.gee_ttest_type,
                    smoothing=self.gee_smoothing,
                    mask_before_smooth=self.gee_mask_before_smooth,
                    lee_mode=self.gee_lee_mode,
                )
            # Pass remote_job_id for backends that support resuming (openEO)
            if self.remote_job_id:
                run_kwargs["remote_job_id"] = self.remote_job_id
            result_path = self.backend.run(**run_kwargs)
        except ProductsOfflineError as e:
            self._capture_remote_job_id()
            self.products_offline = True
            self.offline_product_ids = list(e.product_ids)
            scenes = getattr(e, "offline_scenes", None) or []
            self.offline_products = [
                {"id": s.get("id", ""), "name": s.get("name", ""), "date": s.get("date", "")}
                for s in scenes
                if isinstance(s, dict) and s.get("id")
            ]
            self._emit_msg(str(e))
            # Return True so QgsTask emits taskCompleted (not taskTerminated). Waiting-for-GRD
            # is handled in the jobs dock; False would trigger QGIS "task failed" notifications.
            return True
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
                "damage_threshold": self.damage_threshold,
                "gee_viz": self.gee_viz,
                "gee_method": self.gee_method,
                "gee_ttest_type": self.gee_ttest_type,
                "gee_smoothing": self.gee_smoothing,
                "gee_mask_before_smooth": self.gee_mask_before_smooth,
                "gee_lee_mode": self.gee_lee_mode,
                "output_tif": self.output_tif,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            # Include backend run_metadata (scenes, date ranges, logs, etc.)
            run_meta = getattr(self.backend, "run_metadata", None)
            if run_meta and isinstance(run_meta, dict):
                meta["processing_details"] = run_meta
            meta_path = os.path.join(self.output_dir, "job_info.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception as meta_err:
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"Failed to write job metadata: {meta_err}",
                "PWTT",
                level=Qgis.Warning,
            )

        if self.footprints_sources:
            _source_date = {
                "current_osm": None,
                "historical_war_start": self.war_start,
                "historical_inference_start": self.inference_start,
            }
            _source_suffix = {
                "current_osm": "current",
                "historical_war_start": "war_start",
                "historical_inference_start": "infer_start",
            }
            from .footprints import compute_footprints
            for source in self.footprints_sources:
                suffix = _source_suffix.get(source, source)
                fp_name = (
                    f"pwtt_{self.job_id}_footprints_{suffix}.gpkg"
                    if self.job_id else f"pwtt_footprints_{suffix}.gpkg"
                )
                fp_path = os.path.join(self.output_dir, fp_name)
                date_iso = _source_date.get(source)
                try:
                    compute_footprints(
                        result_path,
                        self.aoi_wkt,
                        fp_path,
                        date_iso=date_iso,
                        progress_callback=progress,
                    )
                    if os.path.isfile(fp_path):
                        self.footprints_gpkgs[source] = fp_path
                        if self.footprints_gpkg is None:
                            self.footprints_gpkg = fp_path  # backwards compat
                except Exception as e:
                    self.exception = e
                    self.error_detail = traceback.format_exc()
                    return False
        return True

    def finished(self, success):
        if success and self.output_tif and os.path.isfile(self.output_tif):
            from .qgis_layer_tree import (
                add_map_layer_to_pwtt_job_group,
                pwtt_damage_layer_name,
                pwtt_footprints_layer_name,
            )
            from .qgis_output_style import style_pwtt_footprints_layer, style_pwtt_raster_layer

            backend_id = getattr(self.backend, "id", None)
            ds = self.data_source
            if backend_id == "local" and not ds:
                ds = getattr(self.backend, "_data_source", None)
            project = QgsProject.instance()
            label = pwtt_damage_layer_name(self.job_id, backend_id, data_source=ds)
            layer = QgsRasterLayer(self.output_tif, label, "gdal")
            if layer.isValid():
                style_pwtt_raster_layer(layer, damage_threshold=self.damage_threshold)
                add_map_layer_to_pwtt_job_group(
                    project, layer, self.job_id, backend_id, data_source=ds
                )
            for source, fp_path in self.footprints_gpkgs.items():
                if os.path.isfile(fp_path):
                    fp_label = pwtt_footprints_layer_name(
                        self.job_id,
                        backend_id,
                        source,
                        data_source=ds,
                        war_start=self.war_start,
                        inference_start=self.inference_start,
                    )
                    vl = QgsVectorLayer(fp_path, fp_label, "ogr")
                    if vl.isValid():
                        style_pwtt_footprints_layer(vl)
                        add_map_layer_to_pwtt_job_group(
                            project, vl, self.job_id, backend_id, data_source=ds
                        )
            # GEE map preview must run on the main thread (webbrowser.open)
            if self.gee_viz:
                viz_aoi = getattr(self.backend, "_viz_aoi", None)
                viz_image = getattr(self.backend, "_viz_image", None)
                if viz_aoi is not None and viz_image is not None:
                    try:
                        from .gee_pwtt import open_geemap_preview
                        open_geemap_preview(viz_aoi, viz_image, output_dir=self.output_dir)
                    except Exception as e:
                        from qgis.core import QgsMessageLog, Qgis
                        QgsMessageLog.logMessage(
                            f"GEE map preview failed: {e}", "PWTT", level=Qgis.Warning,
                        )
        if not success and self.exception:
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"{self.exception}\n{self.error_detail}",
                "PWTT",
                level=Qgis.Critical,
            )
