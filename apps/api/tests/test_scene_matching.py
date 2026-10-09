import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import shape

from apps.api.app.api.v1 import observations as observations_api
from apps.api.app.main import app
from apps.api.app.schemas.observation_pairs import ObservationPairSearchQuery
from apps.api.app.schemas.observations import ObservationMetadata, ObservationSearchResponse
from apps.api.app.services.scene_matching import rank_observation_pairs

AOI = (84.04, 28.20, 84.15, 28.31)
EVENT_DATE = date(2026, 8, 26)
DEFAULT_POLARIZATION = object()
FULL_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[84.0, 28.0], [84.2, 28.0], [84.2, 28.4], [84.0, 28.4], [84.0, 28.0]]],
}
PARTIAL_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[84.0, 28.0], [84.1, 28.0], [84.1, 28.4], [84.0, 28.4], [84.0, 28.0]]],
}
DISJOINT_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[85.0, 28.0], [85.2, 28.0], [85.2, 28.4], [85.0, 28.4], [85.0, 28.0]]],
}


def observation(
    scene_id: str,
    acquired_at: str,
    *,
    footprint: dict | None = FULL_FOOTPRINT,
    orbit_direction: str | None = "ascending",
    relative_orbit: int | None = 158,
    polarization: list[str] | None | object = DEFAULT_POLARIZATION,
    product_type: str | None = "IW_GRDH_1S",
    collection: str = "sentinel-1-grd",
) -> ObservationMetadata:
    return ObservationMetadata(
        scene_id=scene_id,
        product_name=f"{scene_id}.SAFE",
        collection=collection,
        product_type=product_type,
        acquisition_start=datetime.fromisoformat(acquired_at.replace("Z", "+00:00")),
        acquisition_end=None,
        polarization=["VV", "VH"] if polarization is DEFAULT_POLARIZATION else polarization,
        orbit_direction=orbit_direction,
        relative_orbit=relative_orbit,
        footprint=footprint,
        catalogue_source="https://stac.dataspace.copernicus.eu/v1/search",
        metadata_retrieved_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
    )


def evaluated_pair(pre: ObservationMetadata, post: ObservationMetadata):
    return rank_observation_pairs([pre], [post], AOI, EVENT_DATE)[0]


class PairQueryTests(unittest.TestCase):
    def test_accepts_a_different_configured_area_and_event(self) -> None:
        query = ObservationPairSearchQuery(
            bbox=(-74.1, 40.6, -73.9, 40.8),
            event_date=date(2025, 1, 10),
            pre_event_start=date(2025, 1, 1),
            pre_event_end=date(2025, 1, 9),
            post_event_start=date(2025, 1, 11),
            post_event_end=date(2025, 1, 20),
        )
        self.assertEqual(query.bbox, (-74.1, 40.6, -73.9, 40.8))
        self.assertEqual(query.pre_query().start_date, date(2025, 1, 1))
        self.assertEqual(query.post_query().end_date, date(2025, 1, 20))

    def test_rejects_invalid_bbox_and_windows_that_include_event_date(self) -> None:
        values = {
            "bbox": "84.15,28.20,84.04,28.31",
            "event_date": EVENT_DATE,
            "pre_event_start": date(2026, 8, 20),
            "pre_event_end": date(2026, 8, 25),
            "post_event_start": date(2026, 8, 27),
            "post_event_end": date(2026, 9, 10),
        }
        with self.assertRaises(ValueError):
            ObservationPairSearchQuery(**values)
        with self.assertRaises(ValueError):
            ObservationPairSearchQuery(**{**values, "bbox": AOI, "pre_event_end": EVENT_DATE})


class SceneMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pre = observation("pre", "2026-08-21T12:29:46.705159Z")
        self.post = observation("post", "2026-09-02T12:29:47.191710Z")

    def test_full_coverage_and_matching_metadata_are_compatible(self) -> None:
        pair = evaluated_pair(self.pre, self.post)
        self.assertEqual(pair.verification_status, "compatible")
        self.assertEqual(pair.common_aoi_coverage, "full")
        self.assertEqual(pair.orbit_compatibility, "match")
        self.assertEqual(pair.polarization_compatibility, "match")
        self.assertAlmostEqual(pair.elapsed_days or 0, 12.000005631, places=6)
        self.assertEqual(shape(pair.common_footprint_intersection).geom_type, "Polygon")

    def test_ranking_prefers_twelve_day_separation_after_compatibility(self) -> None:
        farther_post = observation("post-20-day", "2026-09-10T12:29:47Z")
        pairs = rank_observation_pairs(
            [self.pre],
            [farther_post, self.post],
            AOI,
            EVENT_DATE,
        )
        self.assertEqual([pair.post_scene_id for pair in pairs], ["post", "post-20-day"])
        self.assertEqual([pair.verification_status for pair in pairs], ["compatible", "compatible"])

    def test_wrong_pre_event_date_is_unsuitable(self) -> None:
        pre = observation("late-pre", "2026-08-27T12:00:00Z")
        pair = evaluated_pair(pre, self.post)
        self.assertEqual(pair.temporal_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_wrong_post_event_date_is_unsuitable(self) -> None:
        post = observation("early-post", "2026-08-25T12:00:00Z")
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.temporal_compatibility, "mismatch")
        self.assertGreater(pair.elapsed_seconds or 0, 0)
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_reversed_acquisition_order_is_unsuitable(self) -> None:
        pre = observation("later-pre", "2026-09-02T12:29:47Z")
        post = observation("earlier-post", "2026-08-21T12:29:46Z")
        pair = evaluated_pair(pre, post)
        self.assertEqual(pair.temporal_compatibility, "mismatch")
        self.assertLess(pair.elapsed_seconds or 0, 0)
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_event_day_is_ambiguous_and_not_accepted(self) -> None:
        post = observation("event-day", "2026-08-26T12:00:00Z")
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.temporal_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_ascending_descending_mismatch_is_questionable(self) -> None:
        post = observation("descending", "2026-09-02T12:29:47Z", orbit_direction="descending")
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.orbit_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "questionable")

    def test_relative_orbit_mismatch_is_questionable(self) -> None:
        post = observation("other-track", "2026-09-02T12:29:47Z", relative_orbit=85)
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.orbit_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "questionable")

    def test_polarization_mismatch_is_questionable(self) -> None:
        post = observation("vv-only", "2026-09-02T12:29:47Z", polarization=["VV"])
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.polarization_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "questionable")

    def test_missing_orbit_and_polarization_are_not_verified(self) -> None:
        pre = observation(
            "missing-metadata",
            "2026-08-21T12:29:46Z",
            orbit_direction=None,
            relative_orbit=None,
            polarization=None,
        )
        pair = evaluated_pair(pre, self.post)
        self.assertEqual(pair.orbit_compatibility, "unknown")
        self.assertEqual(pair.polarization_compatibility, "unknown")
        self.assertEqual(pair.verification_status, "not_verified")
        self.assertTrue(pair.warnings)

    def test_partial_common_coverage_is_reported_without_a_percentage(self) -> None:
        pre = observation("partial", "2026-08-21T12:29:46Z", footprint=PARTIAL_FOOTPRINT)
        pair = evaluated_pair(pre, self.post)
        self.assertEqual(pair.pre_aoi_coverage, "partial")
        self.assertEqual(pair.common_aoi_coverage, "partial")
        self.assertEqual(pair.verification_status, "questionable")
        self.assertIsNotNone(pair.common_footprint_intersection)

    def test_no_spatial_intersection_is_unsuitable(self) -> None:
        post = observation("outside", "2026-09-02T12:29:47Z", footprint=DISJOINT_FOOTPRINT)
        pair = evaluated_pair(self.pre, post)
        self.assertEqual(pair.common_aoi_coverage, "none")
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_missing_or_invalid_geometry_is_unknown(self) -> None:
        missing = observation("missing-footprint", "2026-08-21T12:29:46Z", footprint=None)
        pair_missing = evaluated_pair(missing, self.post)
        self.assertEqual(pair_missing.common_aoi_coverage, "unknown")
        self.assertEqual(pair_missing.verification_status, "not_verified")

        invalid = {
            "type": "Polygon",
            "coordinates": [[[84.0, 28.0], [84.2, 28.4], [84.0, 28.4], [84.2, 28.0], [84.0, 28.0]]],
        }
        pair_invalid = evaluated_pair(
            observation("invalid-footprint", "2026-08-21T12:29:46Z", footprint=invalid),
            self.post,
        )
        self.assertEqual(pair_invalid.common_aoi_coverage, "unknown")
        self.assertEqual(pair_invalid.verification_status, "not_verified")

    def test_non_grd_product_is_unsuitable(self) -> None:
        pre = observation("not-grd", "2026-08-21T12:29:46Z", collection="sentinel-2-l2a")
        pair = evaluated_pair(pre, self.post)
        self.assertEqual(pair.product_compatibility, "mismatch")
        self.assertEqual(pair.verification_status, "unsuitable")

    def test_empty_catalogue_side_produces_no_pairs(self) -> None:
        self.assertEqual(rank_observation_pairs([], [self.post], AOI, EVENT_DATE), [])


class ObservationPairEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.params = {
            "bbox": "84.04,28.20,84.15,28.31",
            "event_date": "2026-08-26",
            "pre_event_start": "2026-08-20",
            "pre_event_end": "2026-08-25",
            "post_event_start": "2026-08-27",
            "post_event_end": "2026-09-10",
            "limit_per_period": 10,
        }

    def _search_response(self, observations: list[ObservationMetadata]) -> ObservationSearchResponse:
        return ObservationSearchResponse(
            status="completed" if observations else "no_data",
            query=ObservationPairSearchQuery(**self.params).pre_query(),
            catalogue_source="https://stac.dataspace.copernicus.eu/v1/search",
            returned_count=len(observations),
            has_more=False,
            metadata_retrieved_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
            observations=observations,
        )

    @patch.object(observations_api, "search_sentinel1")
    def test_pair_endpoint_uses_two_real_catalogue_windows(self, search: unittest.mock.Mock) -> None:
        search.side_effect = [
            self._search_response([observation("pre", "2026-08-21T12:29:46Z")]),
            self._search_response([observation("post", "2026-09-02T12:29:47Z")]),
        ]
        response = self.client.get("/api/v1/observations/pairs", params=self.params)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["pairs"][0]["verification_status"], "compatible")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args_list[0].args[0].start_date, date(2026, 8, 20))
        self.assertEqual(search.call_args_list[1].args[0].start_date, date(2026, 8, 27))

    @patch.object(observations_api, "search_sentinel1")
    def test_pair_endpoint_returns_no_data_for_empty_window(self, search: unittest.mock.Mock) -> None:
        search.side_effect = [self._search_response([]), self._search_response([])]
        response = self.client.get("/api/v1/observations/pairs", params=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_data")
        self.assertEqual(response.json()["pairs"], [])

    @patch.object(observations_api, "search_sentinel1")
    def test_pair_endpoint_rejects_bad_windows_before_catalogue_call(
        self,
        search: unittest.mock.Mock,
    ) -> None:
        bad_params = {**self.params, "pre_event_end": "2026-08-27"}
        response = self.client.get("/api/v1/observations/pairs", params=bad_params)
        self.assertEqual(response.status_code, 422)
        search.assert_not_called()

    @patch.object(observations_api, "search_sentinel1")
    def test_pair_endpoint_surfaces_catalogue_errors(self, search: unittest.mock.Mock) -> None:
        from apps.api.app.services.satellite_catalogue import CatalogueSearchError

        search.side_effect = CatalogueSearchError("catalogue_timeout", "timeout")
        response = self.client.get("/api/v1/observations/pairs", params=self.params)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "catalogue_timeout")


if __name__ == "__main__":
    unittest.main()
