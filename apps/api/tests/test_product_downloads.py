from collections import Counter
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import httpx
import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS
from rasterio.transform import from_origin

from apps.api.app.core.config import Settings
from apps.api.app.main import app
from apps.api.app.schemas.downloads import ProductDownloadSelection
from apps.api.app.services.product_downloads import (
    ProductDownloadError,
    ProductDownloadService,
)


PRE_ID = "S1D_IW_GRDH_1SDV_20260821T122946_20260821T123011_004224_007C0F_BB02_COG"
POST_ID = "S1D_IW_GRDH_1SDV_20260902T122947_20260902T123012_004399_00822F_7364_COG"
PRE_ODO_ID = "9ac4bede-15a3-4ddc-bf73-7e3660be233c"
POST_ODO_ID = "6634dff9-500f-4c3b-af67-b499b36b8a71"
AOI = [84.04, 28.20, 84.15, 28.31]


def make_safe_archive(
    measurement_bytes: bytes | None = None,
    *,
    with_gcps: bool = False,
    with_georeferencing: bool = True,
) -> bytes:
    with tempfile.TemporaryDirectory() as temporary_dir:
        root = Path(temporary_dir)
        raster_path = root / "measurement.tiff"
        if measurement_bytes is None:
            raster_options = {
                "driver": "GTiff",
                "width": 4,
                "height": 3,
                "count": 1,
                "dtype": "uint16",
                "nodata": 0,
            }
            if with_georeferencing and not with_gcps:
                raster_options.update({"crs": "EPSG:4326", "transform": from_origin(84.0, 29.0, 0.01, 0.01)})
            with rasterio.open(
                raster_path,
                "w",
                **raster_options,
            ) as dataset:
                dataset.write(np.arange(12, dtype=np.uint16).reshape((1, 3, 4)))
                if with_gcps:
                    dataset.gcps = (
                        [
                            GroundControlPoint(row=0, col=0, x=84.0, y=29.0),
                            GroundControlPoint(row=0, col=3, x=84.03, y=29.0),
                            GroundControlPoint(row=2, col=0, x=84.0, y=28.98),
                            GroundControlPoint(row=2, col=3, x=84.03, y=28.98),
                        ],
                        CRS.from_epsg(4326),
                    )
        else:
            raster_path.write_bytes(measurement_bytes)

        archive_path = root / "product.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("S1_TEST.SAFE/manifest.safe", "<xfdu:XFDU xmlns:xfdu='urn:test' />")
            archive.write(raster_path, "S1_TEST.SAFE/measurement/test-vv.tiff")
        return archive_path.read_bytes()


