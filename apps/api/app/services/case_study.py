"""Read-only API view of verified real case-study inputs and outputs.

This module exposes display quicklooks from an existing completed SNAP job.
It deliberately does not create flood predictions or infrastructure exposure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import zlib
from typing import Any

import numpy as np
import rasterio
from rasterio.warp import transform_bounds


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MANIFEST_RELATIVE = Path("data/manifests/sentinel1_preprocessing/manifest.json")
PROCESSED_RELATIVE = Path("data/processed/sentinel1")
OSM_METADATA_RELATIVE = Path(
    "data/raw/osm"
)
OSM_PREPARED_RELATIVE = Path(
    "data/processed/infrastructure"
)
MODEL_EVALUATION_RELATIVE = Path("reports/generated")
SENTINEL_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2026."
OSM_ATTRIBUTION = "© OpenStreetMap contributors."


class CaseStudyUnavailable(Exception):
    """Raised when no safe, completed case-study raster pair is available."""


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _encode_rgba_png(gray: np.ndarray, alpha: np.ndarray) -> bytes:
    height, width = gray.shape
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, :3] = gray[:, :, None]
    rgba[:, :, 3] = alpha
    rows = b"".join(b"\x00" + rgba[row].tobytes() for row in range(height))
    header = struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, level=6))
        + _png_chunk(b"IEND", b"")
    )


class CaseStudyService:
    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root.resolve()

    def _read_json(self, relative_path: Path) -> dict[str, Any] | None:
        return self._read_json_path(self.project_root / relative_path)

    def _read_json_path(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _latest_job(self) -> tuple[dict[str, Any], dict[str, Path]]:
        manifest = self._read_json(MANIFEST_RELATIVE)
        if not manifest:
            raise CaseStudyUnavailable("No preprocessing manifest is available.")
        processed_root = (self.project_root / PROCESSED_RELATIVE).resolve()
        candidates = sorted(
            (job for job in manifest.get("jobs", []) if isinstance(job, dict) and job.get("status") == "completed"),
            key=lambda job: str(job.get("updated_at", "")),
            reverse=True,
        )
        for job in candidates:
            outputs = job.get("outputs")
            if not isinstance(outputs, dict):
                continue
            paths: dict[str, Path] = {}
            for role in ("pre_event", "post_event"):
                output = outputs.get(role)
                if not isinstance(output, dict) or not isinstance(output.get("path"), str):
                    break
                path = (processed_root / output["path"]).resolve()
                if not path.is_relative_to(processed_root) or path.suffix.lower() not in {".tif", ".tiff"} or not path.is_file():
                    break
                expected_sha256 = output.get("sha256")
                if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
                    break
                digest = hashlib.sha256()
                with path.open("rb") as source_file:
                    for block in iter(lambda: source_file.read(8 * 1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != expected_sha256.lower():
                    break
                paths[role] = path
            if len(paths) == 2:
                with rasterio.open(paths["pre_event"]) as pre, rasterio.open(paths["post_event"]) as post:
                    if (
                        pre.crs is None
                        or pre.crs != post.crs
                        or pre.transform != post.transform
                        or pre.width != post.width
                        or pre.height != post.height
                        or pre.count < 2
                        or post.count < 2
                        or pre.descriptions[:2] != ("Sigma0_VV", "Sigma0_VH")
                        or post.descriptions[:2] != ("Sigma0_VV", "Sigma0_VH")
                    ):
                        continue
                return job, paths
        raise CaseStudyUnavailable("No completed preprocessing job has both real raster outputs available.")

    def _matching_osm(self, job: dict[str, Any]) -> dict[str, Any] | None:
        pair = job.get("pair") or {}
        requested_bbox = pair.get("requested_bbox_wgs84")
        snapshot_root = (self.project_root / OSM_METADATA_RELATIVE).resolve()
        prepared_root = (self.project_root / OSM_PREPARED_RELATIVE).resolve()
        if not snapshot_root.is_relative_to(self.project_root) or not prepared_root.is_relative_to(self.project_root):
            return None
        for metadata_path in sorted(snapshot_root.glob("*/snapshot_metadata.json")):
            if not metadata_path.resolve().is_relative_to(snapshot_root):
                continue
            snapshot = self._read_json_path(metadata_path)
            if not snapshot or not isinstance(snapshot.get("snapshot_id"), str):
                continue
            snapshot_id = snapshot["snapshot_id"]
            if metadata_path.parent.name != snapshot_id:
                continue
            prepared_path = (prepared_root / snapshot_id / "processing_metadata.json").resolve()
            if not prepared_path.is_relative_to(prepared_root):
                continue
            prepared = self._read_json_path(prepared_path)
            snapshot_bbox = (snapshot.get("aoi") or {}).get("bbox_wgs84")
            if (
                snapshot.get("event_date") != pair.get("event_date")
                or snapshot_bbox != requested_bbox
                or not prepared
                or prepared.get("snapshot_id") != snapshot_id
            ):
                continue
            return {
                "status": "prepared_not_intersected_with_flood",
                "snapshot_id": snapshot_id,
                "provider_snapshot_date": snapshot.get("provider_snapshot_date"),
                "temporal_verification_status": snapshot.get("temporal_verification_status"),
                "feature_counts_by_category": prepared.get("feature_counts_by_category", {}),
                "attribution": OSM_ATTRIBUTION,
                "limitations": list(prepared.get("limitations", [])) + list(snapshot.get("warnings", [])),
            }
        return None

    def _latest_model_evaluation(self) -> dict[str, Any] | None:
        """Read a generated evaluation summary without treating it as approval."""
        report_root = (self.project_root / MODEL_EVALUATION_RELATIVE).resolve()
        if not report_root.is_relative_to(self.project_root) or not report_root.is_dir():
            return None
        candidates = sorted(
            (path for path in report_root.glob("*evaluation*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            if not path.resolve().is_relative_to(report_root):
                continue
            report = self._read_json_path(path)
            if not report:
                continue
            checkpoint_sha256 = report.get("checkpoint_sha256")
            if (
                report.get("evaluation_status") not in {"completed_pending_review", "completed_approved"}
                or not isinstance(checkpoint_sha256, str)
                or len(checkpoint_sha256) != 64
            ):
                continue
            return report
        return None

    def summary(self) -> dict[str, Any]:
        job, paths = self._latest_job()
        pair = job.get("pair") or {}
        pre = pair.get("pre") or {}
        post = pair.get("post") or {}
        output_meta = job.get("outputs") or {}
        pre_output = output_meta.get("pre_event") or {}
        post_output = output_meta.get("post_event") or {}
        crs = pre_output.get("crs")
        bounds_projected: list[float] | None = None
        bbox_wgs84: list[float] | None = None
        if crs:
            with rasterio.open(paths["pre_event"]) as source:
                bounds_projected = [float(v) for v in source.bounds]
                bbox_wgs84 = [float(v) for v in transform_bounds(source.crs, "EPSG:4326", *source.bounds, densify_pts=21)]
        shared_grid = output_meta.get("common_grid") or {}
        infrastructure = self._matching_osm(job)
        evaluation = self._latest_model_evaluation()
        model_status = (
            "exploratory_checkpoint_pending_review"
            if evaluation and not evaluation.get("deployment_approved")
            else "no_deployment_approved_checkpoint"
        )
        stage = (
            "model_acceptance_and_nepal_input_validation_required"
            if evaluation
            else "model_training_and_independent_evaluation_required"
        )
        model_warnings = (
            [
                "An exploratory checkpoint has a partial-subset evaluation pending review; it is not approved for Nepal inference.",
                "No Nepal inference has run. Flood area, mask, probability and boundaries remain unavailable.",
            ]
            if evaluation
            else [
                "No deployment-approved checkpoint is configured. Flood area, mask, probability and boundaries are unavailable.",
            ]
        )
        return {
            "schema_version": "jaldrishti.case-study.v1",
            "analysis_id": job.get("job_id"),
            "analysis_status": "imagery_ready_prediction_pending",
            "stage": stage,
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "event": {
                "event_date": pair.get("event_date"),
                "aoi_bbox_wgs84": pair.get("requested_bbox_wgs84"),
            },
            "source": {
                "pre_scene_id": pre.get("scene_id"),
                "pre_acquisition_start": pre.get("acquisition_start"),
                "post_scene_id": post.get("scene_id"),
                "post_acquisition_start": post.get("acquisition_start"),
                "orbit_direction": pre.get("orbit_direction"),
                "relative_orbit": pre.get("relative_orbit"),
                "polarization": pre.get("polarization"),
            },
            "rasters": {
                "pre_event": {
                    "quicklook_url": "/api/v1/case-study/latest/rasters/pre_event.png",
                    "format": "PNG display quicklook",
                    "source_format": "GeoTIFF",
                    "source_sha256": pre_output.get("sha256"),
                    "crs": crs,
                    "bounds": bounds_projected,
                    "bounds_crs": crs,
                    "bbox_wgs84": bbox_wgs84,
                    "width": shared_grid.get("width"),
                    "height": shared_grid.get("height"),
                    "bands": pre_output.get("bands"),
                    "display_band": "Sigma0_VV",
                },
                "post_event": {
                    "quicklook_url": "/api/v1/case-study/latest/rasters/post_event.png",
                    "format": "PNG display quicklook",
                    "source_format": "GeoTIFF",
                    "source_sha256": post_output.get("sha256"),
                    "crs": crs,
                    "bounds": bounds_projected,
                    "bounds_crs": crs,
                    "bbox_wgs84": bbox_wgs84,
                    "width": shared_grid.get("width"),
                    "height": shared_grid.get("height"),
                    "bands": post_output.get("bands"),
                    "display_band": "Sigma0_VV",
                },
                "flood_mask": None,
                "flood_probability": None,
                "boundaries_geojson": None,
            },
            "model": {
                "status": model_status,
                "version": None,
                "inference_eligible": False,
                "evaluation_status": evaluation.get("evaluation_status") if evaluation else None,
                "candidate_checkpoint_sha256": evaluation.get("checkpoint_sha256") if evaluation else None,
                "deployment_approved": bool(evaluation and evaluation.get("deployment_approved")),
            },
            "results": {
                "flooded_area_km2": None,
                "flooded_pixel_count": None,
                "valid_aoi_pixel_count": shared_grid.get("aoi_pixel_count"),
                "crs": crs,
                "bounds": bounds_projected,
                "bounds_crs": crs,
                "bbox_wgs84": bbox_wgs84,
            },
            "infrastructure": infrastructure,
            "attribution": [SENTINEL_ATTRIBUTION] + ([OSM_ATTRIBUTION] if infrastructure else []),
            "warnings": [
                "The VV quicklooks are display-only grayscale stretches of real, geospatially validated SNAP outputs; they are not flood classifications.",
                *model_warnings,
                "The source rasters remain marked analysis_ready=false pending model-input/domain validation.",
            ],
        }

    def quicklook_png(self, role: str) -> bytes:
        if role not in {"pre_event", "post_event"}:
            raise CaseStudyUnavailable("Raster role must be pre_event or post_event.")
        _, paths = self._latest_job()
        arrays: dict[str, np.ndarray] = {}
        valid_masks: dict[str, np.ndarray] = {}
        for name, path in paths.items():
            with rasterio.open(path) as source:
                if source.crs is None or source.count < 2 or source.descriptions[:2] != ("Sigma0_VV", "Sigma0_VH"):
                    raise CaseStudyUnavailable("The selected raster does not expose the expected georeferenced Sigma0_VV band.")
                values = source.read(1).astype(np.float32, copy=False)
                valid = np.isfinite(values) & (values > 0)
                if not np.any(valid):
                    raise CaseStudyUnavailable("The selected raster contains no valid positive VV pixels.")
                db = np.full(values.shape, np.nan, dtype=np.float32)
                db[valid] = 10.0 * np.log10(values[valid])
                arrays[name] = db
                valid_masks[name] = valid
        combined = np.concatenate([arrays[name][valid_masks[name]] for name in ("pre_event", "post_event")])
        low, high = (float(v) for v in np.percentile(combined, (2, 98)))
        if not np.isfinite(low) or not np.isfinite(high) or high <= low:
            raise CaseStudyUnavailable("The shared VV display stretch could not be calculated from valid pixels.")
        scaled = np.zeros(arrays[role].shape, dtype=np.uint8)
        clipped = np.clip((arrays[role][valid_masks[role]] - low) / (high - low), 0, 1)
        scaled[valid_masks[role]] = np.rint(clipped * 255).astype(np.uint8)
        alpha = np.where(valid_masks[role], 255, 0).astype(np.uint8)
        return _encode_rgba_png(scaled, alpha)
