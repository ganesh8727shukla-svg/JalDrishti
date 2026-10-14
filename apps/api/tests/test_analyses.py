import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from fastapi.testclient import TestClient
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from apps.api.app.api.v1 import analyses as analyses_api
from apps.api.app.main import app
from apps.api.app.schemas.analysis import FloodAnalysisRequest
from apps.api.app.services.flood_analysis import FloodAnalysisError, FloodAnalysisService
from ml.src.independent_validation import accept_validation_report, align_reference_to_prediction, validate_against_reference


PREPROCESSING_JOB_ID = "12345678-1234-4234-9234-123456789abc"


class FloodAnalysisServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        prep_dir = self.root / "data/manifests/sentinel1_preprocessing"
        prep_dir.mkdir(parents=True)
        (prep_dir / "manifest.json").write_text(json.dumps({
            "jobs": [{
                "job_id": PREPROCESSING_JOB_ID,
                "status": "completed",
                "model_input_status": "blocked_unknown_model_contract",
            }]
        }), encoding="utf-8")
        self.service = FloodAnalysisService(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_job_is_persisted_then_truthfully_blocked_by_input_readiness(self) -> None:
        job = self.service.create_job(FloodAnalysisRequest(preprocessing_job_id=UUID(PREPROCESSING_JOB_ID)))
        job_file = self.root / "data/manifests/analyses" / f"{job['analysis_id']}.json"
        self.assertTrue(job_file.is_file())
        self.service.run_job(job["analysis_id"])
        status = self.service.get_status(job["analysis_id"])
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["error"]["code"], "model_input_review_required")
        self.assertIsNone(status["results"]["flooded_area_km2"])
        self.assertIsNone(status["rasters"]["flood_mask"])

    def test_status_rejects_unknown_analysis(self) -> None:
        with self.assertRaises(FloodAnalysisError) as caught:
            self.service.get_status("12345678-1234-4234-9234-123456789abc")
        self.assertEqual(caught.exception.code, "analysis_not_found")

    def test_recovery_marks_jobs_interrupted_by_api_restart(self) -> None:
        job = self.service.create_job(FloodAnalysisRequest(preprocessing_job_id=UUID(PREPROCESSING_JOB_ID)))
        path = self.root / "data/manifests/analyses" / f"{job['analysis_id']}.json"
        stored = json.loads(path.read_text())
        stored.update(status="running", stage="inference")
        path.write_text(json.dumps(stored), encoding="utf-8")

        recovered = FloodAnalysisService(self.root).get_status(job["analysis_id"])

        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"]["code"], "worker_interrupted")

    def test_status_promotes_results_and_artifacts_only_after_acceptance(self) -> None:
        job = self.service.create_job(FloodAnalysisRequest(preprocessing_job_id=UUID(PREPROCESSING_JOB_ID)))
        analysis_id = job["analysis_id"]
        output_dir = self.root / "data/processed/inference" / analysis_id
        output_dir.mkdir(parents=True)
        transform = from_origin(203000, 3123000, 10, 10)
        mask_path = output_dir / "flood_mask.tif"
        reference_path = self.root / "data/validation/event/reference.tif"
        reference_path.parent.mkdir(parents=True)
        values = np.array([[1, 0], [0, 0]], dtype=np.uint8)
        for path in (mask_path, reference_path):
            with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8", crs="EPSG:32645", transform=transform, nodata=255) as raster:
                raster.write(values, 1)
        with rasterio.open(mask_path) as raster:
            bounds = transform_bounds(raster.crs, "EPSG:4326", *raster.bounds)
        inset_x = (bounds[2] - bounds[0]) * 0.05
        inset_y = (bounds[3] - bounds[1]) * 0.05
        bbox = [bounds[0] + inset_x, bounds[1] + inset_y, bounds[2] - inset_x, bounds[3] - inset_y]
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        mask_manifest = output_dir / "phase4_flood_mask_manifest.json"
        mask_manifest.write_text(json.dumps({
            "schema_version": "jaldrishti.phase4-flood-mask.v1", "synthetic": False,
            "artifact_type": "flood_mask", "source_type": "model_inference",
            "analysis_readiness": {"status": "pending_independent_validation"},
            "event_id": "event-1", "event_date": "2026-08-26", "inference_run_id": analysis_id,
            "model_checkpoint_sha256": "a" * 64, "preprocessing_version": "test-snap",
            "mask_semantics": {"flood_value": 1, "non_flood_value": 0, "nodata_value": 255},
            "width": 2, "height": 2, "transform": list(transform)[:6], "crs": "EPSG:32645",
            "nodata": 255, "valid_data_fraction": 1.0, "aoi_bbox_wgs84": bbox,
            "artifact_sha256": digest(mask_path), "artifact_path": "flood_mask.tif",
        }), encoding="utf-8")
        aligned_reference_path = reference_path.with_name("reference_aligned.tif")
        align_reference_to_prediction(
            project_root=self.root,
            reference_raster_path=reference_path,
            mask_manifest_path=mask_manifest,
            output_path=aligned_reference_path,
            source_flood_value=1,
            source_non_flood_values=[0],
            source_nodata_value=255,
        )
        report_path = self.root / "reports/generated/independent-validation.json"
        validate_against_reference(
            project_root=self.root, mask_manifest_path=mask_manifest,
            reference_raster_path=aligned_reference_path, output_path=report_path,
            event_id="event-1", event_date="2026-08-26", aoi_bbox_wgs84=tuple(bbox),
            reference_name="Fixture reference", reference_url="https://example.org/reference",
            reference_license="fixture-only", reference_flood_value=1,
            attest_independent_of_training_data=True, attest_not_used_as_prediction_input=True,
        )
        accept_validation_report(
            project_root=self.root, mask_manifest_path=mask_manifest, report_path=report_path,
            reviewed_by="Reviewer", acceptance_policy_id="policy-v1",
            minimum_flood_iou=1.0, minimum_flood_recall=1.0, minimum_valid_coverage=1.0,
        )
        boundaries_path = output_dir / "boundaries.geojson"
        boundaries_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        (output_dir / "analysis_manifest.json").write_text(json.dumps({
            "rasters": {"boundaries_geojson": {"sha256": digest(boundaries_path)}}
        }), encoding="utf-8")
        stored_job = self.service._read_job(analysis_id)
        stored_job.update(
            status="pending_independent_validation", event_id="event-1", event_date="2026-08-26",
            aoi_bbox_wgs84=bbox,
            candidate_artifacts={"output_directory": f"data/processed/inference/{analysis_id}"},
        )
        self.service._write_job(stored_job)

        response = self.service.get_status(analysis_id)

        self.assertEqual(response["status"], "completed")
        self.assertAlmostEqual(response["results"]["flooded_area_km2"], 0.0001)
        self.assertEqual(response["rasters"]["flood_mask"]["format"], "GeoTIFF")
        self.assertIsNone(response["rasters"]["flood_probability"])
        artifact, media_type = self.service.get_artifact(analysis_id, "flood_mask.tif")
        self.assertEqual(artifact.resolve(), mask_path.resolve())
        self.assertEqual(media_type, "image/tiff")

    def test_request_requires_overlap_smaller_than_tile(self) -> None:
        with self.assertRaises(ValueError):
            FloodAnalysisRequest(preprocessing_job_id=UUID(PREPROCESSING_JOB_ID), tile_size=64, overlap=64)


class FloodAnalysisAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        prep_dir = self.root / "data/manifests/sentinel1_preprocessing"
        prep_dir.mkdir(parents=True)
        (prep_dir / "manifest.json").write_text(json.dumps({
            "jobs": [{
                "job_id": PREPROCESSING_JOB_ID,
                "status": "completed",
                "model_input_status": "blocked_unknown_model_contract",
            }]
        }), encoding="utf-8")
        self.previous_service = analyses_api._service
        analyses_api._service = FloodAnalysisService(self.root)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        analyses_api._service = self.previous_service
        self.temp_dir.cleanup()

    def test_post_returns_job_id_and_poll_reports_blocked_input(self) -> None:
        response = self.client.post("/api/v1/analyses", json={"preprocessing_job_id": PREPROCESSING_JOB_ID})
        self.assertEqual(response.status_code, 202)
        analysis_id = response.json()["analysis_id"]
        self.assertEqual(response.json()["status"], "queued")
        poll = self.client.get(f"/api/v1/analyses/{analysis_id}")
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["status"], "blocked")
        self.assertEqual(poll.json()["error"]["code"], "model_input_review_required")
        self.assertIsNone(poll.json()["results"]["flooded_area_km2"])
        self.assertNotIn("candidate_flooded_area_km2", poll.json()["results"])

    def test_unknown_artifact_is_rejected_by_route_validation(self) -> None:
        response = self.client.get(f"/api/v1/analyses/{PREPROCESSING_JOB_ID}/artifacts/private.txt")
        self.assertEqual(response.status_code, 422)

    def test_candidate_artifact_is_not_served_before_phase4_acceptance(self) -> None:
        created = self.client.post("/api/v1/analyses", json={"preprocessing_job_id": PREPROCESSING_JOB_ID})
        analysis_id = created.json()["analysis_id"]

        response = self.client.get(f"/api/v1/analyses/{analysis_id}/artifacts/flood_mask.tif")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "artifact_not_validated")


if __name__ == "__main__":
    unittest.main()
