from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import rasterio
import torch
from rasterio.transform import from_origin

from ml.src.inference import InferenceBlocked, _boundaries_geojson, run_inference
from ml.src.model import UNet
from geospatial.infrastructure import InfrastructureError, validate_flood_mask


JOB_ID = "12345678-1234-4234-9234-123456789abc"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class InferenceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pre_path = self.root / "data/processed/sentinel1" / JOB_ID / "pre.tif"
        self.post_path = self.root / "data/processed/sentinel1" / JOB_ID / "post.tif"
        self.pre_path.parent.mkdir(parents=True)
        self._write_input(self.pre_path, 0.08)
        self._write_input(self.post_path, 0.09)
        self.manifest_path = Path("data/manifests/sentinel1_preprocessing/manifest.json")
        manifest_abs = self.root / self.manifest_path
        manifest_abs.parent.mkdir(parents=True)
        manifest_abs.write_text(json.dumps({"jobs": [self._job()]}), encoding="utf-8")
        self.checkpoint_path = Path("ml/checkpoints/test-candidate.pt")
        (self.root / self.checkpoint_path).parent.mkdir(parents=True)
        torch.manual_seed(5)
        self.checkpoint = {
            "checkpoint_schema": 1,
            "model_state_dict": UNet(base_channels=4).state_dict(),
            "model_config": {"architecture": "jaldrishti_unet_v1", "in_channels": 4, "num_classes": 3, "base_channels": 4},
            "input_contract": "jaldrishti_s1_two_date_v1",
        }
        torch.save(self.checkpoint, self.root / self.checkpoint_path)
        self.eval_path = Path("reports/generated/test-evaluation.json")
        (self.root / self.eval_path).parent.mkdir(parents=True)
        eval_report = {
            "evaluation_status": "completed_approved",
            "deployment_approved": True,
            "inference_eligible": True,
            "checkpoint_sha256": _sha256(self.root / self.checkpoint_path),
            "threshold_selection": {"source": "validation_only", "selected_threshold": 0.3},
        }
        (self.root / self.eval_path).write_text(json.dumps(eval_report), encoding="utf-8")
        self.approval_path = Path("reports/generated/test-approval.json")
        self._write_approval()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_input(path: Path, value: float) -> None:
        array = np.full((32, 32), value, dtype=np.float32)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=32,
            height=32,
            count=2,
            dtype="float32",
            crs="EPSG:32645",
            transform=from_origin(500000, 3100000, 10, 10),
            nodata=np.nan,
        ) as output:
            output.write(array, 1)
            output.write(array * 0.3, 2)
            output.set_band_description(1, "Sigma0_VV")
            output.set_band_description(2, "Sigma0_VH")
            output.update_tags(BACKSCATTER_UNITS="linear_sigma0_power")

    def _job(self) -> dict:
        return {
            "job_id": JOB_ID,
            "status": "completed",
            "model_input_status": "validated_for_model",
            "pair": {
                "event_id": "synthetic-test-event",
                "event_date": "2026-08-26",
                "requested_bbox_wgs84": [84.04, 28.2, 84.15, 28.31],
                "orbit_compatibility": "match",
                "polarization_compatibility": "match",
                "common_aoi_coverage": "full",
                "pre": {"scene_id": "PRE", "acquisition_start": "2026-08-21T12:00:00Z", "orbit_direction": "ascending", "relative_orbit": 158, "polarization": ["VV", "VH"]},
                "post": {"scene_id": "POST", "acquisition_start": "2026-09-02T12:00:00Z", "orbit_direction": "ascending", "relative_orbit": 158, "polarization": ["VV", "VH"]},
            },
            "outputs": {
                "pre_event": {"path": f"{JOB_ID}/pre.tif", "sha256": _sha256(self.pre_path), "analysis_ready": True},
                "post_event": {"path": f"{JOB_ID}/post.tif", "sha256": _sha256(self.post_path), "analysis_ready": True},
            },
        }

    def _write_approval(self) -> None:
        approval = {
            "schema_version": "jaldrishti.inference-approval.v1",
            "deployment_approved": True,
            "inference_eligible": True,
            "reviewed_by": "Synthetic test reviewer",
            "reviewed_at": "2026-10-14T00:00:00Z",
            "event_id": "synthetic-test-event",
            "input_contract": "jaldrishti_s1_two_date_v1",
            "checkpoint_sha256": _sha256(self.root / self.checkpoint_path),
            "evaluation_report_sha256": _sha256(self.root / self.eval_path),
            "pre_event_sha256": _sha256(self.pre_path),
            "post_event_sha256": _sha256(self.post_path),
            "event_date": "2026-08-26",
            "aoi_bbox_wgs84": [84.04, 28.2, 84.15, 28.31],
        }
        path = self.root / self.approval_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(approval), encoding="utf-8")

    def _run(self) -> dict:
        return run_inference(
            project_root=self.root,
            preprocessing_manifest_path=self.manifest_path,
            job_id=JOB_ID,
            checkpoint_path=self.checkpoint_path,
            evaluation_report_path=self.eval_path,
            approval_path=self.approval_path,
            output_root=Path("data/processed/inference"),
            tile_size=16,
            overlap=4,
            max_pixels=4096,
            analysis_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    def test_synthetic_approved_case_produces_georeferenced_mask_and_manifest(self) -> None:
        result = self._run()
        self.assertEqual(result["status"], "inference_completed_pending_independent_validation")
        self.assertFalse(result["model"]["probability_calibrated"])
        self.assertEqual(result["results"]["valid_aoi_pixel_count"], 1024)
        output = self.root / "data/processed/inference/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with rasterio.open(output / "flood_mask.tif") as mask:
            self.assertEqual(mask.crs.to_string(), "EPSG:32645")
            self.assertEqual((mask.width, mask.height, mask.count), (32, 32, 1))
            self.assertEqual(mask.nodata, 255)
            self.assertTrue(set(np.unique(mask.read(1))).issubset({0, 1}))
        with rasterio.open(output / "flood_score_uncalibrated.tif") as score:
            self.assertEqual(score.tags()["SCORE_STATUS"], "uncalibrated_softmax")
            self.assertFalse((output / "flood_score_uncalibrated.tif.msk").exists())
        self.assertTrue((output / "boundaries.geojson").is_file())
        boundaries = json.loads((output / "boundaries.geojson").read_text())
        self.assertEqual(boundaries["type"], "FeatureCollection")
        self.assertNotIn("crs", boundaries)
        self.assertTrue((output / "analysis_manifest.json").is_file())
        phase4_manifest = output / "phase4_flood_mask_manifest.json"
        self.assertEqual(json.loads(phase4_manifest.read_text())["analysis_readiness"]["status"], "pending_independent_validation")
        with self.assertRaises(InfrastructureError) as caught:
            validate_flood_mask(
                phase4_manifest,
                event_id="synthetic-test-event",
                event_date="2026-08-26",
                aoi_bbox_wgs84=[84.04, 28.2, 84.15, 28.31],
            )
        self.assertEqual(caught.exception.code, "flood_mask_not_ready")

    def test_boundary_polygonization_has_a_hard_feature_limit(self) -> None:
        mask = np.array([[1, 0, 1]], dtype=np.uint8)
        with self.assertRaises(InferenceBlocked) as caught:
            _boundaries_geojson(mask, from_origin(0, 2, 1, 1), "EPSG:4326", max_features=1)
        self.assertEqual(caught.exception.code, "boundary_feature_limit_exceeded")

    def test_mismatched_review_approval_blocks_and_writes_no_output(self) -> None:
        approval_path = self.root / self.approval_path
        approval = json.loads(approval_path.read_text())
        approval["pre_event_sha256"] = "0" * 64
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        with self.assertRaisesRegex(InferenceBlocked, "does not match the current pre_event_sha256"):
            self._run()
        self.assertFalse((self.root / "data/processed/inference").exists())

    def test_not_model_ready_pair_is_blocked_even_with_approval_files(self) -> None:
        manifest_path = self.root / self.manifest_path
        manifest = json.loads(manifest_path.read_text())
        manifest["jobs"][0]["model_input_status"] = "blocked_unknown_model_contract"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(InferenceBlocked, "has not passed the model-specific"):
            self._run()
        self.assertFalse((self.root / "data/processed/inference").exists())


if __name__ == "__main__":
    unittest.main()
