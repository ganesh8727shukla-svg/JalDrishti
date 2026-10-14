from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from geospatial.infrastructure import InfrastructureError, validate_flood_mask
from ml.src.independent_validation import (
    MaskValidationError,
    accept_validation_report,
    align_reference_to_prediction,
    validate_against_reference,
)


class IndependentMaskValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "data/processed/inference/analysis-1"
        self.output_dir.mkdir(parents=True)
        self.validation_dir = self.root / "data/validation/event-1"
        self.validation_dir.mkdir(parents=True)
        self.prediction_path = self.output_dir / "flood_mask.tif"
        self.reference_path = self.validation_dir / "reference.tif"
        self.transform = from_origin(500000, 3100020, 10, 10)
        self.crs = "EPSG:32645"
        self.prediction = np.array([[1, 0], [1, 0]], dtype=np.uint8)
        self.reference = np.array([[1, 0], [0, 0]], dtype=np.uint8)
        self._write_mask(self.prediction_path, self.prediction)
        self._write_mask(self.reference_path, self.reference)
        with rasterio.open(self.prediction_path) as raster:
            bounds = transform_bounds(raster.crs, "EPSG:4326", *raster.bounds)
        inset_x = (bounds[2] - bounds[0]) * 0.05
        inset_y = (bounds[3] - bounds[1]) * 0.05
        self.bbox = [bounds[0] + inset_x, bounds[1] + inset_y, bounds[2] - inset_x, bounds[3] - inset_y]
        self.mask_manifest_path = self.output_dir / "phase4_flood_mask_manifest.json"
        self.mask_manifest_path.write_text(json.dumps({
            "schema_version": "jaldrishti.phase4-flood-mask.v1",
            "synthetic": False,
            "artifact_type": "flood_mask",
            "source_type": "model_inference",
            "analysis_readiness": {"status": "pending_independent_validation"},
            "event_id": "event-1",
            "event_date": "2026-08-26",
            "inference_run_id": "analysis-1",
            "model_checkpoint_sha256": "a" * 64,
            "preprocessing_version": "test-snap",
            "mask_semantics": {"flood_value": 1, "non_flood_value": 0, "nodata_value": 255},
            "width": 2,
            "height": 2,
            "transform": list(self.transform)[:6],
            "crs": self.crs,
            "nodata": 255,
            "valid_data_fraction": 1.0,
            "aoi_bbox_wgs84": self.bbox,
            "artifact_sha256": self._sha256(self.prediction_path),
            "artifact_path": self.prediction_path.name,
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_mask(path: Path, values: np.ndarray) -> None:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=1,
            dtype="uint8",
            crs="EPSG:32645",
            transform=from_origin(500000, 3100020, 10, 10),
            nodata=255,
        ) as raster:
            raster.write(values, 1)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _make_report(self) -> Path:
        aligned_path = self.validation_dir / "reference_aligned.tif"
        if not aligned_path.exists():
            align_reference_to_prediction(
                project_root=self.root,
                reference_raster_path=self.reference_path,
                mask_manifest_path=self.mask_manifest_path,
                output_path=aligned_path,
                source_flood_value=1,
                source_non_flood_values=[0],
                source_nodata_value=255,
            )
        report_path = self.root / "reports/generated/validation-report.json"
        validate_against_reference(
            project_root=self.root,
            mask_manifest_path=self.mask_manifest_path,
            reference_raster_path=aligned_path,
            output_path=report_path,
            event_id="event-1",
            event_date="2026-08-26",
            aoi_bbox_wgs84=tuple(self.bbox),
            reference_name="Independent fixture reference",
            reference_url="https://example.org/reference",
            reference_license="fixture-only",
            reference_flood_value=1,
            attest_independent_of_training_data=True,
            attest_not_used_as_prediction_input=True,
        )
        return report_path

    def test_metrics_are_computed_and_report_remains_pending_review(self) -> None:
        report_path = self._make_report()
        report = json.loads(report_path.read_text())
        self.assertEqual(report["validation_status"], "completed_pending_review")
        self.assertEqual(report["confusion_counts"], {
            "true_positive": 1,
            "false_positive": 1,
            "false_negative": 0,
            "true_negative": 2,
        })
        self.assertAlmostEqual(report["metrics"]["flood_iou"], 0.5)
        self.assertEqual(report["reference"]["use"], "validation_only")
        with self.assertRaises(InfrastructureError):
            validate_flood_mask(self.mask_manifest_path, event_id="event-1", event_date="2026-08-26", aoi_bbox_wgs84=self.bbox)

    def test_grid_mismatch_is_rejected_instead_of_silently_resampled(self) -> None:
        aligned_path = self.validation_dir / "reference_aligned.tif"
        align_reference_to_prediction(
            project_root=self.root,
            reference_raster_path=self.reference_path,
            mask_manifest_path=self.mask_manifest_path,
            output_path=aligned_path,
            source_flood_value=1,
            source_non_flood_values=[0],
            source_nodata_value=255,
        )
        shifted = np.array([[1, 0], [0, 0]], dtype=np.uint8)
        with rasterio.open(
            aligned_path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=1,
            dtype="uint8",
            crs=self.crs,
            transform=from_origin(500001, 3100020, 10, 10),
            nodata=255,
        ) as raster:
            raster.write(shifted, 1)
        sidecar_path = aligned_path.with_suffix(aligned_path.suffix + ".alignment.json")
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["aligned_reference_sha256"] = self._sha256(aligned_path)
        sidecar_path.write_text(json.dumps(sidecar))
        with self.assertRaises(MaskValidationError) as caught:
            self._make_report()
        self.assertEqual(caught.exception.code, "reference_grid_mismatch")

    def test_alignment_sidecar_must_bind_reference_to_exact_prediction_mask(self) -> None:
        aligned_path = self.validation_dir / "reference_aligned.tif"
        align_reference_to_prediction(
            project_root=self.root,
            reference_raster_path=self.reference_path,
            mask_manifest_path=self.mask_manifest_path,
            output_path=aligned_path,
            source_flood_value=1,
            source_non_flood_values=[0],
            source_nodata_value=255,
        )
        sidecar_path = aligned_path.with_suffix(aligned_path.suffix + ".alignment.json")
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["target_prediction_mask_sha256"] = "0" * 64
        sidecar_path.write_text(json.dumps(sidecar))
        with self.assertRaises(MaskValidationError) as caught:
            self._make_report()
        self.assertEqual(caught.exception.code, "reference_alignment_invalid")

    def test_alignment_creates_separate_nearest_neighbor_copy_and_hash_sidecar(self) -> None:
        output = self.validation_dir / "reference_aligned.tif"
        original_hash = self._sha256(self.reference_path)

        sidecar = align_reference_to_prediction(
            project_root=self.root,
            reference_raster_path=self.reference_path,
            mask_manifest_path=self.mask_manifest_path,
            output_path=output,
            source_flood_value=1,
            source_non_flood_values=[0],
            source_nodata_value=255,
        )

        self.assertTrue(output.is_file())
        self.assertEqual(self._sha256(self.reference_path), original_hash)
        self.assertEqual(sidecar["resampling"], "nearest")
        self.assertEqual(sidecar["source_reference_sha256"], original_hash)
        with rasterio.open(output) as aligned:
            np.testing.assert_array_equal(aligned.read(1), self.reference)
            self.assertEqual(aligned.transform, self.transform)

    def test_explicit_review_and_thresholds_link_report_and_enable_phase4_gate(self) -> None:
        report_path = self._make_report()
        accepted = accept_validation_report(
            project_root=self.root,
            mask_manifest_path=self.mask_manifest_path,
            report_path=report_path,
            reviewed_by="Test reviewer",
            acceptance_policy_id="test-policy-v1",
            minimum_flood_iou=0.5,
            minimum_flood_recall=1.0,
            minimum_valid_coverage=1.0,
        )
        self.assertEqual(accepted["status"], "approved")
        validated = validate_flood_mask(
            self.mask_manifest_path,
            event_id="event-1",
            event_date="2026-08-26",
            aoi_bbox_wgs84=self.bbox,
        )
        self.assertTrue(validated["analysis_ready"])
        self.assertEqual(validated["manifest"]["analysis_readiness"]["acceptance_policy_id"], "test-policy-v1")
        aligned_path = self.validation_dir / "reference_aligned.tif"
        sidecar_path = aligned_path.with_suffix(aligned_path.suffix + ".alignment.json")
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["source_reference_sha256"] = "0" * 64
        sidecar_path.write_text(json.dumps(sidecar))
        with self.assertRaises(InfrastructureError) as caught:
            validate_flood_mask(
                self.mask_manifest_path,
                event_id="event-1",
                event_date="2026-08-26",
                aoi_bbox_wgs84=self.bbox,
            )
        self.assertEqual(caught.exception.code, "independent_reference_alignment_invalid")

    def test_threshold_failure_does_not_approve_mask(self) -> None:
        report_path = self._make_report()
        with self.assertRaises(MaskValidationError) as caught:
            accept_validation_report(
                project_root=self.root,
                mask_manifest_path=self.mask_manifest_path,
                report_path=report_path,
                reviewed_by="Test reviewer",
                acceptance_policy_id="test-policy-v1",
                minimum_flood_iou=0.9,
                minimum_flood_recall=0.9,
                minimum_valid_coverage=1.0,
            )
        self.assertEqual(caught.exception.code, "acceptance_threshold_not_met")


if __name__ == "__main__":
    unittest.main()