class InterruptedStream(httpx.SyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk

    def __iter__(self):
        yield self.first_chunk
        raise httpx.ReadError("simulated interrupted transfer")

    def close(self) -> None:
        return None


class MockCDSE:
    def __init__(
        self,
        archive: bytes,
        *,
        expected_size_delta: int = 0,
        checksum: str | None = "valid",
        download_behaviors: list[object] | None = None,
        token_status: int = 200,
    ) -> None:
        self.archive = archive
        self.expected_size = len(archive) + expected_size_delta
        self.checksum = checksum
        self.download_behaviors = list(download_behaviors or [])
        self.token_status = token_status
        self.counts: Counter[str] = Counter()
        self.seen_authorization: list[str] = []

    def _item(self, scene_id: str, acquisition: str, end_time: str, product_name: str, orbit: int) -> dict:
        return {
            "type": "Feature",
            "id": scene_id,
            "collection": "sentinel-1-grd",
            "bbox": [81.9, 27.0, 84.8, 29.1],
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[81.9, 28.6], [82.2, 27.0], [84.8, 27.5], [84.5, 29.1], [81.9, 28.6]]],
            },
            "properties": {
                "product:type": "IW_GRDH_1S",
                "start_datetime": acquisition,
                "end_datetime": end_time,
                "sat:orbit_state": "ascending",
                "sat:relative_orbit": orbit,
                "sar:polarizations": ["VV", "VH"],
                "platform": "sentinel-1d",
                "_private": {"product_name": product_name},
            },
            "assets": {
                "Product": {
                    "href": "https://download.dataspace.copernicus.eu/odata/v1/Products/test/$value",
                    "type": "application/zip",
                    "roles": ["data", "metadata", "archive"],
                    "file:size": self.expected_size,
                    "auth:refs": ["oidc"],
                }
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.dataspace.copernicus.eu":
            self.counts["token"] += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant", "description": "credential rejected"})
            return httpx.Response(200, json={"access_token": "test-bearer-token-do-not-persist", "expires_in": 3600})

        if request.url.host == "stac.dataspace.copernicus.eu":
            self.counts["stac"] += 1
            scene_id = request.url.path.rsplit("/", 1)[-1]
            if scene_id == PRE_ID:
                body = self._item(
                    PRE_ID,
                    "2026-08-21T12:29:46.705159Z",
                    "2026-08-21T12:30:11.703597Z",
                    f"{PRE_ID}.SAFE",
                    158,
                )
            elif scene_id == POST_ID:
                body = self._item(
                    POST_ID,
                    "2026-09-02T12:29:47.191710Z",
                    "2026-09-02T12:30:12.190176Z",
                    f"{POST_ID}.SAFE",
                    158,
                )
            else:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=body, headers={"Content-Type": "application/geo+json"})

        if request.url.host == "catalogue.dataspace.copernicus.eu" and request.url.path.endswith("/Products"):
            self.counts["odata"] += 1
            query = request.url.params.get("$filter", "")
            if PRE_ID in query:
                name, identifier = f"{PRE_ID}.SAFE", PRE_ODO_ID
            elif POST_ID in query:
                name, identifier = f"{POST_ID}.SAFE", POST_ODO_ID
            else:
                return httpx.Response(200, json={"value": []})
            record = {
                "Id": identifier,
                "Name": name,
                "ContentType": "application/octet-stream",
                "ContentLength": self.expected_size,
                "Online": True,
                "EvictionDate": "9999-12-31T23:59:59.999999Z",
                "ContentDate": {"Start": "2026-08-21T12:29:46.705159Z"},
            }
            if self.checksum == "valid":
                record["Checksum"] = [
                    {"Algorithm": "MD5", "Value": hashlib.md5(self.archive, usedforsecurity=False).hexdigest()},
                    {"Algorithm": "BLAKE3", "Value": "a" * 64},
                ]
            elif self.checksum == "wrong":
                record["Checksum"] = [{"Algorithm": "MD5", "Value": "f" * 32}]
            return httpx.Response(200, json={"value": [record]})

        if request.url.host == "download.dataspace.copernicus.eu":
            self.counts["download"] += 1
            self.seen_authorization.append(request.headers.get("Authorization", ""))
            behavior = self.download_behaviors.pop(0) if self.download_behaviors else "success"
            if behavior == "trusted_redirect":
                return httpx.Response(302, headers={"Location": "https://eodata.dataspace.copernicus.eu/assets/product.zip"})
            if behavior == "unsafe_redirect":
                return httpx.Response(302, headers={"Location": "https://attacker.invalid/product.zip"})
            if behavior == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if behavior == "unauthorized":
                return httpx.Response(401, json={"error": "invalid_token"})
            if behavior == "forbidden":
                return httpx.Response(403, json={"error": "access_denied"})
            if behavior == "rate_limit":
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate_limited"})
            if behavior == "server_error":
                return httpx.Response(503, json={"error": "temporary"})
            if behavior == "interrupted":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(self.archive))},
                    stream=InterruptedStream(self.archive[:64]),
                )
            if behavior == "empty_response":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/zip", "Content-Length": "0"},
                    content=b"",
                )
            if isinstance(behavior, httpx.Response):
                return behavior
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(self.archive))},
                content=self.archive,
            )

        if request.url.host == "eodata.dataspace.copernicus.eu":
            self.counts["redirected_download"] += 1
            self.seen_authorization.append(request.headers.get("Authorization", ""))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(self.archive))},
                content=self.archive,
            )

        raise AssertionError(f"Unexpected test request: {request.method} {request.url}")


def selection(role: str = "pre_event") -> ProductDownloadSelection:
    return ProductDownloadSelection(
        pre_scene_id=PRE_ID,
        post_scene_id=POST_ID,
        event_date=date(2026, 8, 26),
        bbox=tuple(AOI),
        role=role,
        asset_key="Product",
    )


