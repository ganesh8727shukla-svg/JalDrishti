from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from shapely.geometry import GeometryCollection, LineString, Point, box

from geospatial.phase4_cli import build_parser as build_phase4_parser
from geospatial.connectivity import (
    ConnectivityError,
    build_road_graph,
    compute_connectivity_scenario,
    run_connectivity_scenario,
)
from geospatial.infrastructure import categorize_tags, import_local_snapshot, prepare_infrastructure


def road(osm_id, coords, highway="primary"):
    return {
        "osm_id": osm_id,
        "geometry": LineString(coords),
        "tags": {"highway": highway},
    }


class RoadGraphTests(unittest.TestCase):
    def test_nodes_crossing_lines_as_a_shared_graph_vertex(self):
        graph, warnings = build_road_graph([
            road("way/1", [(-10, 0), (10, 0)]),
            road("way/2", [(0, -10), (0, 10)], highway="secondary"),
        ])
        self.assertEqual(graph.number_of_nodes(), 5)
        self.assertEqual(graph.number_of_edges(), 4)
        self.assertTrue(any("one-way" in warning for warning in warnings))
        self.assertTrue(any("2D intersections" in warning for warning in warnings))
        center = (0.0, 0.0)
        self.assertEqual(graph.degree(center), 4)

    def test_intersecting_flood_mask_edge_is_only_a_hypothetical_disconnection(self):
        result = compute_connectivity_scenario(
            roads=[road("way/road", [(0, 0), (100, 0)])],
            places=[
                {"place_id": "node/village", "kind": "settlement", "geometry": Point(0, 10)},
                {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(100, 10)},
            ],
            flood_geometry=box(49, -1, 51, 1),
            max_snap_m=20,
        )
        self.assertEqual(result["status"], "scenario_only")
        self.assertEqual(result["graph"]["segments_removed_by_scenario"], 1)
        self.assertEqual(result["potentially_disconnected_settlement_count_by_kind"]["hospital"], 1)
        access = result["settlements"][0]["access"]["hospital"]
        self.assertEqual(access["status"], "potentially_disconnected_under_scenario")
        self.assertTrue(any("not evidence of road closure" in warning for warning in result["warnings"]))

    def test_alternate_route_remains_connected_after_one_segment_is_removed(self):
        roads = [
            road("way/direct", [(0, 0), (100, 0)]),
            road("way/alt-west", [(0, 0), (0, 100)]),
            road("way/alt-north", [(0, 100), (100, 100)]),
            road("way/alt-east", [(100, 100), (100, 0)]),
        ]
        result = compute_connectivity_scenario(
            roads=roads,
            places=[
                {"place_id": "node/village", "kind": "settlement", "geometry": Point(0, 0)},
                {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(100, 0)},
            ],
            flood_geometry=box(49, -1, 51, 1),
            max_snap_m=1,
        )
        self.assertEqual(result["potentially_disconnected_settlement_count_by_kind"]["hospital"], 0)
        self.assertEqual(result["settlements"][0]["access"]["hospital"]["status"], "route_remains_under_scenario")

    def test_unreachable_in_baseline_is_not_reported_as_newly_disconnected(self):
        result = compute_connectivity_scenario(
            roads=[
                road("way/village", [(0, 0), (10, 0)]),
                road("way/hospital", [(100, 0), (110, 0)]),
            ],
            places=[
                {"place_id": "node/village", "kind": "settlement", "geometry": Point(0, 0)},
                {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(100, 0)},
            ],
            flood_geometry=box(4, -1, 6, 1),
            max_snap_m=1,
        )
        access = result["settlements"][0]["access"]["hospital"]
        self.assertEqual(access["status"], "no_route_in_baseline_graph")
        self.assertEqual(result["potentially_disconnected_settlement_count_by_kind"]["hospital"], 0)

    def test_point_far_from_network_is_rejected(self):
        with self.assertRaisesRegex(ConnectivityError, "exceeding max_snap_m"):
            compute_connectivity_scenario(
                roads=[road("way/1", [(0, 0), (10, 0)])],
                places=[
                    {"place_id": "node/village", "kind": "settlement", "geometry": Point(100, 100)},
                    {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(5, 0)},
                ],
                flood_geometry=box(4, -1, 6, 1),
                max_snap_m=20,
            )

    def test_empty_flood_geometry_is_not_interpreted_as_zero_exposure(self):
        with self.assertRaisesRegex(ConnectivityError, "validated flood geometry is required"):
            compute_connectivity_scenario(
                roads=[road("way/1", [(0, 0), (10, 0)])],
                places=[
                    {"place_id": "node/village", "kind": "settlement", "geometry": Point(0, 0)},
                    {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(10, 0)},
                ],
                flood_geometry=None,
            )

    def test_empty_flood_geometry_is_allowed_only_when_explicitly_validated(self):
        result = compute_connectivity_scenario(
            roads=[road("way/1", [(0, 0), (10, 0)])],
            places=[
                {"place_id": "node/village", "kind": "settlement", "geometry": Point(0, 0)},
                {"place_id": "node/hospital", "kind": "hospital", "geometry": Point(10, 0)},
            ],
            flood_geometry=GeometryCollection(),
            allow_empty_flood_geometry=True,
        )
        self.assertEqual(result["graph"]["segments_removed_by_scenario"], 0)
        self.assertEqual(result["settlements"][0]["access"]["hospital"]["status"], "route_remains_under_scenario")

    def test_phase4_fetch_cli_accepts_the_settlement_category(self):
        parsed = build_phase4_parser().parse_args([
            "fetch-osm", "--event-id", "e", "--event-date", "2026-08-26",
            "--snapshot-time", "2026-08-25T00:00:00Z",
            "--bbox", "84.04", "28.20", "84.15", "28.31",
            "--categories", "settlements",
        ])
        self.assertEqual(parsed.categories, ["settlements"])

    def test_settlement_category_uses_explicit_osm_place_tags(self):
        self.assertIn("settlements", categorize_tags({"place": "village"}))
        self.assertIn("settlements", categorize_tags({"place": "town"}))
        self.assertNotIn("settlements", categorize_tags({}))

    def test_invalid_place_roles_are_rejected(self):
        with self.assertRaisesRegex(ConnectivityError, "Point kind"):
            compute_connectivity_scenario(
                roads=[road("way/1", [(0, 0), (10, 0)])],
                places=[
                    {"place_id": "node/a", "kind": "building", "geometry": Point(0, 0)},
                ],
                flood_geometry=box(4, -1, 6, 1),
            )


