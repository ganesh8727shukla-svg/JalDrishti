import unittest
from datetime import date
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.app.api.v1 import observations as observations_api
from apps.api.app.main import app
from apps.api.app.schemas.observations import ObservationSearchQuery
from apps.api.app.services.satellite_catalogue import (
    COLLECTION_ID,
    STAC_SEARCH_URL,
    CatalogueSearchError,
    search_sentinel1,
)


TEST_STAC_RESPONSE = {
    "type": "FeatureCollection",
    "numberReturned": 1,
    "features": [
        {
            "type": "Feature",
            "id": "TEST_SENTINEL_1_GRD_ITEM",
            "collection": "sentinel-1-grd",
            "bbox": [84.0, 28.0, 84.2, 28.2],
            "geometry": {"type": "Polygon", "coordinates": []},
            "properties": {
                "datetime": "2026-08-28T12:21:41Z",
                "start_datetime": "2026-08-28T12:21:41Z",
                "end_datetime": "2026-08-28T12:22:06Z",
                "_private": {"product_name": "TEST_SENTINEL_1_GRD_ITEM.SAFE"},
                "product:type": "IW_GRDH_1S",
                "sat:orbit_state": "ascending",
                "sat:relative_orbit": 85,
                "sar:polarizations": ["VV", "VH"],
                "platform": "sentinel-1d",
            },
            "assets": {"Product": {"href": "https://example.invalid/product"}, "vv": {}},
            "links": [],
        }
    ],
    "links": [],
}


class ObservationQueryTests(unittest.TestCase):
    def test_valid_query_parses_bbox_and_formats_inclusive_utc_range(self) -> None:
        query = ObservationSearchQuery(
            bbox="84.04,28.20,84.15,28.31",
            start_date=date(2026, 8, 20),
            end_date=date(2026, 9, 10),
            limit=5,
        )
        self.assertEqual(query.bbox, (84.04, 28.2, 84.15, 28.31))
        self.assertEqual(
            query.to_stac_datetime(),
            "2026-08-20T00:00:00Z/2026-09-10T23:59:59.999999Z",
        )

    def test_invalid_bbox_and_reversed_dates_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ObservationSearchQuery(
                bbox="84.15,28.20,84.04,28.31",
                start_date=date(2026, 8, 20),
                end_date=date(2026, 9, 10),
            )
        with self.assertRaises(ValueError):
            ObservationSearchQuery(
                bbox="84.04,28.20,84.15,28.31",
                start_date=date(2026, 9, 10),
                end_date=date(2026, 8, 20),
            )


class CatalogueClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = ObservationSearchQuery(
            bbox="84.04,28.20,84.15,28.31",
            start_date=date(2026, 8, 20),
            end_date=date(2026, 9, 10),
            limit=5,
        )

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_normalizes_item_metadata_from_stac_response(self, post: unittest.mock.Mock) -> None:
        post.return_value = httpx.Response(200, json=TEST_STAC_RESPONSE)
        result = search_sentinel1(self.query)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.returned_count, 1)
        observation = result.observations[0]
        self.assertEqual(observation.scene_id, "TEST_SENTINEL_1_GRD_ITEM")
        self.assertEqual(observation.product_name, "TEST_SENTINEL_1_GRD_ITEM.SAFE")
        self.assertEqual(observation.relative_orbit, 85)
        self.assertEqual(observation.orbit_direction, "ascending")
        self.assertEqual(observation.polarization, ["VV", "VH"])
        self.assertEqual(observation.product_type, "IW_GRDH_1S")

        args, kwargs = post.call_args
        self.assertEqual(args[0], STAC_SEARCH_URL)
        self.assertEqual(kwargs["json"]["collections"], [COLLECTION_ID])
        self.assertEqual(kwargs["json"]["limit"], 5)
        self.assertEqual(kwargs["json"]["bbox"], [84.04, 28.2, 84.15, 28.31])

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_empty_catalogue_response_is_no_data(self, post: unittest.mock.Mock) -> None:
        post.return_value = httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": [], "links": [], "numberReturned": 0},
        )
        result = search_sentinel1(self.query)
        self.assertEqual(result.status, "no_data")
        self.assertEqual(result.returned_count, 0)
        self.assertEqual(result.observations, [])
        self.assertTrue(result.warnings)

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_timeout_is_reported_without_fallback(self, post: unittest.mock.Mock) -> None:
        post.side_effect = httpx.TimeoutException("timeout")
        with self.assertRaises(CatalogueSearchError) as caught:
            search_sentinel1(self.query)
        self.assertEqual(caught.exception.code, "catalogue_timeout")

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_http_error_is_reported(self, post: unittest.mock.Mock) -> None:
        post.return_value = httpx.Response(503, json={"detail": "temporary outage"})
        with self.assertRaises(CatalogueSearchError) as caught:
            search_sentinel1(self.query)
        self.assertEqual(caught.exception.code, "catalogue_unavailable")
        self.assertEqual(caught.exception.upstream_status, 503)

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_invalid_stac_response_is_reported(self, post: unittest.mock.Mock) -> None:
        post.return_value = httpx.Response(200, json={"items": []})
        with self.assertRaises(CatalogueSearchError) as caught:
            search_sentinel1(self.query)
        self.assertEqual(caught.exception.code, "invalid_catalogue_response")

    @patch("apps.api.app.services.satellite_catalogue.httpx.post")
    def test_non_json_success_response_preserves_upstream_status(self, post: unittest.mock.Mock) -> None:
        post.return_value = httpx.Response(
            200,
            content=b"<html>Request Rejected</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        with self.assertRaises(CatalogueSearchError) as caught:
            search_sentinel1(self.query)
        self.assertEqual(caught.exception.code, "invalid_catalogue_response")
        self.assertEqual(caught.exception.upstream_status, 200)
        self.assertIn("text/html", caught.exception.message)


class ObservationEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.params = {
            "bbox": "84.04,28.20,84.15,28.31",
            "start_date": "2026-08-20",
            "end_date": "2026-09-10",
            "limit": 5,
        }

    def test_phase_one_health_endpoint_still_works(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch.object(observations_api, "search_sentinel1")
    def test_search_route_returns_catalogue_response(self, search: unittest.mock.Mock) -> None:
        search.return_value = search_sentinel1_from_test_fixture(self.params)
        response = self.client.get("/api/v1/observations/search", params=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["observations"][0]["scene_id"], "TEST_SENTINEL_1_GRD_ITEM")
        search.assert_called_once()

    @patch.object(observations_api, "search_sentinel1")
    def test_invalid_query_returns_422_without_catalogue_request(self, search: unittest.mock.Mock) -> None:
        bad_params = {**self.params, "bbox": "84.15,28.20,84.04,28.31"}
        response = self.client.get("/api/v1/observations/search", params=bad_params)
        self.assertEqual(response.status_code, 422)
        search.assert_not_called()


def search_sentinel1_from_test_fixture(params: dict[str, object]):
    from apps.api.app.services.satellite_catalogue import search_sentinel1 as real_search

    with patch(
        "apps.api.app.services.satellite_catalogue.httpx.post",
        return_value=httpx.Response(200, json=TEST_STAC_RESPONSE),
    ):
        return real_search(ObservationSearchQuery(**params))


if __name__ == "__main__":
    unittest.main()
