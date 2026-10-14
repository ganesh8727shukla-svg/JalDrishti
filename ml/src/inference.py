"""Approval-gated tiled inference for a validated Sentinel-1 pre/post pair.

The runner creates no output unless a reviewer has approved the exact model,
evaluation report, and raster pair, and the preprocessing manifest marks that
pair model-ready. Raw softmax scores are never described as calibrated
confidence.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any
import uuid

import numpy as np
import rasterio
import torch
from rasterio.features import shapes
from rasterio.warp import transform_bounds, transform_geom

from ml.src.dataset import (
    CLASS_NAMES,
    IGNORE_INDEX,
    LINEAR_SIGMA0_CLIP_MAX,
    POLARIZATION_MEAN,
    POLARIZATION_STD,
)
from ml.src.evaluation import _load_checkpoint_safely
from ml.src.input_contract import InputContractError, validate_sar_pair
from ml.src.model import UNet
from ml.src.tiling import iter_tiles, reconstruct_probabilities
from ml.src.training import sha256_file


APPROVAL_SCHEMA = "jaldrishti.inference-approval.v1"
INPUT_CONTRACT = "jaldrishti_s1_two_date_v1"
MAX_PIXELS_DEFAULT = 10_000_000
MAX_BOUNDARY_FEATURES = 100_000
SENTINEL_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2026."
JOB_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")


class InferenceBlocked(ValueError):
    """Inference precondition failed with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InferenceBlocked("manifest_unreadable", f"{label} is missing or invalid JSON.") from exc
    if not isinstance(value, dict):
        raise InferenceBlocked("manifest_invalid", f"{label} must contain a JSON object.")
    return value


def _project_path(project_root: Path, candidate: str | Path, label: str) -> Path:
    path = (project_root / candidate).resolve()
    if not path.is_relative_to(project_root):
        raise InferenceBlocked("unsafe_path", f"{label} must be inside the project directory.")
    return path


def _check_hash(path: Path, expected: Any, label: str) -> str:
    if not path.is_file():
        raise InferenceBlocked("source_file_missing", f"The selected {label} raster does not exist.")
    actual = sha256_file(path)
    if not isinstance(expected, str) or actual != expected.lower():
        raise InferenceBlocked("source_checksum_mismatch", f"The {label} raster does not match its preprocessing manifest checksum.")
    return actual


def _date_before_after(pair: dict[str, Any]) -> tuple[str, str, str]:
    try:
        event_date = date.fromisoformat(str(pair["event_date"]))
        pre_date = date.fromisoformat(str(pair["pre"]["acquisition_start"])[:10])
        post_date = date.fromisoformat(str(pair["post"]["acquisition_start"])[:10])
    except (KeyError, TypeError, ValueError) as exc:
        raise InferenceBlocked("pair_dates_missing", "The preprocessing manifest must contain valid event and acquisition dates.") from exc
    if not pre_date < event_date < post_date:
        raise InferenceBlocked("pair_does_not_bracket_event", "Pre/post acquisitions must strictly bracket the event date.")
    return pre_date.isoformat(), event_date.isoformat(), post_date.isoformat()


