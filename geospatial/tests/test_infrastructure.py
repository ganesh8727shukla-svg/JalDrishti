"""Unit tests with explicitly synthetic OSM-like features and tiny rasters only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import httpx
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon, box
import osmium

from geospatial.infrastructure import (
    InfrastructureError,
    _mask_to_geometry,
    acquire_ohsome_snapshot,
    audit_snapshot,
    build_ohsome_filter,
    calculate_exposure,
    categorize_tags,
    import_local_snapshot,
    import_geofabrik_pbf,
    measure_feature_exposure,
    prepare_infrastructure,
    verify_ohsome_temporal_evidence,
)


SNAPSHOT_TIME = "2026-08-25T12:00:00Z"
EVENT_DATE = "2026-08-26"
BOUNDS = (84.0, 28.0, 84.1, 28.1)


def _metadata(end="2026-10-10T00:00:00Z"):
    return {
        "apiVersion": "1.10.4",
        "attribution": {"text": "© OpenStreetMap contributors", "url": "https://ohsome.org/copyrights"},
        "extractRegion": {
            "temporalExtent": {"fromTimestamp": "2007-10-08T00:00:00Z", "toTimestamp": end},
            "spatialExtent": {"type": "Polygon", "coordinates": []},
        },
    }


def _feature(osm_id, geometry, tags, **metadata):
    props = {"@osmId": osm_id, "@osmType": osm_id.split("/")[0], "@version": 1, **metadata, **tags}
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _collection(timestamp=SNAPSHOT_TIME):
    return {
        "type": "FeatureCollection",
        "attribution": {"text": "© OpenStreetMap contributors", "url": "https://ohsome.org/copyrights"},
        "features": [
            _feature("way/101", {"type": "LineString", "coordinates": [[84.01, 28.02], [84.09, 28.08]]}, {"highway": "primary", "bridge": "yes"}, **{"@snapshotTimestamp": timestamp, "@lastEdit": "2026-08-20T12:00:00Z"}),
            _feature("way/202", {"type": "Polygon", "coordinates": [[[84.03, 28.03], [84.04, 28.03], [84.04, 28.04], [84.03, 28.04], [84.03, 28.03]]]}, {"building": "yes"}, **{"@snapshotTimestamp": timestamp}),
        ],
    }


class HistoricalEvidenceTests(unittest.TestCase):
    def test_verifies_pre_event_timestamp_against_source_extent_and_features(self):
        result = verify_ohsome_temporal_evidence(_collection(), _metadata(), SNAPSHOT_TIME, EVENT_DATE)
        self.assertEqual(result["status"], "verified_historical")
        self.assertEqual(result["feature_snapshot_timestamps_checked"], 2)

    def test_rejects_snapshot_at_or_after_event_cutoff(self):
        with self.assertRaisesRegex(InfrastructureError, "strictly earlier"):
            verify_ohsome_temporal_evidence(_collection("2026-08-26T00:00:00Z"), _metadata(), "2026-08-26T00:00:00Z", EVENT_DATE)

    def test_rejects_source_temporal_extent_that_does_not_cover_time(self):
        with self.assertRaisesRegex(InfrastructureError, "does not cover"):
            verify_ohsome_temporal_evidence(_collection(), _metadata("2026-08-24T00:00:00Z"), SNAPSHOT_TIME, EVENT_DATE)

    def test_rejects_missing_or_mismatched_feature_snapshot_metadata(self):
        collection = _collection("2026-08-24T12:00:00Z")
        with self.assertRaisesRegex(InfrastructureError, "matching the requested"):
            verify_ohsome_temporal_evidence(collection, _metadata(), SNAPSHOT_TIME, EVENT_DATE)

    def test_rejects_feature_edited_after_requested_snapshot(self):
        collection = _collection()
        collection["features"][0]["properties"]["@lastEdit"] = "2026-08-26T01:00:00Z"
        with self.assertRaisesRegex(InfrastructureError, "later than the requested"):
            verify_ohsome_temporal_evidence(collection, _metadata(), SNAPSHOT_TIME, EVENT_DATE)

    def test_builds_bounded_category_filter_and_rejects_unknown_categories(self):
        value = build_ohsome_filter(["roads", "buildings"])
        self.assertIn("highway=*", value)
        self.assertIn("building=*", value)
        with self.assertRaises(InfrastructureError):
            build_ohsome_filter(["everything"])

    def test_mocked_ohsome_acquisition_writes_provenance_checksum_and_attribution(self):
        collection_bytes = json.dumps(_collection(), separators=(",", ":")).encode()
        def handler(request):
            if request.url.path.endswith("/metadata"):
                return httpx.Response(200, json=_metadata())
            self.assertEqual(request.url.path, "/v1/elements/geometry")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.params.get("unused"), None)
            return httpx.Response(200, content=collection_bytes, headers={"content-type": "application/geo+json"})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as temp:
            result = acquire_ohsome_snapshot(
                event_id="test-event", event_date=EVENT_DATE, snapshot_timestamp=SNAPSHOT_TIME,
                bbox_wgs84=BOUNDS, output_root=Path(temp) / "osm", categories=["roads", "buildings"], client=client,
            )
            directory = Path(result["snapshot_directory"])
            self.assertEqual(result["temporal_verification_status"], "verified_historical")
            self.assertEqual((directory / "infrastructure.geojson").read_bytes(), collection_bytes)
            self.assertEqual(result["source_artifact_sha256"], hashlib.sha256(collection_bytes).hexdigest())
            self.assertEqual((directory / "attribution.txt").read_text().splitlines()[0], "© OpenStreetMap contributors.")
            audited = audit_snapshot(directory)
            self.assertTrue(audited["sha256_verified"])
            self.assertTrue(audited["historical_use_allowed"])
        client.close()

    def test_streamed_acquisition_enforces_size_cap_without_content_length(self):
        class ChunkStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * 12
                yield b"y" * 12

        def handler(request):
            if request.url.path.endswith("/metadata"):
                return httpx.Response(200, json=_metadata())
            return httpx.Response(200, stream=ChunkStream())
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "osm"
            with self.assertRaises(InfrastructureError) as caught:
                acquire_ohsome_snapshot(
                    event_id="bounded", event_date=EVENT_DATE, snapshot_timestamp=SNAPSHOT_TIME,
                    bbox_wgs84=BOUNDS, output_root=output, client=client, max_response_bytes=20,
                )
            self.assertEqual(caught.exception.code, "response_too_large")
            self.assertFalse(output.exists())
        client.close()

    def test_mocked_acquisition_fails_when_history_does_not_cover_requested_time(self):
        def handler(request):
            if request.url.path.endswith("/metadata"):
                return httpx.Response(200, json=_metadata("2026-08-24T00:00:00Z"))
            return httpx.Response(200, json=_collection())
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(InfrastructureError, "does not cover"):
                acquire_ohsome_snapshot(event_id="x", event_date=EVENT_DATE, snapshot_timestamp=SNAPSHOT_TIME, bbox_wgs84=BOUNDS, output_root=Path(temp) / "osm", client=client)
        client.close()


class LocalImportAndPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.geojson"
        self.source.write_text(json.dumps(_collection()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_local_timestamp_is_never_treated_as_verified_history(self):
        result = import_local_snapshot(
            input_path=self.source, output_root=self.root / "raw", event_id="x", event_date=EVENT_DATE,
            source_crs="EPSG:4326", snapshot_timestamp=SNAPSHOT_TIME, bbox_wgs84=BOUNDS,
        )
        self.assertEqual(result["temporal_verification_status"], "historical_unverified")
        self.assertIsNone(result["verified_underlying_snapshot_timestamp"])

    def test_import_requires_explicit_known_crs(self):
        with self.assertRaises(InfrastructureError):
            import_local_snapshot(input_path=self.source, output_root=self.root / "raw", event_id="x", event_date=EVENT_DATE, source_crs="unknown")

    def test_prepare_clips_normalizes_and_records_metric_crs_and_tags(self):
        imported = import_local_snapshot(
            input_path=self.source, output_root=self.root / "raw", event_id="x", event_date=EVENT_DATE,
            source_crs="EPSG:4326", snapshot_timestamp=SNAPSHOT_TIME, bbox_wgs84=BOUNDS,
        )
        output = self.root / "prepared"
        audit = prepare_infrastructure(snapshot_dir=imported["snapshot_directory"], output_dir=output)
        self.assertEqual(audit["temporal_verification_status"], "historical_unverified")
        self.assertEqual(audit["prepared_feature_count"], 2)
        self.assertEqual(audit["output_geometry_crs"], "EPSG:4326")
        self.assertTrue(audit["measurement_crs"].startswith("EPSG:326"))
        prepared = json.loads((output / "infrastructure_prepared.geojson").read_text())
        road = next(f for f in prepared["features"] if f["properties"]["osm_id"] == "way/101")
        self.assertEqual(road["properties"]["tags"]["highway"], "primary")
        self.assertIn("bridges", road["properties"]["categories"])
        self.assertGreater(road["properties"]["length_m"], 1000)
        self.assertEqual(audit["feature_counts_by_category"]["roads"], 1)

    def test_snapshot_checksum_tampering_is_detected(self):
        imported = import_local_snapshot(input_path=self.source, output_root=self.root / "raw", event_id="x", event_date=EVENT_DATE, source_crs="EPSG:4326")
        artifact = Path(imported["source_artifact_path"])
        artifact.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(InfrastructureError, "SHA-256"):
            audit_snapshot(imported["snapshot_directory"])

    def test_geometry_alone_does_not_infer_facility_category(self):
        self.assertEqual(categorize_tags({}), [])

    def _write_tiny_geofabrik_source(self, root: Path, name="test-260801.osm.pbf"):
        pbf = root / name
        writer = osmium.SimpleWriter(str(pbf))
        for node_id, lon, lat, tags in [
            (1, 84.02, 28.02, {"place": "village"}),
            (2, 84.03, 28.03, {}),
            (3, 84.04, 28.04, {}),
        ]:
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=osmium.osm.Location(lon, lat), tags=tags))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[2, 3], tags={"highway": "primary"}))
        writer.close()
        sidecar = root / (name + ".md5")
        digest = hashlib.md5(pbf.read_bytes()).hexdigest()
        sidecar.write_text(f"{digest}  {name}\n", encoding="ascii")
        return pbf, sidecar

    def _import_tiny_geofabrik(self, root: Path, pbf: Path, sidecar: Path):
        return import_geofabrik_pbf(
            input_path=pbf,
            checksum_path=sidecar,
            output_root=root,
            event_id="test-event",
            event_date=EVENT_DATE,
            bbox_wgs84=BOUNDS,
            source_url=f"https://download.geofabrik.de/asia/{pbf.name}",
            checksum_url=f"https://download.geofabrik.de/asia/{pbf.name}.md5",
            source_page_url=f"https://download.geofabrik.de/asia/{pbf.name.rsplit('-', 1)[0]}.html",
            categories=("roads", "settlements"),
        )

    def test_geofabrik_pbf_is_checksum_verified_and_imported_with_date_level_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pbf, sidecar = self._write_tiny_geofabrik_source(root)
            imported = self._import_tiny_geofabrik(root, pbf, sidecar)
            self.assertEqual(imported["temporal_verification_status"], "provider_dated_historical")
            self.assertEqual(imported["provider_snapshot_date"], "2026-08-01")
            self.assertEqual(imported["source_pbf_path"], pbf.name)
            self.assertEqual(imported["attribution"], "© OpenStreetMap contributors.")
            self.assertTrue(audit_snapshot(imported["snapshot_directory"])["historical_use_allowed"])
            collection = json.loads(Path(imported["source_artifact_path"]).read_text())
            self.assertEqual({item["id"] for item in collection["features"]}, {"node/1", "way/10"})
            prepared = prepare_infrastructure(snapshot_dir=imported["snapshot_directory"], output_dir=root / "prepared")
            self.assertEqual(prepared["temporal_verification_status"], "provider_dated_historical")

    def test_geofabrik_pbf_checksum_and_event_date_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pbf, sidecar = self._write_tiny_geofabrik_source(root)
            sidecar.write_text("0" * 32 + "  " + pbf.name + "\n", encoding="ascii")
            with self.assertRaisesRegex(InfrastructureError, "MD5 does not match"):
                self._import_tiny_geofabrik(root, pbf, sidecar)
            sidecar.write_text(hashlib.md5(pbf.read_bytes()).hexdigest() + "  " + pbf.name + "\n", encoding="ascii")
            with self.assertRaises(InfrastructureError) as caught:
                import_geofabrik_pbf(
                    input_path=pbf, checksum_path=sidecar, output_root=root,
                    event_id="test-event", event_date="2026-08-01", bbox_wgs84=BOUNDS,
                    source_url=f"https://download.geofabrik.de/asia/{pbf.name}",
                    checksum_url=f"https://download.geofabrik.de/asia/{pbf.name}.md5",
                    source_page_url=f"https://download.geofabrik.de/asia/{pbf.name.rsplit('-', 1)[0]}.html",
                )
            self.assertEqual(caught.exception.code, "snapshot_not_pre_event")

    def test_geofabrik_audit_rechecks_raw_pbf_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pbf, sidecar = self._write_tiny_geofabrik_source(root)
            imported = self._import_tiny_geofabrik(root, pbf, sidecar)
            pbf.write_bytes(pbf.read_bytes() + b"changed")
            with self.assertRaises(InfrastructureError) as caught:
                audit_snapshot(imported["snapshot_directory"])
            self.assertEqual(caught.exception.code, "pbf_provenance_mismatch")

    def test_geofabrik_import_rejects_nonofficial_or_inconsistent_asset_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pbf, sidecar = self._write_tiny_geofabrik_source(root)
            with self.assertRaises(InfrastructureError) as caught:
                import_geofabrik_pbf(
                    input_path=pbf, checksum_path=sidecar, output_root=root,
                    event_id="test-event", event_date=EVENT_DATE, bbox_wgs84=BOUNDS,
                    source_url=f"https://example.com/{pbf.name}",
                    checksum_url=f"https://example.com/{pbf.name}.md5",
                    source_page_url=f"https://download.geofabrik.de/asia/{pbf.name.rsplit('-', 1)[0]}.html",
                )
            self.assertEqual(caught.exception.code, "geofabrik_source_invalid")


class ExposureGateAndMathTests(unittest.TestCase):
    def test_exposure_requires_an_existing_verified_snapshot_before_mask_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(InfrastructureError) as caught:
                calculate_exposure(snapshot_dir=temp, prepared_dir=temp, flood_mask_manifest_path=Path(temp) / "absent.json", event_id="x", event_date=EVENT_DATE, output_path=Path(temp) / "out.json", aoi_bbox_wgs84=BOUNDS)
            self.assertEqual(caught.exception.code, "snapshot_manifest_missing")

    def test_synthetic_mask_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "mask.json"
            manifest.write_text(json.dumps({"synthetic": True, "artifact_type": "flood_mask", "source_type": "model_inference"}))
            from geospatial.infrastructure import validate_flood_mask
            with self.assertRaisesRegex(InfrastructureError, "synthetic"):
                validate_flood_mask(manifest, event_id="x", event_date=EVENT_DATE)

    def test_unapproved_mask_is_rejected_before_opening_any_raster(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "mask.json"
            manifest.write_text(json.dumps({
                "synthetic": False, "artifact_type": "flood_mask", "source_type": "model_inference",
                "analysis_readiness": {"status": "pending"}, "event_id": "x", "event_date": EVENT_DATE,
            }))
            from geospatial.infrastructure import validate_flood_mask
            with self.assertRaisesRegex(InfrastructureError, "not approved"):
                validate_flood_mask(manifest, event_id="x", event_date=EVENT_DATE)

    def test_synthetic_projected_geometries_exercise_exposure_arithmetic(self):
        # Synthetic test geometry only; never persisted as JalDrishti event results.
        flood = box(40, -10, 60, 10)
        features = [
            ("way/1", {"categories": ["roads", "bridges"], "tags": {"highway": "primary"}}, LineString([(0, 0), (100, 0)])),
            ("way/2", {"categories": ["buildings"], "tags": {"building": "yes"}}, box(45, 1, 50, 6)),
            ("way/3", {"categories": ["buildings"], "tags": {"building": "yes"}}, box(70, 1, 75, 6)),
            ("node/4", {"categories": ["healthcare"], "tags": {"amenity": "hospital"}}, Point(50, 5)),
            ("node/5", {"categories": ["critical"], "tags": {"emergency": "fire_station"}}, Point(80, 5)),
            ("way/6", {"categories": ["railway"], "tags": {"railway": "rail"}}, LineString([(0, 2), (100, 2)])),
        ]
        result = measure_feature_exposure(features, flood)
        self.assertAlmostEqual(result["road_length_m"]["total_mapped"], 100)
        self.assertAlmostEqual(result["road_length_m"]["potentially_exposed"], 20)
        self.assertAlmostEqual(result["road_length_m"]["percent_potentially_exposed"], 20)
        self.assertEqual(result["buildings"], {"total_mapped": 2, "potentially_exposed": 1})
        self.assertEqual(result["healthcare_facilities"], {"total_mapped": 1, "potentially_exposed": 1})
        self.assertEqual(result["critical_facilities"], {"total_mapped": 1, "potentially_exposed": 0})
        self.assertEqual(result["bridges"]["total_mapped"], 1)
        self.assertEqual(result["bridges"]["potentially_exposed"], 1)
        self.assertEqual(result["railway_features"], {"total_mapped": 1, "potentially_exposed": 1})

    def test_small_synthetic_georeferenced_mask_polygonizes_in_its_crs(self):
        # Synthetic test raster only. It has no model provenance and is not accepted by validate_flood_mask.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic-mask.tif"
            data = np.array([[1, 0], [0, 1]], dtype=np.uint8)
            with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8", crs="EPSG:32645", transform=from_origin(500000, 3100000, 10, 10), nodata=255) as dst:
                dst.write(data, 1)
            geom, crs = _mask_to_geometry(path, 1)
            self.assertIsNotNone(geom)
            self.assertEqual(crs.to_epsg(), 32645)
            self.assertAlmostEqual(geom.area, 200)


if __name__ == "__main__":
    unittest.main()