def settings_for(path: Path, *, credentials: bool = True, max_attempts: int = 3, max_bytes: int = 10_000_000) -> Settings:
    return Settings(
        data_dir=path,
        max_download_bytes=max_bytes,
        max_attempts=max_attempts,
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        backoff_seconds=0.1,
        max_backoff_seconds=5,
        cdse_username="student@example.invalid" if credentials else None,
        cdse_password="test-account-password-never-log" if credentials else None,
    )


class ProductDownloadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_dir.name) / "data"
        self.archive = make_safe_archive()
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self.temporary_dir.cleanup()

    def service(self, mock: MockCDSE, *, credentials: bool = True, max_attempts: int = 3) -> ProductDownloadService:
        return ProductDownloadService(
            settings_for(self.data_dir, credentials=credentials, max_attempts=max_attempts),
            transport=httpx.MockTransport(mock),
            sleep=self.sleeps.append,
        )

    def start_and_run(self, service: ProductDownloadService, role: str = "pre_event") -> dict:
        job, is_new = service.request_download(selection(role))
        self.assertTrue(is_new)
        service.run_download_job(job["download_id"])
        return service.get_status(job["download_id"])

    def test_success_streams_to_configured_path_and_validates_safe_geotiff(self) -> None:
        mock = MockCDSE(self.archive)
        service = self.service(mock)
        result = self.start_and_run(service)

        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(result["file_size_bytes"], len(self.archive))
        self.assertEqual(result["checksum_status"], "verified")
        self.assertEqual(result["validation"]["geospatial"]["raster_count"], 1)
        raster = result["validation"]["geospatial"]["rasters"][0]
        self.assertEqual(raster["crs"], "EPSG:4326")
        self.assertEqual((raster["width"], raster["height"], raster["band_count"]), (4, 3, 1))
        self.assertFalse(result["validation"]["geospatial"]["analysis_ready"])
        self.assertFalse(Path(result["file_path"]).is_absolute())
        self.assertTrue((self.data_dir / result["file_path"]).is_file())
        self.assertEqual(mock.counts["stac"], 2)
        self.assertEqual(mock.counts["odata"], 1)
        self.assertEqual(mock.counts["download"], 1)
        self.assertEqual(mock.seen_authorization, ["Bearer test-bearer-token-do-not-persist"])

    def test_gcp_georeferenced_geotiff_is_inspected_without_reading_pixels(self) -> None:
        result = self.start_and_run(self.service(MockCDSE(make_safe_archive(with_gcps=True))))
        self.assertEqual(result["status"], "geospatial_inspected")
        geospatial = result["validation"]["geospatial"]
        raster = geospatial["rasters"][0]
        self.assertEqual(raster["crs"], "EPSG:4326")
        self.assertEqual(raster["georeferencing"], "ground_control_points")
        self.assertEqual(raster["bounds_source"], "ground_control_points")
        self.assertEqual(raster["gcp_count"], 4)
        self.assertEqual(raster["bounds"], [84.0, 28.98, 84.03, 29.0])
        self.assertFalse(geospatial["pixel_values_read"])
        self.assertFalse(geospatial["analysis_ready"])
        self.assertTrue(any("not been warped" in warning for warning in geospatial["warnings"]))

    def test_raster_without_affine_crs_or_gcps_is_rejected(self) -> None:
        archive = make_safe_archive(with_georeferencing=False)
        result = self.start_and_run(self.service(MockCDSE(archive)))
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["error"]["code"], "invalid_raster_metadata")

    def test_reinspection_endpoint_reuses_existing_file_without_another_download(self) -> None:
        from apps.api.app.api.v1 import downloads as downloads_api

        mock = MockCDSE(make_safe_archive(with_gcps=True))
        service = self.service(mock)
        result = self.start_and_run(service)
        download_id = result["download_id"]
        service._update_job(
            download_id,
            {
                "status": "validation_failed",
                "validation": {
                    "integrity": result["validation"]["integrity"],
                    "geospatial": {"status": "failed", "code": "invalid_raster_metadata"},
                },
                "error": {"code": "invalid_raster_metadata", "message": "Previous metadata check did not support GCP georeferencing."},
            },
        )
        download_requests_before = mock.counts["download"]
        client = TestClient(app)
        with patch.object(downloads_api, "get_product_download_service", return_value=service):
            response = client.post(f"/api/v1/downloads/{download_id}/inspect")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "geospatial_inspected")
        self.assertEqual(mock.counts["download"], download_requests_before)
        self.assertEqual(response.json()["validation"]["geospatial"]["rasters"][0]["gcp_count"], 4)
        self.assertFalse(response.json()["validation"]["geospatial"]["analysis_ready"])
        self.assertEqual(len(json.loads(service.manifest_path.read_text())["downloads"][0]["inspection_attempts"]), 1)

    def test_reinspection_rejects_path_traversal_in_manifest(self) -> None:
        service = self.service(MockCDSE(make_safe_archive(with_gcps=True)))
        result = self.start_and_run(service)
        service._update_job(result["download_id"], {"status": "validation_failed", "file_path": "products/../../outside.zip"})
        with self.assertRaises(ProductDownloadError) as context:
            service.reinspect_geospatial_metadata(result["download_id"])
        self.assertEqual(context.exception.code, "unsafe_product_path")

    def test_authentication_failure_is_recorded_without_downloading(self) -> None:
        mock = MockCDSE(self.archive, token_status=401)
        service = self.service(mock)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "authentication_failed")
        self.assertEqual(mock.counts["download"], 0)
        manifest_text = service.manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("test-account-password-never-log", manifest_text)
        self.assertNotIn("test-bearer-token-do-not-persist", manifest_text)

    def test_download_401_refreshes_token_once_then_retries(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["unauthorized", "success"])
        service = self.service(mock)
        result = self.start_and_run(service)

        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(mock.counts["token"], 2)
        self.assertEqual(mock.counts["download"], 2)
        attempts = json.loads(service.manifest_path.read_text())["downloads"][0]["attempts"]
        self.assertEqual([attempt["outcome"] for attempt in attempts], ["failed", "geospatial_inspected"])
        self.assertEqual(attempts[0]["error"]["code"], "download_authentication_failed")

    def test_repeated_download_401_stops_after_one_fresh_token(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["unauthorized", "unauthorized", "success"])
        service = self.service(mock)
        result = self.start_and_run(service)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "download_authentication_failed")
        self.assertEqual(mock.counts["token"], 2)
        self.assertEqual(mock.counts["download"], 2)

    def test_download_403_is_reported_as_product_access_denial_without_retry(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["forbidden"])
        service = self.service(mock)
        result = self.start_and_run(service)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "download_access_denied")
        self.assertEqual(mock.counts["token"], 1)
        self.assertEqual(mock.counts["download"], 1)

    def test_missing_credentials_prevent_any_catalogue_or_download_request(self) -> None:
        mock = MockCDSE(self.archive)
        service = self.service(mock, credentials=False)
        with self.assertRaises(ProductDownloadError) as context:
            service.request_download(selection())
        self.assertEqual(context.exception.code, "credentials_required")
        self.assertEqual(mock.counts, Counter())
        self.assertFalse(service.manifest_path.exists())

    def test_rate_limit_respects_retry_after_and_then_completes(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["rate_limit", "success"])
        service = self.service(mock)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(mock.counts["download"], 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(len(json.loads(service.manifest_path.read_text())["downloads"][0]["attempts"]), 2)

    def test_temporary_server_failure_retries_only_within_configured_limit(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["server_error", "server_error", "server_error"])
        service = self.service(mock, max_attempts=3)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "download_upstream_unavailable")
        self.assertEqual(mock.counts["download"], 3)
        self.assertFalse(list((self.data_dir / "temporary").glob("*.part")))

    def test_timeout_is_retried_and_partial_files_are_not_accepted(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["timeout", "timeout", "timeout"])
        service = self.service(mock, max_attempts=3)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "download_timeout")
        self.assertEqual(mock.counts["download"], 3)
        self.assertFalse(list((self.data_dir / "temporary").glob("*.part")))
        self.assertFalse(list((self.data_dir / "products").glob("*.zip")))

    def test_redirect_is_followed_only_to_an_official_cdse_domain(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["trusted_redirect"])
        result = self.start_and_run(self.service(mock))
        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(mock.counts["redirected_download"], 1)
        self.assertEqual(mock.seen_authorization, [
            "Bearer test-bearer-token-do-not-persist",
            "Bearer test-bearer-token-do-not-persist",
        ])

    def test_redirect_to_untrusted_domain_is_blocked_before_sending_token(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["unsafe_redirect"])
        result = self.start_and_run(self.service(mock))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "unsafe_download_redirect")
        self.assertEqual(mock.counts["download"], 1)
        self.assertEqual(mock.seen_authorization, ["Bearer test-bearer-token-do-not-persist"])
        self.assertEqual(mock.counts["redirected_download"], 0)

    def test_interrupted_download_restarts_from_zero_and_removes_partial_file(self) -> None:
        mock = MockCDSE(self.archive, download_behaviors=["interrupted", "success"])
        service = self.service(mock)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(mock.counts["download"], 2)
        self.assertFalse(list((self.data_dir / "temporary").glob("*.part")))

    def test_size_mismatch_is_retried_and_never_finalized(self) -> None:
        mock = MockCDSE(self.archive, expected_size_delta=1)
        service = self.service(mock)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "size_mismatch")
        self.assertEqual(mock.counts["download"], 3)
        self.assertFalse(list((self.data_dir / "products").glob("*.zip")))

    def test_checksum_mismatch_fails_validation_and_does_not_finalize_file(self) -> None:
        mock = MockCDSE(self.archive, checksum="wrong")
        service = self.service(mock)
        result = self.start_and_run(service)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["error"]["code"], "checksum_mismatch")
        self.assertFalse(list((self.data_dir / "products").glob("*.zip")))

    def test_unexpected_html_and_empty_response_are_not_accepted(self) -> None:
        html_mock = MockCDSE(
            self.archive,
            download_behaviors=[
                httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>login</html>")
            ],
        )
        html_result = self.start_and_run(self.service(html_mock))
        self.assertEqual(html_result["status"], "failed")
        self.assertEqual(html_result["error"]["code"], "unexpected_download_format")

        empty_mock = MockCDSE(
            self.archive,
            download_behaviors=["empty_response", "empty_response", "empty_response"],
        )
        empty_result = self.start_and_run(self.service(empty_mock))
        self.assertEqual(empty_result["status"], "failed")
        self.assertEqual(empty_result["error"]["code"], "size_mismatch")

    def test_invalid_zip_and_corrupt_measurement_raster_are_reported(self) -> None:
        invalid_zip = b"not a zip archive"
        invalid_zip_mock = MockCDSE(invalid_zip, checksum=None)
        invalid_zip_result = self.start_and_run(self.service(invalid_zip_mock))
        self.assertEqual(invalid_zip_result["status"], "validation_failed")
        self.assertEqual(invalid_zip_result["error"]["code"], "invalid_archive")

        corrupt_raster_zip = make_safe_archive(b"not a geotiff")
        corrupt_raster_mock = MockCDSE(corrupt_raster_zip)
        corrupt_raster_result = self.start_and_run(self.service(corrupt_raster_mock), role="post_event")
        self.assertEqual(corrupt_raster_result["status"], "validation_failed")
        self.assertEqual(corrupt_raster_result["error"]["code"], "raster_unreadable")
        self.assertTrue((self.data_dir / corrupt_raster_result["file_path"]).is_file())
        self.assertEqual(corrupt_raster_result["validation"]["integrity"]["integrity"], "valid")

    def test_missing_provider_checksum_is_reported_without_inventing_verification(self) -> None:
        mock = MockCDSE(self.archive, checksum=None)
        result = self.start_and_run(self.service(mock))
        self.assertEqual(result["status"], "geospatial_inspected")
        self.assertEqual(result["checksum_status"], "unavailable")
        self.assertIn("CDSE did not provide a product checksum", result["warnings"][0])
        self.assertEqual(len(result["computed_sha256"]), 64)

    def test_duplicate_request_reuses_the_same_job_and_manifest(self) -> None:
        mock = MockCDSE(self.archive)
        service = self.service(mock)
        first, first_is_new = service.request_download(selection())
        second, second_is_new = service.request_download(selection())
        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(first["download_id"], second["download_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(json.loads(service.manifest_path.read_text())["downloads"]), 1)

    def test_restart_marks_in_progress_job_failed_cleans_partial_and_allows_retry(self) -> None:
        mock = MockCDSE(self.archive)
        first_service = self.service(mock)
        job, is_new = first_service.request_download(selection())
        self.assertTrue(is_new)
        first_service._begin_attempt(job["download_id"], 1)
        temporary_dir = self.data_dir / "temporary"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        partial = temporary_dir / f"{job['download_id']}-1-test.part"
        partial.write_bytes(b"interrupted bytes")

        restarted_service = ProductDownloadService(
            settings_for(self.data_dir),
            transport=httpx.MockTransport(mock),
            sleep=self.sleeps.append,
        )
        recovered = restarted_service.get_status(job["download_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"]["code"], "worker_interrupted")
        self.assertFalse(partial.exists())

        manifest_entry = json.loads(restarted_service.manifest_path.read_text())["downloads"][0]
        self.assertEqual(manifest_entry["attempts"][0]["outcome"], "interrupted")
        self.assertTrue(manifest_entry["attempts"][0]["ended_at"])
        retried, retry_is_new = restarted_service.request_download(selection())
        self.assertTrue(retry_is_new)
        self.assertNotEqual(retried["download_id"], job["download_id"])

    def test_manifest_status_paths_and_secrets_are_safe(self) -> None:
        mock = MockCDSE(self.archive)
        service = self.service(mock)
        result = self.start_and_run(service)
        manifest = json.loads(service.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        entry = manifest["downloads"][0]
        self.assertEqual(entry["status"], "geospatial_inspected")
        self.assertTrue(entry["attempts"][0]["started_at"])
        self.assertTrue(entry["attempts"][0]["ended_at"])
        self.assertEqual(entry["attribution"], "Contains modified Copernicus Sentinel data 2026.")
        serialized = json.dumps({"manifest": manifest, "status": result})
        self.assertNotIn("test-account-password-never-log", serialized)
        self.assertNotIn("test-bearer-token-do-not-persist", serialized)
        self.assertNotIn("student@example.invalid", serialized)
        if os.name == "posix":
            self.assertEqual(service.manifest_path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_manifest_is_left_untouched(self) -> None:
        self.data_dir.mkdir(parents=True)
        self.data_dir.joinpath("manifest.json").write_text("{broken", encoding="utf-8")
        service = self.service(MockCDSE(self.archive))
        with self.assertRaises(ProductDownloadError) as context:
            service.request_download(selection())
        self.assertEqual(context.exception.code, "manifest_unreadable")
        self.assertEqual(service.manifest_path.read_text(encoding="utf-8"), "{broken")


class ProductDownloadApiTests(unittest.TestCase):
    def test_path_traversal_and_unapproved_asset_are_rejected_by_schema(self) -> None:
        values = {
            "pre_scene_id": "../../etc/passwd",
            "post_scene_id": POST_ID,
            "event_date": "2026-08-26",
            "bbox": AOI,
            "role": "pre_event",
            "asset_key": "Product",
        }
        with self.assertRaises(ValueError):
            ProductDownloadSelection(**values)
        with self.assertRaises(ValueError):
            ProductDownloadSelection(**{**values, "pre_scene_id": PRE_ID, "asset_key": "vv"})

    def test_download_endpoint_reports_missing_credentials_without_url_or_path_input(self) -> None:
        from apps.api.app.api.v1 import downloads as downloads_api

        service = ProductDownloadService(
            settings_for(Path(tempfile.mkdtemp()), credentials=False),
            transport=httpx.MockTransport(MockCDSE(make_safe_archive())),
            sleep=lambda _seconds: None,
        )
        client = TestClient(app)
        with patch.object(downloads_api, "get_product_download_service", return_value=service):
            response = client.post(
                "/api/v1/downloads",
                json={
                    "pre_scene_id": PRE_ID,
                    "post_scene_id": POST_ID,
                    "event_date": "2026-08-26",
                    "bbox": AOI,
                    "role": "pre_event",
                    "asset_key": "Product",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "credentials_required")


if __name__ == "__main__":
    unittest.main()