def _load_ready_pair(
    project_root: Path,
    preprocessing_manifest_path: Path,
    job_id: str,
) -> tuple[dict[str, Any], Path, Path, dict[str, str]]:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise InferenceBlocked("preprocessing_job_not_found", "A valid completed preprocessing job ID is required.")
    manifest = _read_json(preprocessing_manifest_path, "Preprocessing manifest")
    job = next((item for item in manifest.get("jobs", []) if isinstance(item, dict) and item.get("job_id") == job_id), None)
    if not job or job.get("status") != "completed":
        raise InferenceBlocked("preprocessing_job_not_ready", "The selected preprocessing job is not completed.")
    if job.get("model_input_status") != "validated_for_model":
        raise InferenceBlocked("model_input_review_required", "The preprocessing manifest has not passed the model-specific input and domain review gate.")
    pair = job.get("pair")
    outputs = job.get("outputs")
    if not isinstance(pair, dict) or not isinstance(outputs, dict):
        raise InferenceBlocked("pair_manifest_incomplete", "The preprocessing job is missing pair or output metadata.")
    if pair.get("orbit_compatibility") != "match" or pair.get("polarization_compatibility") != "match" or pair.get("common_aoi_coverage") != "full":
        raise InferenceBlocked("pair_incompatible", "The preprocessing manifest does not certify matched orbit, polarization, and full common AOI coverage.")
    _date_before_after(pair)
    raw_bbox = pair.get("requested_bbox_wgs84")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4 or not all(isinstance(item, (int, float)) and math.isfinite(item) for item in raw_bbox):
        raise InferenceBlocked("aoi_missing", "The preprocessing job must contain a finite WGS84 AOI bounding box.")
    pre_scene = pair.get("pre") or {}
    post_scene = pair.get("post") or {}
    if pre_scene.get("orbit_direction") != post_scene.get("orbit_direction") or pre_scene.get("relative_orbit") != post_scene.get("relative_orbit") or pre_scene.get("polarization") != post_scene.get("polarization"):
        raise InferenceBlocked("pair_compatibility_metadata_mismatch", "Pre/post orbit and polarization metadata must match.")

    processed_root = (project_root / "data/processed/sentinel1").resolve()
    raster_paths: dict[str, Path] = {}
    raster_hashes: dict[str, str] = {}
    for role in ("pre_event", "post_event"):
        item = outputs.get(role)
        if not isinstance(item, dict) or item.get("analysis_ready") is not True:
            raise InferenceBlocked("model_input_not_ready", f"The {role} output is not marked analysis_ready by preprocessing validation.")
        relative_path = item.get("path")
        if not isinstance(relative_path, str):
            raise InferenceBlocked("pair_manifest_incomplete", f"The {role} output path is missing.")
        raster_path = (processed_root / relative_path).resolve()
        if not raster_path.is_relative_to(processed_root) or raster_path.suffix.lower() not in {".tif", ".tiff"}:
            raise InferenceBlocked("unsafe_path", f"The {role} path is outside the configured processed-data directory.")
        raster_hashes[role] = _check_hash(raster_path, item.get("sha256"), role)
        raster_paths[role] = raster_path
    return job, raster_paths["pre_event"], raster_paths["post_event"], raster_hashes


