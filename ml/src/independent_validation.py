"""Compare a real inference mask with a separate validation-only reference.

The first command records metrics but never approves the mask. A reviewer must
apply an explicit acceptance policy in the second command before Phase 4 can
consume the raster. Reference rasters must be preserved under data/validation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit
import uuid

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject, transform_bounds, transform_geom
from shapely.geometry import box

from geospatial.infrastructure import InfrastructureError, validate_bbox, validate_flood_mask


VALIDATION_SCHEMA = "jaldrishti.independent-mask-validation.v1"


class MaskValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def align_reference_to_prediction(
    *,
    project_root: Path,
    reference_raster_path: Path,
    mask_manifest_path: Path,
    output_path: Path,
    source_flood_value: int,
    source_non_flood_values: list[int],
    source_nodata_value: int | None = None,
) -> dict[str, Any]:
    """Create a nearest-neighbour validation copy on the prediction grid."""
    root = project_root.resolve()
    reference_path = reference_raster_path.resolve()
    manifest_path = mask_manifest_path.resolve()
    output_path = output_path.resolve()
    validation_root = (root / "data/validation").resolve()
    if not reference_path.is_relative_to(validation_root) or not reference_path.is_file():
        raise MaskValidationError("validation_data_location_invalid", "Original reference data must be preserved under data/validation.")
    if not output_path.is_relative_to(validation_root) or output_path == reference_path:
        raise MaskValidationError("validation_output_path_invalid", "Aligned reference copy must be a separate file under data/validation.")
    if not manifest_path.is_relative_to(root):
        raise MaskValidationError("unsafe_path", "Flood-mask manifest must be inside the project directory.")
    if not source_non_flood_values or source_flood_value in source_non_flood_values:
        raise MaskValidationError("reference_encoding_invalid", "Specify one or more non-flood values distinct from the flood value.")
    manifest = _read_json(manifest_path, "Flood-mask manifest")
    if manifest.get("synthetic") is not False or manifest.get("artifact_type") != "flood_mask" or manifest.get("source_type") != "model_inference":
        raise MaskValidationError("prediction_mask_invalid", "Alignment target must be a real model-inference flood mask.")
    target_candidate = Path(str(manifest.get("artifact_path", "")))
    target_path = target_candidate if target_candidate.is_absolute() else manifest_path.parent / target_candidate
    target_path = target_path.resolve()
    if not target_path.is_relative_to(manifest_path.parent) or not target_path.is_file() or _sha256(target_path) != manifest.get("artifact_sha256"):
        raise MaskValidationError("prediction_mask_checksum_mismatch", "Prediction mask is missing or its checksum does not match its manifest.")
    if output_path.exists():
        raise MaskValidationError("output_exists", f"Refusing to overwrite aligned reference: {output_path}")

    temporary_raster = output_path.with_suffix(output_path.suffix + ".part")
    try:
        with rasterio.open(target_path) as target, rasterio.open(reference_path) as source:
            if target.crs is None or source.crs is None or source.count != 1 or not np.issubdtype(np.dtype(source.dtypes[0]), np.integer):
                raise MaskValidationError("reference_metadata_invalid", "Reference must be a single-band integer categorical raster with a CRS.")
            target_width, target_height = target.width, target.height
            target_transform, target_crs = target.transform, target.crs
            source_nodata = source_nodata_value if source_nodata_value is not None else source.nodata
            warped = np.full((target_height, target_width), 255, dtype=np.int32)
            reproject(
                source=rasterio.band(source, 1),
                destination=warped,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source_nodata,
                dst_transform=target_transform,
                dst_crs=target_crs,
                dst_nodata=255,
                resampling=Resampling.nearest,
                init_dest_nodata=True,
            )
            source_values = set(int(value) for value in np.unique(warped) if int(value) != 255)
            allowed = {int(source_flood_value), *(int(value) for value in source_non_flood_values)}
            if not source_values or not source_values.issubset(allowed):
                raise MaskValidationError("reference_class_encoding_invalid", "Reference contains valid class values not listed by the explicit class mapping.")
            aligned = np.full(warped.shape, 255, dtype=np.uint8)
            valid = warped != 255
            aligned[valid & np.isin(warped, source_non_flood_values)] = 0
            aligned[valid & (warped == source_flood_value)] = 1
            profile = {
                "driver": "GTiff", "width": target_width, "height": target_height,
                "count": 1, "dtype": "uint8", "crs": target_crs,
                "transform": target_transform, "nodata": 255, "compress": "deflate",
            }
            if target_width >= 16 and target_height >= 16:
                profile.update(tiled=True, blockxsize=256, blockysize=256)
            with rasterio.open(temporary_raster, "w", **profile) as aligned_raster:
                aligned_raster.write(aligned, 1)
                aligned_raster.set_band_description(1, "Reference classes: 0=non_flood, 1=flood, 255=NoData")
                aligned_raster.update_tags(
                    SOURCE_REFERENCE_SHA256=_sha256(reference_path),
                    PREDICTION_GRID_MASK_SHA256=manifest["artifact_sha256"],
                    RESAMPLING="nearest",
                    REFERENCE_USE="validation_only",
                )
        os.replace(temporary_raster, output_path)
    finally:
        temporary_raster.unlink(missing_ok=True)
    sidecar = {
        "schema_version": "jaldrishti.reference-alignment.v1",
        "source_reference_path": str(reference_path.relative_to(root)),
        "source_reference_sha256": _sha256(reference_path),
        "aligned_reference_path": str(output_path.relative_to(root)),
        "aligned_reference_sha256": _sha256(output_path),
        "target_prediction_mask_sha256": manifest["artifact_sha256"],
        "resampling": "nearest",
        "class_mapping": {
            "source_flood_value": int(source_flood_value),
            "source_non_flood_values": [int(value) for value in source_non_flood_values],
            "output_flood_value": 1,
            "output_non_flood_value": 0,
            "output_nodata_value": 255,
        },
        "use": "validation_only",
    }
    _write_json_atomic(output_path.with_suffix(output_path.suffix + ".alignment.json"), sidecar)
    return sidecar


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaskValidationError("manifest_invalid", f"{label} is missing or invalid JSON.") from exc
    if not isinstance(value, dict):
        raise MaskValidationError("manifest_invalid", f"{label} must be a JSON object.")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any], *, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise MaskValidationError("output_exists", f"Refusing to overwrite existing report: {path}")
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_against_reference(
    *,
    project_root: Path,
    mask_manifest_path: Path,
    reference_raster_path: Path,
    output_path: Path,
    event_id: str,
    event_date: str,
    aoi_bbox_wgs84: tuple[float, float, float, float],
    reference_name: str,
    reference_url: str,
    reference_license: str,
    reference_flood_value: int,
    reference_non_flood_value: int = 0,
    reference_nodata_value: int | None = None,
    attest_independent_of_training_data: bool = False,
    attest_not_used_as_prediction_input: bool = False,
) -> dict[str, Any]:
    root = project_root.resolve()
    manifest_path = mask_manifest_path.resolve()
    reference_path = reference_raster_path.resolve()
    output_path = output_path.resolve()
    validation_root = (root / "data/validation").resolve()
    for path, label in ((manifest_path, "Mask manifest"), (reference_path, "Reference raster"), (output_path, "Validation report")):
        if not path.is_relative_to(root):
            raise MaskValidationError("unsafe_path", f"{label} must be inside the project directory.")
    if not reference_path.is_relative_to(validation_root):
        raise MaskValidationError("validation_data_location_invalid", "Keep independent reference data under data/validation, separate from prediction and training inputs.")
    if reference_path == manifest_path:
        raise MaskValidationError("invalid_reference", "The independent reference cannot be the mask manifest.")
    if not attest_independent_of_training_data or not attest_not_used_as_prediction_input:
        raise MaskValidationError("independence_attestation_required", "Explicitly attest that the reference was excluded from training and prediction inputs.")
    parsed_url = urlsplit(reference_url.strip())
    if not reference_name.strip() or not reference_license.strip() or parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password or parsed_url.query:
        raise MaskValidationError("reference_provenance_missing", "Reference name, license, and HTTPS source URL are required.")
    try:
        bounds = validate_bbox(aoi_bbox_wgs84)
    except InfrastructureError as exc:
        raise MaskValidationError(exc.code, exc.message) from exc
    mask_manifest = _read_json(manifest_path, "Flood-mask manifest")
    if mask_manifest.get("synthetic") is not False or mask_manifest.get("artifact_type") != "flood_mask" or mask_manifest.get("source_type") != "model_inference":
        raise MaskValidationError("prediction_mask_invalid", "Validation input must be a real model-inference flood mask.")
    if str(mask_manifest.get("event_id")) != str(event_id) or str(mask_manifest.get("event_date")) != str(event_date):
        raise MaskValidationError("event_mismatch", "Flood-mask manifest event does not match this validation request.")
    if validate_bbox(mask_manifest.get("aoi_bbox_wgs84", [])) != bounds:
        raise MaskValidationError("aoi_mismatch", "Flood-mask manifest AOI does not match this validation request.")
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError as exc:
        raise MaskValidationError("invalid_event_date", "event_date must be YYYY-MM-DD.") from exc
    if not event_id.strip():
        raise MaskValidationError("event_metadata_invalid", "event_id and event_date are required.")

    artifact_candidate = Path(str(mask_manifest.get("artifact_path", "")))
    prediction_path = artifact_candidate if artifact_candidate.is_absolute() else (manifest_path.parent / artifact_candidate)
    prediction_path = prediction_path.resolve()
    if not prediction_path.is_relative_to(manifest_path.parent) or not prediction_path.is_file():
        raise MaskValidationError("prediction_mask_missing", "Flood-mask artifact must exist beside the mask manifest.")
    if _sha256(prediction_path) != mask_manifest.get("artifact_sha256"):
        raise MaskValidationError("prediction_mask_checksum_mismatch", "Flood-mask artifact SHA-256 does not match its manifest.")
    if not reference_path.is_file():
        raise MaskValidationError("reference_raster_missing", "Independent validation reference raster does not exist.")

    alignment_path = reference_path.with_suffix(reference_path.suffix + ".alignment.json")
    if not alignment_path.is_file():
        raise MaskValidationError("reference_alignment_missing", "Align the independent categorical reference with align-reference before scoring it.")
    alignment = _read_json(alignment_path, "Reference alignment sidecar")
    aligned_relative = reference_path.relative_to(root).as_posix()
    if (
        alignment.get("schema_version") != "jaldrishti.reference-alignment.v1"
        or alignment.get("aligned_reference_path") != aligned_relative
        or alignment.get("aligned_reference_sha256") != _sha256(reference_path)
        or alignment.get("target_prediction_mask_sha256") != mask_manifest.get("artifact_sha256")
        or alignment.get("resampling") != "nearest"
        or alignment.get("use") != "validation_only"
    ):
        raise MaskValidationError("reference_alignment_invalid", "Reference alignment sidecar is missing, modified, or bound to a different prediction mask.")
    source_candidate = Path(str(alignment.get("source_reference_path", "")))
    if source_candidate.is_absolute() or ".." in source_candidate.parts:
        raise MaskValidationError("reference_alignment_invalid", "Alignment sidecar contains an unsafe original-reference path.")
    source_reference = (root / source_candidate).resolve()
    if (
        not source_reference.is_relative_to(validation_root)
        or not source_reference.is_file()
        or alignment.get("source_reference_sha256") != _sha256(source_reference)
    ):
        raise MaskValidationError("reference_alignment_invalid", "The original validation reference is missing or its checksum does not match the alignment sidecar.")
    class_mapping = alignment.get("class_mapping") or {}
    if (
        class_mapping.get("output_flood_value") != reference_flood_value
        or class_mapping.get("output_non_flood_value") != reference_non_flood_value
        or class_mapping.get("output_nodata_value") != 255
    ):
        raise MaskValidationError("reference_alignment_invalid", "The aligned reference class mapping does not match the validation request.")

    semantics = mask_manifest.get("mask_semantics") or {}
    try:
        pred_flood = int(semantics["flood_value"])
        pred_non_flood = int(semantics["non_flood_value"])
        pred_nodata = int(semantics["nodata_value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MaskValidationError("mask_semantics_invalid", "Flood mask must declare flood, non-flood, and NoData integer values.") from exc
    if reference_flood_value == reference_non_flood_value:
        raise MaskValidationError("reference_encoding_invalid", "Reference flood and non-flood class values must differ.")

    counts = {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}
    total_pixels = 0
    comparable_pixels = 0
    grid_metadata: dict[str, Any] = {}
    try:
        with rasterio.open(prediction_path) as prediction, rasterio.open(reference_path) as reference:
            if prediction.count != 1 or reference.count != 1 or prediction.crs is None or reference.crs is None:
                raise MaskValidationError("raster_metadata_invalid", "Prediction and reference must be single-band georeferenced rasters.")
            if not np.issubdtype(np.dtype(prediction.dtypes[0]), np.integer) or not np.issubdtype(np.dtype(reference.dtypes[0]), np.integer):
                raise MaskValidationError("raster_class_encoding_invalid", "Prediction and reference class rasters must use integer data types.")
            if (prediction.crs, prediction.transform, prediction.width, prediction.height) != (reference.crs, reference.transform, reference.width, reference.height):
                raise MaskValidationError("reference_grid_mismatch", "Prediction and reference must have identical CRS, transform, and dimensions; align a copy of the validation reference explicitly before scoring.")
            raster_bbox = transform_bounds(prediction.crs, "EPSG:4326", *prediction.bounds, densify_pts=21)
            if not (raster_bbox[0] <= bounds[0] and raster_bbox[1] <= bounds[1] and raster_bbox[2] >= bounds[2] and raster_bbox[3] >= bounds[3]):
                raise MaskValidationError("aoi_coverage_invalid", "Prediction/reference grid does not cover the requested AOI.")
            grid_metadata = {"crs": prediction.crs.to_string(), "width": prediction.width, "height": prediction.height}
            aoi_in_raster_crs = transform_geom("EPSG:4326", prediction.crs, box(*bounds).__geo_interface__)
            aoi_pixels = geometry_mask(
                [aoi_in_raster_crs],
                out_shape=(prediction.height, prediction.width),
                transform=prediction.transform,
                invert=True,
            )
            for _, window in prediction.block_windows(1):
                predicted = prediction.read(1, window=window)
                reference_values = reference.read(1, window=window)
                row = int(window.row_off)
                col = int(window.col_off)
                in_aoi = aoi_pixels[row:row + int(window.height), col:col + int(window.width)]
                total_pixels += int(in_aoi.sum())
                pred_valid = (prediction.read_masks(1, window=window) > 0) & (predicted != pred_nodata)
                reference_valid = reference.read_masks(1, window=window) > 0
                nodata_value = reference_nodata_value if reference_nodata_value is not None else reference.nodata
                if nodata_value is not None:
                    reference_valid &= reference_values != nodata_value
                valid = pred_valid & reference_valid & in_aoi
                if not valid.any():
                    continue
                pred_values = set(int(value) for value in np.unique(predicted[valid]))
                ref_values = set(int(value) for value in np.unique(reference_values[valid]))
                if not pred_values.issubset({pred_flood, pred_non_flood}) or not ref_values.issubset({reference_flood_value, reference_non_flood_value}):
                    raise MaskValidationError("raster_class_encoding_invalid", "Prediction or reference contains unexpected valid class values.")
                predicted_flood = predicted[valid] == pred_flood
                reference_flood = reference_values[valid] == reference_flood_value
                counts["true_positive"] += int(np.count_nonzero(predicted_flood & reference_flood))
                counts["false_positive"] += int(np.count_nonzero(predicted_flood & ~reference_flood))
                counts["false_negative"] += int(np.count_nonzero(~predicted_flood & reference_flood))
                counts["true_negative"] += int(np.count_nonzero(~predicted_flood & ~reference_flood))
                comparable_pixels += int(valid.sum())
    except rasterio.errors.RasterioError as exc:
        raise MaskValidationError("raster_unreadable", "Prediction or validation reference raster is corrupt or unreadable.") from exc
    if comparable_pixels == 0:
        raise MaskValidationError("no_comparable_pixels", "No valid overlapping pixels are available for independent validation.")

    tp, fp, fn, tn = (counts[key] for key in ("true_positive", "false_positive", "false_negative", "true_negative"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    iou = tp / (tp + fp + fn) if tp + fp + fn else 0.0
    reference_sha256 = _sha256(reference_path)
    report = {
        "schema_version": VALIDATION_SCHEMA,
        "validation_id": str(uuid.uuid4()),
        "validation_status": "completed_pending_review",
        "event_id": event_id,
        "event_date": event_date,
        "aoi_bbox_wgs84": list(bounds),
        "prediction_mask_sha256": mask_manifest["artifact_sha256"],
        "prediction_mask_manifest_sha256": _sha256(manifest_path),
        "reference": {
            "name": reference_name.strip(),
            "source_url": reference_url.strip(),
            "license": reference_license.strip(),
            "sha256": reference_sha256,
            "path": str(reference_path.relative_to(root)),
            "alignment_sidecar_path": str(alignment_path.relative_to(root)),
            "alignment_sidecar_sha256": _sha256(alignment_path),
            "original_source_path": str(source_reference.relative_to(root)),
            "original_source_sha256": alignment["source_reference_sha256"],
            "use": "validation_only",
            "independent_of_training_data": True,
            "used_as_prediction_input": False,
        },
        "grid": {
            **grid_metadata,
            "comparable_pixel_count": comparable_pixels,
            "total_pixel_count": total_pixels,
        },
        "confusion_counts": counts,
        "metrics": {
            "flood_iou": iou,
            "flood_f1": f1,
            "flood_precision": precision,
            "flood_recall": recall,
            "valid_pixel_coverage": comparable_pixels / total_pixels,
            "accuracy": (tp + tn) / comparable_pixels,
        },
        "limitations": [
            "Metrics are valid only for the cited reference map, its classes, date, and overlapping valid pixels.",
            "Independent validation does not establish map completeness or physical flood depth.",
            "This report records measurements only; a human acceptance decision is still required.",
        ],
    }
    _write_json_atomic(output_path, report)
    return report


def accept_validation_report(
    *,
    project_root: Path,
    mask_manifest_path: Path,
    report_path: Path,
    reviewed_by: str,
    acceptance_policy_id: str,
    minimum_flood_iou: float,
    minimum_flood_recall: float,
    minimum_valid_coverage: float,
) -> dict[str, Any]:
    root = project_root.resolve()
    manifest_path = mask_manifest_path.resolve()
    source_report_path = report_path.resolve()
    for path, label in ((manifest_path, "Mask manifest"), (source_report_path, "Validation report")):
        if not path.is_relative_to(root):
            raise MaskValidationError("unsafe_path", f"{label} must be inside the project directory.")
    report = _read_json(source_report_path, "Independent validation report")
    manifest = _read_json(manifest_path, "Flood-mask manifest")
    if report.get("schema_version") != VALIDATION_SCHEMA or report.get("validation_status") != "completed_pending_review":
        raise MaskValidationError("validation_report_not_reviewable", "Only a completed pending-review report can be accepted.")
    if report.get("prediction_mask_sha256") != manifest.get("artifact_sha256") or report.get("event_id") != manifest.get("event_id") or report.get("event_date") != manifest.get("event_date"):
        raise MaskValidationError("validation_evidence_mismatch", "The validation report does not refer to this exact flood mask and event.")
    if not reviewed_by.strip() or not acceptance_policy_id.strip():
        raise MaskValidationError("review_metadata_missing", "Reviewer and acceptance policy ID are required.")
    thresholds = {
        "minimum_flood_iou": minimum_flood_iou,
        "minimum_flood_recall": minimum_flood_recall,
        "minimum_valid_coverage": minimum_valid_coverage,
    }
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in thresholds.values()):
        raise MaskValidationError("acceptance_threshold_invalid", "Acceptance thresholds must be finite values from 0 to 1.")
    metrics = report.get("metrics") or {}
    actual = {"minimum_flood_iou": metrics.get("flood_iou"), "minimum_flood_recall": metrics.get("flood_recall"), "minimum_valid_coverage": metrics.get("valid_pixel_coverage")}
    failures = [name for name, threshold in thresholds.items() if not isinstance(actual.get(name), (int, float)) or actual[name] < threshold]
    if failures:
        raise MaskValidationError("acceptance_threshold_not_met", "The report does not meet policy thresholds: " + ", ".join(failures))

    reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    report["validation_status"] = "passed"
    report["review"] = {"reviewed_by": reviewed_by.strip(), "reviewed_at": reviewed_at}
    report["acceptance"] = {"policy_id": acceptance_policy_id.strip(), "thresholds": thresholds, "decision": "approve"}
    stored_report = manifest_path.parent / "independent_validation.json"
    if stored_report.resolve() == source_report_path:
        raise MaskValidationError("output_conflict", "Store the report under a distinct validation report path before accepting it.")
    if stored_report.exists():
        raise MaskValidationError("output_exists", "An independent validation report is already linked to this flood mask; refusing to overwrite it.")
    _write_json_atomic(stored_report, report)
    readiness = dict(manifest.get("analysis_readiness") or {})
    readiness.update({
        "status": "approved",
        "acceptance_policy_id": acceptance_policy_id.strip(),
        "independent_validation_id": report["validation_id"],
        "independent_validation_report_path": stored_report.name,
        "independent_validation_report_sha256": _sha256(stored_report),
    })
    manifest["analysis_readiness"] = readiness
    _write_json_atomic(manifest_path, manifest, overwrite=True)
    try:
        accepted = validate_flood_mask(
            manifest_path,
            event_id=str(manifest.get("event_id")),
            event_date=str(manifest.get("event_date")),
            aoi_bbox_wgs84=manifest.get("aoi_bbox_wgs84"),
        )
    except Exception:
        manifest["analysis_readiness"] = {"status": "pending_independent_validation"}
        _write_json_atomic(manifest_path, manifest, overwrite=True)
        raise
    return {
        "status": "approved",
        "acceptance_policy_id": acceptance_policy_id.strip(),
        "independent_validation_id": report["validation_id"],
        "flood_mask_sha256": accepted["manifest"]["artifact_sha256"],
        "validated_raster": accepted["raster"],
        "warnings": ["Acceptance confirms the recorded policy checks and independent reference comparison; it does not imply physical damage or flood depth."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    align = commands.add_parser("align-reference", help="Create a categorical nearest-neighbour copy on the candidate-mask grid.")
    align.add_argument("--reference-raster", type=Path, required=True)
    align.add_argument("--mask-manifest", type=Path, required=True)
    align.add_argument("--output", type=Path, required=True)
    align.add_argument("--source-flood-value", type=int, required=True)
    align.add_argument("--source-non-flood-values", type=int, nargs="+", required=True)
    align.add_argument("--source-nodata-value", type=int)

    validate = commands.add_parser("validate-mask", help="Compare an inference mask to a separate aligned validation raster.")
    validate.add_argument("--mask-manifest", type=Path, required=True)
    validate.add_argument("--reference-raster", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--event-id", required=True)
    validate.add_argument("--event-date", required=True)
    validate.add_argument("--bbox", type=float, nargs=4, required=True)
    validate.add_argument("--reference-name", required=True)
    validate.add_argument("--reference-url", required=True)
    validate.add_argument("--reference-license", required=True)
    validate.add_argument("--reference-flood-value", type=int, required=True)
    validate.add_argument("--reference-non-flood-value", type=int, default=0)
    validate.add_argument("--reference-nodata-value", type=int)
    validate.add_argument("--attest-independent-of-training-data", action="store_true", required=True)
    validate.add_argument("--attest-not-used-as-prediction-input", action="store_true", required=True)

    accept = commands.add_parser("accept-mask", help="Apply a human-reviewed policy to a validation report and enable Phase 4 use.")
    accept.add_argument("--mask-manifest", type=Path, required=True)
    accept.add_argument("--report", type=Path, required=True)
    accept.add_argument("--reviewed-by", required=True)
    accept.add_argument("--acceptance-policy-id", required=True)
    accept.add_argument("--minimum-flood-iou", type=float, required=True)
    accept.add_argument("--minimum-flood-recall", type=float, required=True)
    accept.add_argument("--minimum-valid-coverage", type=float, required=True)

    args = parser.parse_args()
    try:
        if args.command == "align-reference":
            result = align_reference_to_prediction(
                project_root=Path.cwd(), reference_raster_path=args.reference_raster,
                mask_manifest_path=args.mask_manifest, output_path=args.output,
                source_flood_value=args.source_flood_value,
                source_non_flood_values=args.source_non_flood_values,
                source_nodata_value=args.source_nodata_value,
            )
        elif args.command == "validate-mask":
            result = validate_against_reference(
                project_root=Path.cwd(), mask_manifest_path=args.mask_manifest,
                reference_raster_path=args.reference_raster, output_path=args.output,
                event_id=args.event_id, event_date=args.event_date, aoi_bbox_wgs84=tuple(args.bbox),
                reference_name=args.reference_name, reference_url=args.reference_url,
                reference_license=args.reference_license, reference_flood_value=args.reference_flood_value,
                reference_non_flood_value=args.reference_non_flood_value,
                reference_nodata_value=args.reference_nodata_value,
                attest_independent_of_training_data=args.attest_independent_of_training_data,
                attest_not_used_as_prediction_input=args.attest_not_used_as_prediction_input,
            )
        else:
            result = accept_validation_report(
                project_root=Path.cwd(), mask_manifest_path=args.mask_manifest,
                report_path=args.report, reviewed_by=args.reviewed_by,
                acceptance_policy_id=args.acceptance_policy_id,
                minimum_flood_iou=args.minimum_flood_iou,
                minimum_flood_recall=args.minimum_flood_recall,
                minimum_valid_coverage=args.minimum_valid_coverage,
            )
        print(json.dumps(result, indent=2))
        return 0
    except (MaskValidationError, InfrastructureError) as exc:
        print(json.dumps({"status": "blocked", "code": getattr(exc, "code", "validation_failed"), "message": getattr(exc, "message", str(exc))}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
