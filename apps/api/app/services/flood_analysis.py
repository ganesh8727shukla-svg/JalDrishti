"""Small local job store and approval-gated adapter for Sentinel flood inference."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
import uuid

import rasterio
from rasterio.warp import transform_bounds

from ..core.config import PROJECT_ROOT
from ..schemas.analysis import FloodAnalysisRequest


JOB_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")
ANALYSIS_MANIFEST_RELATIVE = Path("data/manifests/analyses")
INFERENCE_OUTPUT_RELATIVE = Path("data/processed/inference")
FLOOD_MASK_READINESS_RELATIVE = Path("phase4_flood_mask_manifest.json")
ARTIFACTS = {
    "flood_mask.tif": ("rasters", "flood_mask", "image/tiff"),
    "boundaries.geojson": ("rasters", "boundaries_geojson", "application/geo+json"),
}


class FloodAnalysisError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 404) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FloodAnalysisService:
    """Runs inference in FastAPI's existing background-task pattern.

    Model/evaluation/approval paths come only from server configuration. Clients
    can select a preprocessing job and safe numeric tiling parameters, never a
    URL or filesystem path. Inference remains blocked until all model/data gates
    pass. Artifacts are served only after the Phase 4 mask acceptance gate.
    """

    _lock = threading.RLock()

    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root.resolve()
        self.manifest_dir = (self.project_root / ANALYSIS_MANIFEST_RELATIVE).resolve()
        self.output_root = (self.project_root / INFERENCE_OUTPUT_RELATIVE).resolve()
        if not self.manifest_dir.is_relative_to(self.project_root) or not self.output_root.is_relative_to(self.project_root):
            raise ValueError("Analysis storage paths must remain inside the project directory.")
        self.preprocessing_manifest = self.project_root / "data/manifests/sentinel1_preprocessing/manifest.json"
        self.checkpoint = self._configured_path(
            "JALDRISHTI_MODEL_CHECKPOINT", "ml/checkpoints/jaldrishti-unet-kuro-siwo-20261014.pt"
        )
        self.evaluation_report = self._configured_path(
            "JALDRISHTI_MODEL_EVALUATION", "reports/generated/jaldrishti-evaluation-20261014.json"
        )
        self.approval_manifest = self._configured_path(
            "JALDRISHTI_INFERENCE_APPROVAL", "ml/approvals/inference-approval.json"
        )
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        if not self.manifest_dir.is_dir():
            return
        for path in sorted(self.manifest_dir.glob("*.json")):
            if not JOB_ID_PATTERN.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(job, dict) or job.get("status") not in {"queued", "running"}:
                continue
            job.update(
                status="failed",
                stage="worker_interrupted",
                updated_at=_now(),
                error={"code": "worker_interrupted", "message": "The API process stopped during this analysis. Submit the same selected preprocessing job again to retry."},
                warnings=["No validated flood result was produced."],
            )
            try:
                self._write_job(job)
            except OSError:
                continue

    def _configured_path(self, variable: str, default: str) -> Path:
        raw = os.getenv(variable, default).strip()
        path = Path(raw).expanduser()
        resolved = (self.project_root / path).resolve() if not path.is_absolute() else path.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ValueError(f"{variable} must point inside the project directory.")
        return resolved

    def _job_path(self, analysis_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(analysis_id):
            raise FloodAnalysisError("analysis_not_found", "The flood analysis job was not found.")
        path = (self.manifest_dir / f"{analysis_id}.json").resolve()
        if not path.is_relative_to(self.manifest_dir):
            raise FloodAnalysisError("analysis_not_found", "The flood analysis job was not found.")
        return path

    def _read_job(self, analysis_id: str) -> dict[str, Any]:
        path = self._job_path(analysis_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FloodAnalysisError("analysis_not_found", "The flood analysis job was not found.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise FloodAnalysisError("analysis_manifest_invalid", "The flood analysis job record is unreadable.", 500) from exc
        if not isinstance(value, dict):
            raise FloodAnalysisError("analysis_manifest_invalid", "The flood analysis job record is invalid.", 500)
        return value

    def _write_job(self, payload: dict[str, Any], *, create: bool = False) -> None:
        path = self._job_path(str(payload["analysis_id"]))
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        if create and path.exists():
            raise FloodAnalysisError("analysis_id_collision", "Could not allocate a unique analysis ID.", 500)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=self.manifest_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if create:
                # A hard link provides create-only semantics without replacing an
                # existing job in the rare event of an ID collision.
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def create_job(self, request: FloodAnalysisRequest) -> dict[str, Any]:
        now = _now()
        analysis_id = str(uuid.uuid4())
        payload = {
            "schema_version": "jaldrishti.analysis-job.v1",
            "analysis_id": analysis_id,
            "status": "queued",
            "stage": "waiting_for_worker",
            "created_at": now,
            "updated_at": now,
            "preprocessing_job_id": str(request.preprocessing_job_id),
            "parameters": {
                "tile_size": request.tile_size,
                "overlap": request.overlap,
                "max_pixels": request.max_pixels,
            },
            "event_id": None,
            "event_date": None,
            "model_version": None,
            "model_checkpoint_sha256": None,
            "source": {
                "preprocessing_job_id": str(request.preprocessing_job_id),
                "pre_scene_id": None,
                "post_scene_id": None,
                "pre_sha256": None,
                "post_sha256": None,
            },
            "results": {
                "flooded_area_km2": None,
                "flooded_pixel_count": None,
                "valid_aoi_pixel_count": None,
                "crs": None,
                "bounds": None,
                "bounds_crs": None,
                "bbox_wgs84": None,
            },
            "rasters": {"flood_mask": None, "flood_probability": None},
            "boundaries": None,
            "warnings": ["Flood results remain unavailable until the model and input pair pass their review gates."],
            "error": None,
        }
        self._write_job(payload, create=True)
        return payload

    def run_job(self, analysis_id: str) -> None:
        with self._lock:
            job = self._read_job(analysis_id)
            if job.get("status") != "queued":
                return
            job.update(status="running", stage="validating_model_and_pair", updated_at=_now())
            self._write_job(job)
        try:
            # Import ML runtime lazily so the observation/download API remains
            # usable when optional PyTorch dependencies are not installed.
            from ml.src.inference import run_inference

            result = run_inference(
                project_root=self.project_root,
                preprocessing_manifest_path=self.preprocessing_manifest,
                job_id=job["preprocessing_job_id"],
                checkpoint_path=self.checkpoint,
                evaluation_report_path=self.evaluation_report,
                approval_path=self.approval_manifest,
                output_root=self.output_root,
                tile_size=job["parameters"]["tile_size"],
                overlap=job["parameters"]["overlap"],
                max_pixels=job["parameters"]["max_pixels"],
                analysis_id=analysis_id,
            )
        except Exception as exc:
            with self._lock:
                job = self._read_job(analysis_id)
                now = _now()
                if type(exc).__name__ == "InferenceBlocked":
                    job.update(status="blocked", stage=getattr(exc, "code", "inference_blocked"), error={"code": getattr(exc, "code", "inference_blocked"), "message": getattr(exc, "message", "Inference preconditions were not satisfied.")})
                else:
                    # Do not include stack traces, credentials, or arbitrary
                    # exception text in persisted API responses.
                    job.update(status="failed", stage="inference_failed", error={"code": "inference_failed", "message": f"Inference failed ({type(exc).__name__}). Check server logs for the local diagnostic."})
                job["updated_at"] = now
                job["warnings"] = ["No validated flood result was produced."]
                self._write_job(job)
            return
        with self._lock:
            job = self._read_job(analysis_id)
            job.update(
                status="pending_independent_validation",
                stage="independent_prediction_validation_required",
                updated_at=_now(),
                event_id=result.get("event_id"),
                event_date=result.get("event_date"),
                aoi_bbox_wgs84=result.get("aoi_bbox_wgs84"),
                model_version=(result.get("model") or {}).get("architecture"),
                model_checkpoint_sha256=(result.get("model") or {}).get("checkpoint_sha256"),
                source={
                    "preprocessing_job_id": job["preprocessing_job_id"],
                    **(result.get("source") or {}),
                },
                results={
                    "flooded_area_km2": None,
                    "flooded_pixel_count": None,
                    "valid_aoi_pixel_count": None,
                    "crs": result.get("results", {}).get("crs"),
                    "bounds": result.get("results", {}).get("bounds"),
                    "bounds_crs": result.get("results", {}).get("bounds_crs"),
                    "bbox_wgs84": result.get("results", {}).get("bbox_wgs84"),
                },
                candidate_results={
                    "flooded_area_km2": result.get("results", {}).get("flooded_area_km2"),
                    "flooded_pixel_count": result.get("results", {}).get("flooded_pixel_count"),
                },
                candidate_artifacts={
                    "output_directory": result.get("output_directory"),
                    "phase4_mask_manifest": (result.get("rasters") or {}).get("phase4_mask_manifest"),
                },
                warnings=list(result.get("warnings", [])),
            )
            job["warnings"].append("Candidate outputs are withheld until the Phase 4 independent-validation and acceptance gate passes.")
            self._write_job(job)

    def _accepted_mask(self, job: dict[str, Any]) -> dict[str, Any] | None:
        candidate = job.get("candidate_artifacts") or {}
        output_directory = candidate.get("output_directory")
        if not isinstance(output_directory, str):
            return None
        output_dir = (self.project_root / output_directory).resolve()
        if not output_dir.is_relative_to(self.output_root) or output_dir.name != job.get("analysis_id"):
            return None
        mask_manifest = (output_dir / FLOOD_MASK_READINESS_RELATIVE).resolve()
        try:
            mask_manifest.relative_to(output_dir)
            from geospatial.infrastructure import validate_flood_mask

            accepted = validate_flood_mask(
                mask_manifest,
                event_id=str(job.get("event_id")),
                event_date=str(job.get("event_date")),
                aoi_bbox_wgs84=job.get("aoi_bbox_wgs84"),
            )
        except Exception:
            return None
        return accepted

    def get_status(self, analysis_id: str) -> dict[str, Any]:
        job = self._read_job(analysis_id)
        # Results are only promoted into the frontend contract if the same
        # Phase 4 fail-closed validator accepts the exact inference mask.
        accepted = self._accepted_mask(job) if job.get("status") == "pending_independent_validation" else None
        response = {key: value for key, value in job.items() if key not in {"parameters", "candidate_artifacts", "candidate_results", "schema_version"}}
        if accepted:
            mask_path = Path(accepted["artifact_path"])
            output_dir = mask_path.parent
            analysis_manifest_path = output_dir / "analysis_manifest.json"
            try:
                analysis_manifest = json.loads(analysis_manifest_path.read_text(encoding="utf-8"))
                boundaries_entry = (analysis_manifest.get("rasters") or {}).get("boundaries_geojson") or {}
                boundaries_path = (output_dir / "boundaries.geojson").resolve()
                if not boundaries_path.is_relative_to(output_dir) or _sha256(boundaries_path) != boundaries_entry.get("sha256"):
                    accepted = None
            except (OSError, json.JSONDecodeError):
                accepted = None
        if accepted:
            with rasterio.open(accepted["artifact_path"]) as raster:
                array = raster.read(1)
                flood_value = int(accepted["manifest"]["mask_semantics"]["flood_value"])
                flood_pixels = int((array == flood_value).sum())
                pixel_area = abs(raster.transform.a * raster.transform.e - raster.transform.b * raster.transform.d)
                if raster.crs and raster.crs.is_projected:
                    pixel_area *= float(raster.crs.linear_units_factor[1]) ** 2
                    raster_crs = raster.crs.to_string()
                    raster_bounds = [float(value) for value in raster.bounds]
                    raster_bbox_wgs84 = [float(value) for value in transform_bounds(raster.crs, "EPSG:4326", *raster.bounds, densify_pts=21)]
                    valid_pixels = int((raster.read_masks(1) > 0).sum())
                    raster_nodata = raster.nodata
                else:
                    accepted = None
            if accepted:
                response.update(
                    status="completed",
                    stage="analysis_ready",
                    results={
                        "flooded_area_km2": flood_pixels * pixel_area / 1_000_000,
                        "flooded_pixel_count": flood_pixels,
                        "valid_aoi_pixel_count": valid_pixels,
                        "crs": raster_crs,
                        "bounds": raster_bounds,
                        "bounds_crs": raster_crs,
                        "bbox_wgs84": raster_bbox_wgs84,
                    },
                    rasters={
                        "flood_mask": {"url": f"/api/v1/analyses/{analysis_id}/artifacts/flood_mask.tif", "format": "GeoTIFF", "crs": raster_crs, "bounds": raster_bounds, "bounds_crs": raster_crs, "bbox_wgs84": raster_bbox_wgs84, "nodata": raster_nodata, "sha256": accepted["manifest"]["artifact_sha256"]},
                        "flood_probability": None,
                    },
                    boundaries={"url": f"/api/v1/analyses/{analysis_id}/artifacts/boundaries.geojson", "format": "GeoJSON", "crs": "EPSG:4326"},
                    warnings=["Flood area is model-predicted spatial extent, not confirmed damage or depth.", "No calibrated flood probability is available; the softmax score is withheld."],
                )
        return response

    def get_artifact(self, analysis_id: str, artifact_name: str) -> tuple[Path, str]:
        if artifact_name not in ARTIFACTS:
            raise FloodAnalysisError("artifact_not_found", "The requested analysis artifact was not found.")
        job = self._read_job(analysis_id)
        accepted = self._accepted_mask(job) if job.get("status") == "pending_independent_validation" else None
        if not accepted:
            raise FloodAnalysisError("artifact_not_validated", "Analysis artifacts are unavailable until the Phase 4 mask validation and acceptance gate passes.", 409)
        artifact = Path(accepted["artifact_path"]).resolve()
        output_dir = (self.output_root / analysis_id).resolve()
        if not artifact.is_relative_to(output_dir):
            raise FloodAnalysisError("artifact_not_found", "The requested analysis artifact was not found.")
        if artifact_name == "flood_mask.tif":
            if _sha256(artifact) != accepted["manifest"].get("artifact_sha256"):
                raise FloodAnalysisError("artifact_integrity_failed", "The flood-mask artifact failed its recorded checksum.", 409)
        else:
            manifest = json.loads((output_dir / "analysis_manifest.json").read_text(encoding="utf-8"))
            expected = ((manifest.get("rasters") or {}).get("boundaries_geojson") or {}).get("sha256")
            artifact = (output_dir / "boundaries.geojson").resolve()
            if not artifact.is_relative_to(output_dir) or not artifact.is_file() or _sha256(artifact) != expected:
                raise FloodAnalysisError("artifact_integrity_failed", "The boundary artifact failed its recorded checksum.", 409)
        return artifact, ARTIFACTS[artifact_name][2]