def _verify_approval(
    *,
    approval_path: Path,
    evaluation_path: Path,
    checkpoint_path: Path,
    raster_hashes: dict[str, str],
    event_date: str,
    bbox: list[float],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    approval = _read_json(approval_path, "Approval manifest")
    evaluation = _read_json(evaluation_path, "Evaluation report")
    checkpoint_hash = sha256_file(checkpoint_path)
    evaluation_hash = sha256_file(evaluation_path)
    approval_hash = sha256_file(approval_path)
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        raise InferenceBlocked("approval_schema_invalid", f"Approval schema_version must be {APPROVAL_SCHEMA}.")
    if approval.get("deployment_approved") is not True or approval.get("inference_eligible") is not True:
        raise InferenceBlocked("approval_required", "A reviewer must explicitly approve this exact checkpoint and raster pair for inference.")
    if not str(approval.get("reviewed_by", "")).strip() or not str(approval.get("reviewed_at", "")).strip():
        raise InferenceBlocked("approval_reviewer_missing", "The approval manifest must name a reviewer and review timestamp.")
    if not str(approval.get("event_id", "")).strip():
        raise InferenceBlocked("approval_event_id_missing", "The approval manifest must name the event ID used by Phase 4 provenance.")
    if approval.get("input_contract") != INPUT_CONTRACT:
        raise InferenceBlocked("approval_contract_mismatch", "The approval manifest must explicitly accept the checkpoint's input contract.")
    expected = {
        "checkpoint_sha256": checkpoint_hash,
        "evaluation_report_sha256": evaluation_hash,
        "pre_event_sha256": raster_hashes["pre_event"],
        "post_event_sha256": raster_hashes["post_event"],
        "event_date": event_date,
        "aoi_bbox_wgs84": bbox,
    }
    for key, value in expected.items():
        if approval.get(key) != value:
            raise InferenceBlocked("approval_evidence_mismatch", f"Approval evidence does not match the current {key}.")
    if evaluation.get("deployment_approved") is not True or evaluation.get("inference_eligible") is not True:
        raise InferenceBlocked("evaluation_not_approved", "The referenced evaluation report does not approve deployment and inference.")
    if evaluation.get("checkpoint_sha256") != checkpoint_hash:
        raise InferenceBlocked("evaluation_checkpoint_mismatch", "The evaluation report does not refer to the selected checkpoint.")
    threshold = (evaluation.get("threshold_selection") or {}).get("selected_threshold")
    if (evaluation.get("threshold_selection") or {}).get("source") != "validation_only" or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise InferenceBlocked("validation_threshold_missing", "A finite flood threshold selected using validation data only is required.")
    return approval, evaluation, approval_hash


def _read_inputs(pre_path: Path, post_path: Path, max_pixels: int) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    if max_pixels < 1:
        raise InferenceBlocked("invalid_pixel_limit", "max_pixels must be positive.")
    try:
        contract = validate_sar_pair(pre_path, post_path)
    except InputContractError as exc:
        raise InferenceBlocked("input_contract_invalid", str(exc)) from exc
    pixel_count = contract["width"] * contract["height"]
    if pixel_count > max_pixels:
        raise InferenceBlocked("scene_exceeds_pixel_limit", f"Raster has {pixel_count:,} pixels; configured inference limit is {max_pixels:,}.")
    with rasterio.open(pre_path) as pre, rasterio.open(post_path) as post:
        pre_data = pre.read((1, 2), out_dtype="float32")
        post_data = post.read((1, 2), out_dtype="float32")
        raw = np.concatenate((pre_data, post_data), axis=0)
        masks = np.concatenate((pre.read_masks((1, 2)), post.read_masks((1, 2))), axis=0)
        valid = np.all(masks > 0, axis=0) & np.all(np.isfinite(raw), axis=0) & np.all(raw > 0, axis=0)
        if not np.any(valid):
            raise InferenceBlocked("no_valid_pixels", "No common finite positive VV/VH pixels are available for inference.")
        normalized = np.zeros_like(raw, dtype=np.float32)
        for index in range(4):
            polarization = index % 2
            values = np.minimum(raw[index, valid], LINEAR_SIGMA0_CLIP_MAX)
            normalized[index, valid] = (values - POLARIZATION_MEAN[polarization]) / POLARIZATION_STD[polarization]
    return contract, normalized, valid


def _predict_probabilities(checkpoint: dict[str, Any], inputs: np.ndarray, valid: np.ndarray, tile_size: int, overlap: int) -> tuple[np.ndarray, torch.device]:
    model_config = checkpoint.get("model_config") or {}
    if checkpoint.get("input_contract") != INPUT_CONTRACT or model_config.get("in_channels") != 4 or model_config.get("num_classes") != 3:
        raise InferenceBlocked("checkpoint_contract_invalid", "The checkpoint does not implement the declared four-channel, three-class model contract.")
    model = UNet(
        in_channels=4,
        num_classes=3,
        base_channels=int(model_config.get("base_channels", 16)),
    )
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (KeyError, RuntimeError, TypeError) as exc:
        raise InferenceBlocked("checkpoint_weights_invalid", "Checkpoint weights do not match the declared model architecture.") from exc
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)
    model.eval()
    labels = np.full(valid.shape, IGNORE_INDEX, dtype=np.uint8)
    tile_probabilities: list[tuple[np.ndarray, tuple[int, int]]] = []
    try:
        with torch.inference_mode():
            for tile, _ignored_labels, origin in iter_tiles(inputs, labels, tile_size=tile_size, overlap=overlap):
                tensor = torch.from_numpy(tile[None]).to(device=device, dtype=torch.float32)
                probabilities = torch.softmax(model(tensor), dim=1)[0].cpu().numpy()
                tile_probabilities.append((probabilities, origin))
    except (RuntimeError, ValueError) as exc:
        raise InferenceBlocked("inference_failed", "The model could not process the selected pair with the configured tile shape.") from exc
    probabilities, _weights = reconstruct_probabilities(
        iter(tile_probabilities),
        height=valid.shape[0],
        width=valid.shape[1],
        tile_size=tile_size,
    )
    return probabilities, device


def _write_geotiff(path: Path, array: np.ndarray, source_profile: dict[str, Any], *, dtype: str, nodata: float | int, description: str, tags: dict[str, str]) -> None:
    profile = {
        "driver": "GTiff",
        "width": array.shape[1],
        "height": array.shape[0],
        "count": 1,
        "dtype": dtype,
        "crs": source_profile["crs"],
        "transform": source_profile["transform"],
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 2 if dtype == "float32" else 1,
    }
    if array.shape[0] >= 16 and array.shape[1] >= 16:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    temporary_path = path.with_suffix(path.suffix + ".part")
    try:
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            with rasterio.open(temporary_path, "w", **profile) as target:
                target.write(array, 1)
                target.set_band_description(1, description)
                target.update_tags(**tags)
                if np.issubdtype(array.dtype, np.floating) and isinstance(nodata, float) and math.isnan(nodata):
                    valid_mask = np.isfinite(array)
                else:
                    valid_mask = array != nodata
                target.write_mask(np.where(valid_mask, 255, 0).astype(np.uint8))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _boundaries_geojson(mask: np.ndarray, transform, source_crs, *, max_features: int = MAX_BOUNDARY_FEATURES) -> dict[str, Any]:
    if max_features < 1:
        raise InferenceBlocked("boundary_feature_limit_invalid", "max boundary features must be positive.")
    features = []
    for geometry, class_value in shapes(mask, mask=mask == 1, transform=transform, connectivity=8):
        if int(class_value) != 1:
            continue
        if len(features) >= max_features:
            raise InferenceBlocked("boundary_feature_limit_exceeded", f"Flood mask exceeds the configured vectorization limit of {max_features:,} polygons.")
        geographic = transform_geom(source_crs, "EPSG:4326", geometry, precision=7)
        features.append({"type": "Feature", "properties": {"class_id": 1, "class_name": "flood"}, "geometry": geographic})
    return {"type": "FeatureCollection", "features": features}