class ConnectivityProvenanceGateTests(unittest.TestCase):
    def test_unverified_local_osm_import_cannot_run_production_connectivity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.geojson"
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "id": "way/7",
                    "geometry": {"type": "LineString", "coordinates": [[84.04, 28.25], [84.15, 28.25]]},
                    "properties": {"@osmId": "way/7", "tags": {"highway": "primary"}},
                }],
            }))
            imported = import_local_snapshot(
                input_path=source,
                output_root=root / "raw",
                event_id="event-1",
                event_date="2026-08-26",
                source_crs="EPSG:4326",
                bbox_wgs84=[84.04, 28.20, 84.15, 28.31],
            )
            prepared = prepare_infrastructure(
                snapshot_dir=imported["snapshot_directory"],
                output_dir=root / "prepared",
            )
            self.assertEqual(prepared["temporal_verification_status"], "historical_unverified")
            with self.assertRaises(ConnectivityError) as context:
                run_connectivity_scenario(
                    snapshot_dir=imported["snapshot_directory"],
                    prepared_dir=root / "prepared",
                    mask_manifest_path=root / "missing-mask.json",
                    event_id="event-1",
                    event_date="2026-08-26",
                    aoi_bbox_wgs84=[84.04, 28.20, 84.15, 28.31],
                    output_root=root / "results",
                )
            self.assertEqual(context.exception.code, "historical_snapshot_required")


if __name__ == "__main__":
    unittest.main()
