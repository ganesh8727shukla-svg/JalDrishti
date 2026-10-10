from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from xml.etree import ElementTree

from fastapi.testclient import TestClient
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from apps.api.app.core.config import Settings
from apps.api.app.main import app
from apps.api.app.schemas.observations import ObservationMetadata
from apps.api.app.schemas.preprocessing import SARPreprocessingRequest
from apps.api.app.services.sar_preprocessing import SARPreprocessingError, SARPreprocessingService


PRE_ID = "S1D_IW_GRDH_1SDV_20260821T122946_20260821T123011_004224_007C0F_BB02_COG"
POST_ID = "S1D_IW_GRDH_1SDV_20260902T122947_20260902T123012_004399_00822F_7364_COG"
PRE_DOWNLOAD = "0e0ae6c7-064b-4e8e-af81-cb0c05397ac0"
POST_DOWNLOAD = "3a7508d9-dd51-4e06-a8b5-f8842f807cd0"
BBOX = [84.04, 28.20, 84.15, 28.31]


def make_item(scene_id: str, acquisition: str, relative_orbit: int = 158) -> dict:
    ring = [[83.0, 27.0], [85.0, 27.0], [85.0, 30.0], [83.0, 30.0], [83.0, 27.0]]
    product_name = f"{scene_id}.SAFE"
    return {
        "type": "Feature",
        "id": scene_id,
        "collection": "sentinel-1-grd",
        "bbox": [83.0, 27.0, 85.0, 30.0],
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "title": product_name,
            "product:name": product_name,
            "product:type": "IW_GRDH_1SDV",
            "start_datetime": acquisition,
            "end_datetime": acquisition,
            "sat:orbit_state": "ascending",
            "sat:relative_orbit": relative_orbit,
            "sar:polarizations": ["VV", "VH"],
            "platform": "sentinel-1d",
        },
        "assets": {
            "Product": {
                "type": "application/zip",
                "roles": ["archive", "data"],
                "href": "s3://eodata/some/product.zip",
                "file:size": 100,
            }
        },
    }


def write_source_zip(path: Path, thermal_flags: tuple[str, str] = ("false", "false")) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("S1_TEST.SAFE/manifest.safe", "<manifest />")
        archive.writestr("S1_TEST.SAFE/measurement/source.txt", "measurement metadata fixture")
        for polarization, flag in zip(("vv", "vh"), thermal_flags):
            archive.writestr(
                f"S1_TEST.SAFE/annotation/s1-{polarization}.xml",
                f"<product><thermalNoiseCorrectionPerformed>{flag}</thermalNoiseCorrectionPerformed></product>",
            )
            archive.writestr(
                f"S1_TEST.SAFE/annotation/calibration/calibration-s1-{polarization}.xml",
                "<calibration />",
            )
            archive.writestr(
                f"S1_TEST.SAFE/annotation/rfi/rfi-s1-{polarization}.xml",
                "<rfi />",
            )


def settings_for(root: Path) -> Settings:
    return Settings(
        data_dir=root / "raw",
        max_download_bytes=10_000_000,
        max_attempts=1,
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        backoff_seconds=0.1,
        max_backoff_seconds=1,
        cdse_username=None,
        cdse_password=None,
    )


class SARPreprocessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = settings_for(self.root)
        self.settings.data_dir.mkdir(parents=True)
        self.pre_path = self.settings.data_dir / "products/pre.zip"
        self.post_path = self.settings.data_dir / "products/post.zip"
        write_source_zip(self.pre_path)
        write_source_zip(self.post_path)
        self.pre_item = make_item(PRE_ID, "2026-08-21T12:29:46.705159Z")
        self.post_item = make_item(POST_ID, "2026-09-02T12:29:47.191710Z")
        self.metadata_by_id = {PRE_ID: self.pre_item, POST_ID: self.post_item}
        self.write_download_manifest()
        self.service = self.make_service()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_download_manifest(self, *, pre_path: str = "products/pre.zip", pre_status: str = "geospatial_inspected") -> None:
        pre_size = (self.settings.data_dir / "products/pre.zip").stat().st_size
        post_size = (self.settings.data_dir / "products/post.zip").stat().st_size
        common = {
            "pre_scene_id": PRE_ID,
            "post_scene_id": POST_ID,
            "event_date": "2026-08-26",
            "bbox": BBOX,
            "asset_key": "Product",
            "collection": "sentinel-1-grd",
            "expected_size_bytes": 1000,
            "status": "geospatial_inspected",
            "validation": {"integrity": {"integrity": "valid"}},
        }
        downloads = [
            {
                **common, "download_id": PRE_DOWNLOAD, "scene_id": PRE_ID,
                "role": "pre_event", "file_path": pre_path,
                "file_name": "pre.zip", "expected_size_bytes": pre_size, "file_size_bytes": pre_size,
                "status": pre_status,
            },
            {
                **common, "download_id": POST_DOWNLOAD, "scene_id": POST_ID,
                "role": "post_event", "file_path": "products/post.zip",
                "file_name": "post.zip", "expected_size_bytes": post_size, "file_size_bytes": post_size,
            },
        ]
        self.settings.data_dir.joinpath("manifest.json").write_text(json.dumps({"schema_version": 1, "downloads": downloads}), encoding="utf-8")

    def make_service(self, *, orbit: int = 158, snap: str | None = "/mock/snap/bin/gpt") -> SARPreprocessingService:
        def fetch(scene_id: str):
            item = self.metadata_by_id[scene_id]
            if scene_id == POST_ID and orbit != 158:
                item = make_item(POST_ID, "2026-09-02T12:29:47.191710Z", relative_orbit=orbit)
            metadata = ObservationMetadata(
                scene_id=scene_id,
                collection="sentinel-1-grd",
                product_type="IW_GRDH_1SDV",
                acquisition_start=datetime.fromisoformat(item["properties"]["start_datetime"].replace("Z", "+00:00")),
                acquisition_end=datetime.fromisoformat(item["properties"]["end_datetime"].replace("Z", "+00:00")),
                polarization=["VV", "VH"],
                orbit_direction="ascending",
                relative_orbit=orbit if scene_id == POST_ID else 158,
                platform="sentinel-1d",
                bbox=item["bbox"],
                footprint=item["geometry"],
                asset_keys=["Product"],
                catalogue_source="https://stac.dataspace.copernicus.eu/v1/search",
                metadata_retrieved_at=datetime.now(timezone.utc),
            )
            return item, metadata

        service = SARPreprocessingService(
            self.settings,
            snap_executable=snap,
            metadata_fetcher=fetch,
            snap_runner=lambda command, log, timeout: None,
        )
        service.manifest_dir = self.root / "manifests"
        service.processed_dir = self.root / "processed"
        return service

    def request(self, **changes) -> SARPreprocessingRequest:
        values = {
            "pre_download_id": PRE_DOWNLOAD,
            "post_download_id": POST_DOWNLOAD,
            "bbox": BBOX,
            "target_crs": "EPSG:32645",
            "pixel_spacing_m": 200,
            "allow_snap_auxiliary_downloads": True,
        }
        values.update(changes)
        return SARPreprocessingRequest(**values)

    def test_request_rejects_geographic_crs_and_equal_downloads(self) -> None:
        with self.assertRaises(ValueError):
            self.request(target_crs="EPSG:4326")
        with self.assertRaises(ValueError):
            self.request(target_crs="EPSG:2263")
        with self.assertRaises(ValueError):
            self.request(post_download_id=PRE_DOWNLOAD)

    def test_pair_is_requeried_and_provenance_job_is_recorded(self) -> None:
        job = self.service.create_job(self.request())
        self.assertEqual(job["status"], "selected")
        self.assertEqual(job["pair"]["pre"]["scene_id"], PRE_ID)
        self.assertEqual(job["pair"]["post"]["relative_orbit"], 158)
        self.assertEqual(job["model_input_status"], "blocked_unknown_model_contract")
        manifest_text = self.service.jobs_manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("s3://", manifest_text)
        self.assertNotIn("password", manifest_text.lower())
        self.assertEqual(self.service.get_status(job["job_id"])["job_id"], job["job_id"])

    def test_snap_child_environment_and_logs_do_not_receive_cdse_secrets(self) -> None:
        script = self.root / "inspect_env.py"
        script.write_text(
            "import os; print(os.getenv('CDSE_USERNAME', '')); print(os.getenv('CDSE_PASSWORD', ''))",
            encoding="utf-8",
        )
        log = self.root / "snap.log"
        with patch.dict(os.environ, {"CDSE_USERNAME": "private-user-value", "CDSE_PASSWORD": "private-password-value"}):
            SARPreprocessingService._run_snap([sys.executable, str(script)], log, 10)
        contents = log.read_text(encoding="utf-8")
        self.assertNotIn("private-user-value", contents)
        self.assertNotIn("private-password-value", contents)

    def test_incompatible_catalogue_orbit_pair_is_rejected(self) -> None:
        service = self.make_service(orbit=19)
        with self.assertRaises(SARPreprocessingError) as context:
            service.create_job(self.request())
        self.assertEqual(context.exception.code, "pair_not_compatible")

    def test_nonvalidated_source_and_path_traversal_are_rejected(self) -> None:
        self.write_download_manifest(pre_status="validation_failed")
        with self.assertRaises(SARPreprocessingError) as context:
            self.service.create_job(self.request())
        self.assertEqual(context.exception.code, "source_not_validated")

        self.write_download_manifest(pre_path="products/../../outside.zip")
        with self.assertRaises(SARPreprocessingError) as context:
            self.service.create_job(self.request())
        self.assertEqual(context.exception.code, "unsafe_product_path")

    def test_missing_snap_and_unapproved_auxiliary_data_are_explicit(self) -> None:
        service = self.make_service(snap=None)
        service.snap_executable = None
        with self.assertRaises(SARPreprocessingError) as context:
            service.create_job(self.request())
        self.assertEqual(context.exception.code, "snap_not_installed")

        with self.assertRaises(SARPreprocessingError) as context:
            self.service.create_job(self.request(allow_snap_auxiliary_downloads=False))
        self.assertEqual(context.exception.code, "auxiliary_downloads_not_approved")

    def test_graph_contains_calibration_and_terrain_correction_without_db_or_speckle(self) -> None:
        graph = SARPreprocessingService._graph_xml({"thermal_noise_removal": True})
        ElementTree.fromstring(graph)
        self.assertIn("<operator>Calibration</operator>", graph)
        self.assertIn("<outputImageScaleInDb>false</outputImageScaleInDb>", graph)
        self.assertIn("<selectedPolarisations>VV,VH</selectedPolarisations>", graph)
        self.assertIn("<demName>Copernicus 30m Global DEM</demName>", graph)
        self.assertIn("<operator>BandSelect</operator>", graph)
        self.assertIn("<sourceBands>Sigma0_VV,Sigma0_VH</sourceBands>", graph)
        self.assertIn('<sourceProduct refid="Select-Sigma0-Bands"/>', graph)
        self.assertNotIn("Speckle", graph)
        self.assertNotIn("Gamma0", graph)
        self.assertIn("<operator>Remove-GRD-Border-Noise</operator>", graph)
        graph_without_second_noise_removal = SARPreprocessingService._graph_xml({"thermal_noise_removal": False})
        ElementTree.fromstring(graph_without_second_noise_removal)
        self.assertNotIn("ThermalNoiseRemoval", graph_without_second_noise_removal)

    def test_safe_annotation_controls_thermal_noise_step_and_mixed_pair_fails(self) -> None:
        self.assertEqual(
            SARPreprocessingService._inspect_source_processing(self.pre_path),
            {"VV": False, "VH": False},
        )
        write_source_zip(self.post_path, ("true", "true"))
        self.write_download_manifest()
        job = self.service.create_job(self.request())
        self.service.run_job(job["job_id"])
        result = self.service.get_status(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "thermal_noise_processing_mismatch")

    def test_common_grid_writes_aligned_real_raster_values_and_validity_stats(self) -> None:
        source_paths = [self.root / "pre_snap.tif", self.root / "post_snap.tif"]
        target_crs = "EPSG:32645"
        left, bottom, right, top = transform_bounds("EPSG:4326", target_crs, *BBOX, densify_pts=21)
        pixel = 500.0
        width = int(np.ceil((right - left + 2000) / pixel))
        height = int(np.ceil((top - bottom + 2000) / pixel))
        transform = from_origin(left - 1000, top + 1000, pixel, pixel)
        for index, path in enumerate(source_paths):
            with rasterio.open(
                path, "w", driver="GTiff", width=width, height=height, count=2,
                dtype="float32", crs=target_crs, transform=transform, nodata=-9999.0,
            ) as dataset:
                dataset.write(np.full((height, width), 0.2 + index * 0.1, dtype=np.float32), 1)
                dataset.write(np.full((height, width), 0.08 + index * 0.01, dtype=np.float32), 2)
        output = SARPreprocessingService._align_pair(
            source_paths,
            self.root / "outputs",
            {
                "target_crs": target_crs,
                "bbox_wgs84": BBOX,
                "pixel_spacing_m": 500,
                "snap_output_band_order": ["Sigma0_VV", "Sigma0_VH"],
            },
        )
        pre = self.root / "outputs/pre_event.tif"
        post = self.root / "outputs/post_event.tif"
        with rasterio.open(pre) as pre_ds, rasterio.open(post) as post_ds:
            self.assertEqual((pre_ds.width, pre_ds.height, pre_ds.transform, pre_ds.crs), (post_ds.width, post_ds.height, post_ds.transform, post_ds.crs))
            self.assertEqual(pre_ds.descriptions, ("Sigma0_VV", "Sigma0_VH"))
            self.assertAlmostEqual(float(np.nanmean(pre_ds.read(1))), 0.2, places=5)
            self.assertIn("SNAP BandSelect", pre_ds.tags()["BAND_ORDER_PROVENANCE"])
        self.assertGreater(output["common_grid"]["common_valid_coverage_fraction"], 0.95)
        self.assertTrue(output["common_grid"]["pre_post_grid_aligned"])
        self.assertFalse(output["pre_event"]["analysis_ready"])

    def test_band_order_requires_explicit_graph_provenance(self) -> None:
        source = self.root / "bad_bands.tif"
        with rasterio.open(source, "w", driver="GTiff", width=4, height=4, count=2, dtype="float32", crs="EPSG:32645", transform=from_origin(400000, 3200000, 1000, 1000), nodata=-9999.0) as ds:
            ds.write(np.ones((4, 4), dtype=np.float32), 1)
            ds.write(np.ones((4, 4), dtype=np.float32), 2)
            ds.set_band_description(1, "Sigma0_VH")
            ds.set_band_description(2, "Sigma0_VV")
        with self.assertRaises(SARPreprocessingError) as context:
            SARPreprocessingService._align_pair(
                [source, source], self.root / "out",
                {"target_crs": "EPSG:32645", "bbox_wgs84": BBOX, "pixel_spacing_m": 500},
            )
        self.assertEqual(context.exception.code, "snap_band_order_unverified")

        with self.assertRaises(SARPreprocessingError) as context:
            SARPreprocessingService._align_pair(
                [source, source], self.root / "out-with-provenance",
                {
                    "target_crs": "EPSG:32645",
                    "bbox_wgs84": BBOX,
                    "pixel_spacing_m": 500,
                    "snap_output_band_order": ["Sigma0_VV", "Sigma0_VH"],
                },
            )
        self.assertEqual(context.exception.code, "snap_band_order_mismatch")

    def test_endpoint_reports_snap_missing_without_creating_job(self) -> None:
        service = self.make_service(snap=None)
        service.snap_executable = None
        with patch("apps.api.app.api.v1.preprocessing._service", service):
            client = TestClient(app)
            response = client.post("/api/v1/preprocessing", json=self.request().model_dump(mode="json"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "snap_not_installed")
        self.assertFalse(service.jobs_manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