def run_inference(
    *,
    project_root: Path,
    preprocessing_manifest_path: Path,
    job_id: str,
    checkpoint_path: Path,
    evaluation_report_path: Path,
    approval_path: Path,
    output_root: Path,
    tile_size: int = 256,
    overlap: int = 32,
    max_pixels: int = MAX_PIXELS_DEFAULT,
    analysis_id: str | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    manifest_path = _project_path(project_root, preprocessing_manifest_path, "Preprocessing manifest")
    checkpoint = _project_path(project_root, checkpoint_path, "Checkpoint")
    evaluation_path = _project_path(project_root, evaluation_report_path, "Evaluation report")
    approval_manifest_path = _project_path(project_root, approval_path, "Approval manifest")
    target_root = _project_path(project_root, output_root, "Output directory")
    job, pre_path, post_path, raster_hashes = _load_ready_pair(project_root, manifest_path, job_id)
    pair = job["pair"]
    _pre_date, event_date, _post_date = _date_before_after(pair)
    bbox = [float(item) for item in pair["requested_bbox_wgs84"]]
    if not checkpoint.is_file() or not evaluation_path.is_file() or not approval_manifest_path.is_file():
        raise InferenceBlocked("authorization_artifact_missing", "Checkpoint, evaluation report, and explicit approval manifest are all required.")
    approval, evaluation, approval_hash = _verify_approval(
        approval_path=approval_manifest_path,
        evaluation_path=evaluation_path,
        checkpoint_path=checkpoint,
        raster_hashes=raster_hashes,
        event_date=event_date,
        bbox=bbox,
    )
    if tile_size < 16 or overlap < 0 or overlap >= tile_size:
        raise InferenceBlocked("tile_configuration_invalid", "tile_size must be at least 16 and overlap must be in [0, tile_size).")
    checkpoint_data = _load_checkpoint_safely(checkpoint)
    if checkpoint_data.get("checkpoint_sha256") and checkpoint_data["checkpoint_sha256"] != sha256_file(checkpoint):
        raise InferenceBlocked("checkpoint_checksum_mismatch", "Checkpoint self-reported checksum does not match the selected file.")
    contract, inputs, valid = _read_inputs(pre_path, post_path, max_pixels)
    threshold = float(evaluation["threshold_selection"]["selected_threshold"])
    probabilities, device = _predict_probabilities(checkpoint_data, inputs, valid, tile_size, overlap)
    score = probabilities[2]
    background_or_water = probabilities[:2].argmax(axis=0).astype(np.uint8)
    class_mask = np.full(valid.shape, 255, dtype=np.uint8)
    class_mask[valid] = background_or_water[valid]
    class_mask[valid & (score >= threshold)] = 2
    flood_mask = np.full(valid.shape, 255, dtype=np.uint8)
    flood_mask[valid] = 0
    flood_mask[valid & (score >= threshold)] = 1
    score_output = np.full(valid.shape, np.nan, dtype=np.float32)
    score_output[valid] = score[valid]

    identifier = analysis_id or str(uuid.uuid4())
    if not JOB_ID_PATTERN.fullmatch(identifier):
        raise InferenceBlocked("analysis_id_invalid", "analysis_id must be a UUID.")
    final_dir = target_root / identifier
    if final_dir.exists():
        raise InferenceBlocked("analysis_output_exists", "Refusing to overwrite an existing inference output directory.")
    target_root.mkdir(parents=True, exist_ok=True)
    staging = target_root / f".{identifier}.{uuid.uuid4().hex}.part"
    staging.mkdir(exist_ok=False)
    try:
        with rasterio.open(pre_path) as source:
            profile = source.profile.copy()
            crs = source.crs
            transform = source.transform
            bounds = [float(item) for item in source.bounds]
            if crs is None or not crs.is_projected:
                raise InferenceBlocked("projected_crs_required", "Flooded area requires a projected raster CRS.")
            unit_name, unit_to_metre = crs.linear_units_factor
            if not math.isfinite(float(unit_to_metre)) or float(unit_to_metre) <= 0:
                raise InferenceBlocked("projected_crs_units_invalid", "Could not establish projected CRS unit conversion for area calculation.")
            pixel_area_m2 = abs(transform.a * transform.e - transform.b * transform.d) * float(unit_to_metre) ** 2
            if pixel_area_m2 <= 0 or not math.isfinite(pixel_area_m2):
                raise InferenceBlocked("pixel_area_invalid", "Raster transform does not define a positive pixel area.")
            _write_geotiff(
                staging / "segmentation_classes.tif", class_mask, profile, dtype="uint8", nodata=255,
                description="Class mask: 0=no_water, 1=permanent_water, 2=flood, 255=NoData",
                tags={"MODEL_SHA256": sha256_file(checkpoint), "FLOOD_THRESHOLD": str(threshold), "ANALYSIS_READY": "false"},
            )
            _write_geotiff(
                staging / "flood_mask.tif", flood_mask, profile, dtype="uint8", nodata=255,
                description="Binary flood mask: 0=non_flood, 1=flood, 255=NoData",
                tags={"MODEL_SHA256": sha256_file(checkpoint), "FLOOD_THRESHOLD": str(threshold), "MASK_STATUS": "pending_independent_validation"},
            )
            _write_geotiff(
                staging / "flood_score_uncalibrated.tif", score_output, profile, dtype="float32", nodata=float("nan"),
                description="Uncalibrated softmax flood score; not calibrated confidence",
                tags={"MODEL_SHA256": sha256_file(checkpoint), "SCORE_STATUS": "uncalibrated_softmax", "FLOOD_THRESHOLD": str(threshold)},
            )
        boundaries = _boundaries_geojson(flood_mask, transform, crs)
        (staging / "boundaries.geojson").write_text(json.dumps(boundaries, separators=(",", ":")) + "\n", encoding="utf-8")
        flood_pixels = int(np.count_nonzero(flood_mask == 1))
        valid_pixels = int(np.count_nonzero(valid))
        event_id = str(approval["event_id"])
        if not event_id.strip():
            raise InferenceBlocked("approval_event_id_missing", "The approved event ID must not be empty.")
        with rasterio.open(staging / "flood_mask.tif") as mask_dataset:
            mask_transform = list(mask_dataset.transform)[:6]
            mask_width = mask_dataset.width
            mask_height = mask_dataset.height
            mask_nodata = int(mask_dataset.nodata)
        mask_manifest = {
            "schema_version": "jaldrishti.phase4-flood-mask.v1",
            "synthetic": False,
            "artifact_type": "flood_mask",
            "source_type": "model_inference",
            "analysis_readiness": {
                "status": "pending_independent_validation",
                "acceptance_policy_id": None,
                "independent_validation_id": None,
            },
            "event_id": event_id,
            "event_date": event_date,
            "inference_run_id": identifier,
            "model_checkpoint_sha256": sha256_file(checkpoint),
            "preprocessing_version": str((job.get("software") or {}).get("snap", "SNAP version not recorded")),
            "mask_semantics": {"flood_value": 1, "non_flood_value": 0, "nodata_value": 255},
            "width": mask_width,
            "height": mask_height,
            "transform": mask_transform,
            "crs": crs.to_string(),
            "nodata": mask_nodata,
            "valid_data_fraction": valid_pixels / int(valid.size),
            "aoi_bbox_wgs84": bbox,
            "artifact_sha256": sha256_file(staging / "flood_mask.tif"),
            "artifact_path": "flood_mask.tif",
            "evaluation_report_sha256": sha256_file(evaluation_path),
            "approval_sha256": approval_hash,
            "warnings": [
                "The model output awaits independent flood-map validation and explicit Phase 4 acceptance.",
                "The paired SNAP data are used without additional SAR preprocessing by this inference command.",
            ],
        }
        (staging / "phase4_flood_mask_manifest.json").write_text(json.dumps(mask_manifest, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": "jaldrishti.inference-output.v1",
            "analysis_id": identifier,
            "status": "inference_completed_pending_independent_validation",
            "stage": "independent_prediction_validation_required",
            "event_id": event_id,
            "event_date": event_date,
            "aoi_bbox_wgs84": bbox,
            "source": {
                "preprocessing_job_id": job_id,
                "pre_scene_id": (pair.get("pre") or {}).get("scene_id"),
                "post_scene_id": (pair.get("post") or {}).get("scene_id"),
                "pre_sha256": raster_hashes["pre_event"],
                "post_sha256": raster_hashes["post_event"],
            },
            "model": {
                "architecture": (checkpoint_data.get("model_config") or {}).get("architecture"),
                "checkpoint_sha256": sha256_file(checkpoint),
                "evaluation_report_sha256": sha256_file(evaluation_path),
                "approval_sha256": approval_hash,
                "reviewed_by": approval["reviewed_by"],
                "reviewed_at": approval["reviewed_at"],
                "input_contract": INPUT_CONTRACT,
                "flood_threshold": threshold,
                "threshold_source": "validation_only",
                "probability_calibrated": False,
            },
            "results": {
                "flooded_pixel_count": flood_pixels,
                "valid_aoi_pixel_count": valid_pixels,
                "flooded_area_km2": flood_pixels * pixel_area_m2 / 1_000_000,
                "crs": crs.to_string(),
                "bounds": bounds,
                "bounds_crs": crs.to_string(),
                "bbox_wgs84": [float(item) for item in transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21)],
                "pixel_area_m2": pixel_area_m2,
            },
            "rasters": {
                "flood_mask": {"path": f"data/processed/inference/{identifier}/flood_mask.tif", "sha256": sha256_file(staging / "flood_mask.tif"), "format": "GeoTIFF", "dtype": "uint8", "class_values": {"0": "non_flood", "1": "flood", "255": "nodata"}, "nodata": 255},
                "segmentation_classes": {"path": f"data/processed/inference/{identifier}/segmentation_classes.tif", "sha256": sha256_file(staging / "segmentation_classes.tif"), "format": "GeoTIFF", "dtype": "uint8", "class_values": {"0": CLASS_NAMES[0], "1": CLASS_NAMES[1], "2": CLASS_NAMES[2], "255": "nodata"}, "nodata": 255},
                "flood_score": {"path": f"data/processed/inference/{identifier}/flood_score_uncalibrated.tif", "sha256": sha256_file(staging / "flood_score_uncalibrated.tif"), "format": "GeoTIFF", "dtype": "float32", "status": "uncalibrated_softmax_score", "nodata": "NaN"},
                "boundaries_geojson": {"path": f"data/processed/inference/{identifier}/boundaries.geojson", "sha256": sha256_file(staging / "boundaries.geojson"), "format": "GeoJSON", "crs": "EPSG:4326", "feature_count": len(boundaries["features"])},
                "phase4_mask_manifest": {"path": f"data/processed/inference/{identifier}/phase4_flood_mask_manifest.json", "status": "pending_independent_validation"},
            },
            "input_validation": contract,
            "attribution": SENTINEL_ATTRIBUTION,
            "warnings": [
                "This inference artifact is pending independent validation and must not be used as a validated flood map until that review passes.",
                "The softmax flood score is uncalibrated and is not confidence.",
                "No SAR preprocessing is performed by this command.",
            ],
        }
        (staging / "analysis_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, final_dir)
        manifest["output_directory"] = f"data/processed/inference/{identifier}"
        manifest["device"] = str(device)
        return manifest
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preprocessing-manifest", type=Path, default=Path("data/manifests/sentinel1_preprocessing/manifest.json"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--evaluation-report", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/processed/inference"))
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--overlap", type=int, default=32)
    parser.add_argument("--max-pixels", type=int, default=MAX_PIXELS_DEFAULT)
    parser.add_argument("--analysis-id")
    args = parser.parse_args()
    try:
        result = run_inference(
            project_root=Path.cwd(),
            preprocessing_manifest_path=args.preprocessing_manifest,
            job_id=args.job_id,
            checkpoint_path=args.checkpoint,
            evaluation_report_path=args.evaluation_report,
            approval_path=args.approval,
            output_root=args.output_root,
            tile_size=args.tile_size,
            overlap=args.overlap,
            max_pixels=args.max_pixels,
            analysis_id=args.analysis_id,
        )
    except InferenceBlocked as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "message": exc.message}), file=sys.stderr)
        return 2
    except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
        print(json.dumps({"status": "failed", "code": "inference_failed", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
