import json
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin

from apps.api.app.services.case_study import CaseStudyService


class CaseStudyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        output_dir = self.root / "data/processed/sentinel1/job-1"
        output_dir.mkdir(parents=True)
        pre_path = output_dir / "pre.tif"
        post_path = output_dir / "post.tif"
        self._write_raster(pre_path, np.array([[0.1, 0.2], [0.3, np.nan]], dtype=np.float32))
        self._write_raster(post_path, np.array([[0.2, 0.25], [0.35, np.nan]], dtype=np.float32))
        manifest_dir = self.root / "data/manifests/sentinel1_preprocessing"
        manifest_dir.mkdir(parents=True)
        manifest = {
            "jobs": [{
                "job_id": "job-1",
                "status": "completed",
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
                "pair": {
                    "event_date": "2026-08-26",
                    "requested_bbox_wgs84": [84.04, 28.2, 84.15, 28.31],
                    "pre": {"scene_id": "PRE_REAL", "acquisition_start": "2026-08-21T12:29:46Z", "orbit_direction": "ascending", "relative_orbit": 158, "polarization": ["VV", "VH"]},
                    "post": {"scene_id": "POST_REAL", "acquisition_start": "2026-09-02T12:29:47Z"},
                },
                "outputs": {
                    "pre_event": {"path": "job-1/pre.tif", "sha256": hashlib.sha256(pre_path.read_bytes()).hexdigest(), "crs": "EPSG:32645", "bands": ["Sigma0_VV", "Sigma0_VH"]},
                    "post_event": {"path": "job-1/post.tif", "sha256": hashlib.sha256(post_path.read_bytes()).hexdigest(), "crs": "EPSG:32645", "bands": ["Sigma0_VV", "Sigma0_VH"]},
                    "common_grid": {"width": 2, "height": 2, "aoi_pixel_count": 3},
                },
            }],
        }
        (manifest_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.service = CaseStudyService(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _write_raster(path: Path, band: np.ndarray) -> None:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=2,
            dtype="float32",
            crs="EPSG:32645",
            transform=from_origin(209430, 3135110, 10, 10),
            nodata=np.nan,
        ) as output:
            output.write(band, 1)
            output.write(np.where(np.isfinite(band), band * 0.2, np.nan).astype(np.float32), 2)
            output.set_band_description(1, "Sigma0_VV")
            output.set_band_description(2, "Sigma0_VH")

    def test_summary_reports_real_pair_and_keeps_model_results_pending(self) -> None:
        result = self.service.summary()
        self.assertEqual(result["analysis_status"], "imagery_ready_prediction_pending")
        self.assertEqual(result["source"]["pre_scene_id"], "PRE_REAL")
        self.assertEqual(result["source"]["post_scene_id"], "POST_REAL")
        self.assertEqual(result["rasters"]["pre_event"]["crs"], "EPSG:32645")
        self.assertIsNone(result["rasters"]["flood_mask"])
        self.assertIsNone(result["rasters"]["boundaries_geojson"])
        self.assertIsNone(result["results"]["flooded_area_km2"])
        self.assertFalse(result["model"]["inference_eligible"])
        self.assertEqual(result["model"]["status"], "no_deployment_approved_checkpoint")
        self.assertIsNone(result["model"]["evaluation_status"])

    def test_summary_reports_exploratory_evaluation_without_enabling_inference(self) -> None:
        report_dir = self.root / "reports/generated"
        report_dir.mkdir(parents=True)
        (report_dir / "run-evaluation.json").write_text(json.dumps({
            "evaluation_status": "completed_pending_review",
            "deployment_approved": False,
            "checkpoint_sha256": "a" * 64,
        }), encoding="utf-8")

        result = self.service.summary()

        self.assertEqual(result["stage"], "model_acceptance_and_nepal_input_validation_required")
        self.assertEqual(result["model"]["status"], "exploratory_checkpoint_pending_review")
        self.assertEqual(result["model"]["evaluation_status"], "completed_pending_review")
        self.assertEqual(result["model"]["candidate_checkpoint_sha256"], "a" * 64)
        self.assertFalse(result["model"]["deployment_approved"])
        self.assertFalse(result["model"]["inference_eligible"])
        self.assertIsNone(result["rasters"]["flood_mask"])
        self.assertIsNone(result["results"]["flooded_area_km2"])

    def test_quicklook_is_a_valid_png_with_shared_real_pair_stretch(self) -> None:
        image = self.service.quicklook_png("pre_event")
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(image[12:16], b"IHDR")
        width, height = struct.unpack(">II", image[16:24])
        self.assertEqual((width, height), (2, 2))
        self.assertGreater(len(image), 50)

    def test_rejects_unsafe_manifest_output_path(self) -> None:
        manifest_path = self.root / "data/manifests/sentinel1_preprocessing/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jobs"][0]["outputs"]["pre_event"]["path"] = "../../outside.tif"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "No completed preprocessing job"):
            self.service.quicklook_png("pre_event")

    def test_rejects_unknown_raster_role(self) -> None:
        with self.assertRaisesRegex(Exception, "Raster role"):
            self.service.quicklook_png("mask")

    def test_finds_matching_prepared_osm_snapshot_without_fixed_snapshot_id(self) -> None:
        snapshot_id = "snapshot-a"
        snapshot_dir = self.root / "data/raw/osm" / snapshot_id
        prepared_dir = self.root / "data/processed/infrastructure" / snapshot_id
        snapshot_dir.mkdir(parents=True)
        prepared_dir.mkdir(parents=True)
        (snapshot_dir / "snapshot_metadata.json").write_text(json.dumps({
            "snapshot_id": snapshot_id,
            "event_date": "2026-08-26",
            "provider_snapshot_date": "2026-08-01",
            "temporal_verification_status": "provider_dated_historical",
            "aoi": {"bbox_wgs84": [84.04, 28.2, 84.15, 28.31]},
            "warnings": ["date-level evidence"],
        }), encoding="utf-8")
        (prepared_dir / "processing_metadata.json").write_text(json.dumps({
            "snapshot_id": snapshot_id,
            "feature_counts_by_category": {"buildings": 7},
            "limitations": ["not completeness evidence"],
        }), encoding="utf-8")
        manifest = json.loads((self.root / "data/manifests/sentinel1_preprocessing/manifest.json").read_text())
        result = self.service._matching_osm(manifest["jobs"][0])
        self.assertEqual(result["snapshot_id"], snapshot_id)
        self.assertEqual(result["feature_counts_by_category"], {"buildings": 7})
        self.assertEqual(result["status"], "prepared_not_intersected_with_flood")


if __name__ == "__main__":
    unittest.main()
