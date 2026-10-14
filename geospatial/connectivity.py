"""Road-network accessibility scenarios with strict real-input provenance gates.

Road-mask intersections are treated only as an explicit hypothetical edge-removal
scenario. This module does not claim that a road is closed or damaged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid
from typing import Any, Iterable

import networkx as nx
import rasterio
from rasterio.crs import CRS
from shapely.geometry import GeometryCollection, LineString, Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .infrastructure import (
    InfrastructureError,
    _mask_to_geometry,
    _read_json,
    _transform_geometry,
    audit_snapshot,
    load_snapshot,
    sha256_file,
    validate_bbox,
    validate_flood_mask,
)

CONNECTIVITY_SCHEMA = "jaldrishti.connectivity-scenario.v1"
ORIGIN_PLACES = {"village", "hamlet", "isolated_dwelling"}


class ConnectivityError(ValueError):
    """Connectivity input or analysis error with an actionable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _line_parts(geometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    parts: list[LineString] = []
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        for item in geometry.geoms:
            parts.extend(_line_parts(item))
    return parts


def _node_key(coord: tuple[float, ...]) -> tuple[float, float]:
    # Noding returns shared coordinates for shared topological vertices.
    return float(coord[0]), float(coord[1])


def build_road_graph(roads: Iterable[dict[str, Any]]) -> tuple[nx.MultiGraph, list[str]]:
    """Build an undirected, metric graph and node planar line intersections."""
    road_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in roads:
        osm_id = str(record.get("osm_id", "")).strip()
        geom = record.get("geometry")
        if not osm_id or geom is None:
            raise ConnectivityError("road_identity_missing", "Every road geometry must retain an OSM element ID.")
        if osm_id in seen_ids:
            continue
        if geom.geom_type not in {"LineString", "MultiLineString"} or geom.is_empty:
            continue
        seen_ids.add(osm_id)
        road_records.append({
            "osm_id": osm_id,
            "geometry": geom,
            "tags": dict(record.get("tags") or {}),
        })
    if not road_records:
        raise ConnectivityError("roads_missing", "The prepared infrastructure input contains no usable road line geometries.")

    noded = unary_union([item["geometry"] for item in road_records])
    noded_parts = _line_parts(noded)
    tree = STRtree([item["geometry"] for item in road_records])
    graph = nx.MultiGraph()
    graph.graph["topology"] = "2D planar linework noded with Shapely unary_union"
    graph.graph["weight_unit"] = "metres"
    graph.graph["directed"] = False
    edge_number = 0
    for part in noded_parts:
        coords = list(part.coords)
        for first, second in zip(coords, coords[1:]):
            segment = LineString([first, second])
            length = float(segment.length)
            if not math.isfinite(length) or length <= 0:
                continue
            source_indices = tree.query(segment)
            source_records = []
            for raw_index in source_indices:
                index = int(raw_index)
                source = road_records[index]
                # A small metric tolerance handles harmless GEOS roundoff while
                # still requiring a source way to contain the full graph segment.
                if source["geometry"].buffer(0.001).covers(segment):
                    source_records.append(source)
            if not source_records:
                raise ConnectivityError("road_source_mapping_failed", "Could not map a noded graph edge back to its source OSM way.")
            source_ids = sorted({item["osm_id"] for item in source_records})
            road_classes = sorted({str(item["tags"].get("highway", "unclassified")) for item in source_records})
            special_topology = any(
                item["tags"].get("bridge") not in (None, "", "no", "false", "0")
                or item["tags"].get("tunnel") not in (None, "", "no", "false", "0")
                for item in source_records
            )
            graph.add_edge(
                _node_key(first), _node_key(second), key=f"segment-{edge_number}",
                geometry=segment, length_m=length, source_ids=source_ids,
                road_classes=road_classes, bridge_or_tunnel=special_topology,
            )
            edge_number += 1
    if graph.number_of_edges() == 0:
        raise ConnectivityError("road_graph_empty", "No non-zero-length road edges could be constructed.")

    warnings = [
        "The graph is undirected and does not apply one-way, vehicle-access, seasonal, or road-condition restrictions.",
        "Planar 2D intersections are treated as connected; bridge, tunnel, and level-separated topology requires explicit OSM node-level validation.",
    ]
    if any(data.get("bridge_or_tunnel") for _, _, data in graph.edges(data=True)):
        warnings.append("The AOI includes bridge or tunnel tags; geometry-only noding may create false connections at grade-separated crossings.")
    return graph, warnings


def _nearest_node(point: Point, node_tree: STRtree, node_geometries: list[Point], node_keys: list[tuple[float, float]], max_snap_m: float) -> tuple[tuple[float, float], float]:
    index = int(node_tree.nearest(point))
    node = node_geometries[index]
    distance = float(point.distance(node))
    if not math.isfinite(distance) or distance > max_snap_m:
        raise ConnectivityError(
            "network_snap_too_far",
            f"Point is {distance:.1f} m from the nearest road node, exceeding max_snap_m={max_snap_m:.1f}.",
        )
    return node_keys[index], distance


def _routes_to_kind(
    before_graph: nx.MultiGraph,
    after_graph: nx.MultiGraph,
    origin_node: tuple[float, float],
    origin_snap_m: float,
    destinations: list[dict[str, Any]],
    kind: str,
) -> dict[str, Any]:
    selected = [item for item in destinations if item["kind"] == kind]
    if not selected:
        return {
            "status": "no_destinations_in_aoi",
            "destination_count": 0,
            "reachable_before_count": 0,
            "reachable_after_count": 0,
            "nearest_before": None,
            "nearest_after": None,
        }
    before_distances = nx.single_source_dijkstra_path_length(before_graph, origin_node, weight="length_m")
    after_distances = nx.single_source_dijkstra_path_length(after_graph, origin_node, weight="length_m")

    def nearest(distances):
        candidates = []
        for destination in selected:
            node = destination["road_node"]
            if node in distances:
                total = origin_snap_m + float(distances[node]) + float(destination["snap_distance_m"])
                candidates.append((total, destination["place_id"], destination))
        candidates.sort(key=lambda row: (row[0], row[1]))
        if not candidates:
            return None, 0
        total, _, destination = candidates[0]
        return {
            "place_id": destination["place_id"],
            "distance_m": total,
            "snap_distance_m": destination["snap_distance_m"],
        }, len(candidates)

    nearest_before, count_before = nearest(before_distances)
    nearest_after, count_after = nearest(after_distances)
    if nearest_before is None:
        status = "no_route_in_baseline_graph"
    elif nearest_after is None:
        status = "potentially_disconnected_under_scenario"
    else:
        status = "route_remains_under_scenario"
    return {
        "status": status,
        "destination_count": len(selected),
        "reachable_before_count": count_before,
        "reachable_after_count": count_after,
        "nearest_before": nearest_before,
        "nearest_after": nearest_after,
    }


def compute_connectivity_scenario(
    *,
    roads: Iterable[dict[str, Any]],
    places: Iterable[dict[str, Any]],
    flood_geometry,
    max_snap_m: float = 500.0,
    allow_empty_flood_geometry: bool = False,
) -> dict[str, Any]:
    """Run synthetic-testable undirected access before/after an explicit closure scenario.

    Input geometries must already use a projected CRS with metre units. Production
    entry points call this only after snapshot, AOI, mask and model checks pass.
    """
    if not math.isfinite(max_snap_m) or max_snap_m <= 0:
        raise ConnectivityError("invalid_snap_distance", "max_snap_m must be a finite positive distance.")
    graph_before, warnings = build_road_graph(roads)
    if flood_geometry is None or (flood_geometry.is_empty and not allow_empty_flood_geometry):
        raise ConnectivityError("flood_geometry_missing", "A validated flood geometry is required; missing data is not zero exposure.")
    node_keys = list(graph_before.nodes)
    node_geometries = [Point(node) for node in node_keys]
    node_tree = STRtree(node_geometries)

    locations: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in places:
        place_id = str(item.get("place_id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        geometry = item.get("geometry")
        if not place_id or place_id in ids:
            raise ConnectivityError("place_id_invalid", "Every settlement and destination must have a unique non-empty place_id.")
        if kind not in {"settlement", "hospital", "healthcare", "town"}:
            raise ConnectivityError("place_kind_invalid", "Point kind must be settlement, hospital, healthcare, or town.")
        if not isinstance(geometry, Point) or geometry.is_empty:
            raise ConnectivityError("place_geometry_invalid", "Connectivity points must be non-empty Point geometries in the graph CRS.")
        ids.add(place_id)
        node, distance = _nearest_node(geometry, node_tree, node_geometries, node_keys, max_snap_m)
        locations.append({
            "place_id": place_id,
            "kind": kind,
            "road_node": node,
            "snap_distance_m": distance,
        })
    settlements = [item for item in locations if item["kind"] == "settlement"]
    if not settlements:
        raise ConnectivityError("settlements_missing", "No village, hamlet, or isolated-dwelling origin points were found.")
    if not any(item["kind"] in {"hospital", "healthcare", "town"} for item in locations):
        raise ConnectivityError("destinations_missing", "No hospital, healthcare, or town destination points were found.")

    graph_after = nx.MultiGraph(graph_before)
    removed_edges = []
    for start, end, key, data in list(graph_after.edges(keys=True, data=True)):
        intersection = data["geometry"].intersection(flood_geometry)
        if not intersection.is_empty and float(intersection.length) > 0:
            removed_edges.append((start, end, key))
    graph_after.remove_edges_from(removed_edges)

    results = []
    for settlement in sorted(settlements, key=lambda item: item["place_id"]):
        by_kind = {
            kind: _routes_to_kind(
                graph_before, graph_after,
                settlement["road_node"], settlement["snap_distance_m"],
                locations, kind,
            )
            for kind in ("hospital", "healthcare", "town")
        }
        results.append({
            "settlement_id": settlement["place_id"],
            "road_snap_distance_m": settlement["snap_distance_m"],
            "access": by_kind,
        })
    potentially_disconnected = {
        kind: sum(
            result["access"][kind]["status"] == "potentially_disconnected_under_scenario"
            for result in results
        )
        for kind in ("hospital", "healthcare", "town")
    }
    return {
        "status": "scenario_only",
        "scenario": "Treat each road graph segment with positive-length overlap with the validated flood geometry as unavailable.",
        "graph": {
            "nodes": graph_before.number_of_nodes(),
            "road_segments": graph_before.number_of_edges(),
            "segments_removed_by_scenario": len(removed_edges),
            "crs_units": "projected metres (supplied by the verified preparation metadata)",
        },
        "settlement_count": len(settlements),
        "destination_count_by_kind": {
            kind: sum(item["kind"] == kind for item in locations)
            for kind in ("hospital", "healthcare", "town")
        },
        "potentially_disconnected_settlement_count_by_kind": potentially_disconnected,
        "settlements": results,
        "warnings": warnings + [
            "Removing an edge that overlaps a predicted flood mask is a hypothetical closure assumption, not evidence of road closure, damage, passability, rescue access, or service disruption.",
            "Point-to-road snapping uses straight-line distance to a graph vertex and does not model entrances, bridges, walking access, or travel time.",
            "Mapped OSM network completeness is not established by this calculation.",
        ],
    }


def _prepared_inputs(snapshot_dir: str | Path, prepared_dir: str | Path, event_id: str, event_date: str, bbox: Iterable[float]):
    snapshot_manifest, _ = load_snapshot(snapshot_dir)
    snapshot_audit = audit_snapshot(snapshot_dir)
    if not snapshot_audit.get("historical_use_allowed"):
        raise ConnectivityError("historical_snapshot_required", "Connectivity requires an auditable pre-event OSM snapshot (ohsome verified history or a checksum-verified provider-dated extract).")
    if str(snapshot_manifest.get("event_id")) != str(event_id) or str(snapshot_manifest.get("event_date")) != str(event_date):
        raise ConnectivityError("event_mismatch", "OSM snapshot event ID/date do not match the requested connectivity event.")
    requested_bbox = validate_bbox(bbox)
    snapshot_bbox = validate_bbox((snapshot_manifest.get("aoi") or {}).get("bbox_wgs84", []))
    if snapshot_bbox != requested_bbox:
        raise ConnectivityError("aoi_mismatch", "OSM snapshot AOI does not match the connectivity AOI.")

    prepared_root = Path(prepared_dir).expanduser().resolve()
    audit_path = prepared_root / "processing_metadata.json"
    audit = _read_json(audit_path)
    if audit.get("snapshot_id") != snapshot_manifest.get("snapshot_id"):
        raise ConnectivityError("prepared_snapshot_mismatch", "Prepared geometry belongs to a different OSM snapshot.")
    if audit.get("temporal_verification_status") != snapshot_manifest.get("temporal_verification_status"):
        raise ConnectivityError("historical_snapshot_required", "Prepared OSM geometry is not verified as historical.")
    if str(audit.get("event_id")) != str(event_id) or str(audit.get("event_date")) != str(event_date):
        raise ConnectivityError("event_mismatch", "Prepared OSM event ID/date do not match.")
    if validate_bbox(audit.get("aoi_bbox_wgs84", [])) != requested_bbox:
        raise ConnectivityError("aoi_mismatch", "Prepared OSM AOI does not match the requested AOI.")
    output_file = str(audit.get("output_file", ""))
    if not output_file or Path(output_file).name != output_file:
        raise ConnectivityError("prepared_path_invalid", "Prepared OSM output_file must be a safe filename.")
    prepared_path = prepared_root / output_file
    if not prepared_path.is_file() or sha256_file(prepared_path) != audit.get("output_sha256"):
        raise ConnectivityError("prepared_checksum_mismatch", "Prepared OSM GeoJSON is missing or its checksum does not match.")
    if audit.get("output_geometry_crs") != "EPSG:4326":
        raise ConnectivityError("prepared_crs_unsupported", "Prepared infrastructure geometries must use the recorded EPSG:4326 output CRS.")
    try:
        collection = json.loads(prepared_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectivityError("prepared_geojson_invalid", "Prepared OSM GeoJSON cannot be read.") from exc
    if collection.get("type") != "FeatureCollection" or not isinstance(collection.get("features"), list):
        raise ConnectivityError("prepared_geojson_invalid", "Prepared OSM output is not a GeoJSON FeatureCollection.")
    try:
        metric_crs = CRS.from_user_input(audit["measurement_crs"])
    except Exception as exc:
        raise ConnectivityError("prepared_crs_invalid", "Prepared OSM measurement CRS is invalid.") from exc
    if not metric_crs.is_projected or metric_crs.linear_units_factor[1] != 1.0:
        raise ConnectivityError("prepared_crs_invalid", "Prepared OSM measurement CRS must be projected in metres.")
    return snapshot_manifest, audit, collection, metric_crs, requested_bbox


def _extract_roads_and_places(collection: dict[str, Any], metric_crs: CRS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roads: list[dict[str, Any]] = []
    places: list[dict[str, Any]] = []
    for feature in collection["features"]:
        properties = feature.get("properties") or {}
        categories = set(properties.get("categories") or [])
        tags = properties.get("tags") or {}
        osm_id = str(properties.get("osm_id", feature.get("id", "")))
        if not osm_id:
            raise ConnectivityError("osm_id_missing", "Prepared OSM features must retain their OSM element IDs.")
        try:
            geometry = shape(feature["geometry"])
        except Exception as exc:
            raise ConnectivityError("prepared_geometry_invalid", f"Prepared feature {osm_id} has invalid geometry.") from exc
        if "roads" in categories and geometry.geom_type in {"LineString", "MultiLineString"}:
            roads.append({
                "osm_id": osm_id,
                "geometry": _transform_geometry(geometry, "EPSG:4326", metric_crs),
                "tags": tags,
            })
        place_value = str(tags.get("place", ""))
        if "settlements" in categories and place_value in ORIGIN_PLACES:
            point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
            places.append({
                "place_id": osm_id,
                "kind": "settlement",
                "geometry": _transform_geometry(point, "EPSG:4326", metric_crs),
            })
        elif "settlements" in categories and place_value in {"city", "town"}:
            point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
            places.append({
                "place_id": osm_id,
                "kind": "town",
                "geometry": _transform_geometry(point, "EPSG:4326", metric_crs),
            })
        if "healthcare" in categories:
            if tags.get("amenity") == "hospital" or tags.get("healthcare") == "hospital":
                healthcare_kind = "hospital"
            else:
                healthcare_kind = "healthcare"
            point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
            places.append({
                "place_id": osm_id,
                "kind": healthcare_kind,
                "geometry": _transform_geometry(point, "EPSG:4326", metric_crs),
            })
    # An OSM way can be tagged both as road and place; point role IDs must remain unique.
    unique_places = {item["place_id"]: item for item in places}
    return roads, list(unique_places.values())


def run_connectivity_scenario(
    *,
    snapshot_dir: str | Path,
    prepared_dir: str | Path,
    mask_manifest_path: str | Path,
    event_id: str,
    event_date: str,
    aoi_bbox_wgs84: Iterable[float],
    output_root: str | Path = "data/processed/connectivity",
    max_snap_m: float = 500.0,
) -> dict[str, Any]:
    """Run only after verified historical OSM and approved real mask validation."""
    snapshot, prepared, collection, metric_crs, bbox = _prepared_inputs(
        snapshot_dir, prepared_dir, event_id, event_date, aoi_bbox_wgs84,
    )
    mask = validate_flood_mask(
        mask_manifest_path, event_id=event_id, event_date=event_date,
        aoi_bbox_wgs84=bbox,
    )
    roads, places = _extract_roads_and_places(collection, metric_crs)
    if not roads:
        raise ConnectivityError("roads_missing", "Prepared OSM input has no road category; include roads in the snapshot.")
    semantics = mask["manifest"]["mask_semantics"]
    flood_geometry, mask_crs = _mask_to_geometry(mask["artifact_path"], int(semantics["flood_value"]))
    flood_metric = GeometryCollection() if flood_geometry is None else _transform_geometry(flood_geometry, mask_crs, metric_crs)
    result = compute_connectivity_scenario(
        roads=roads, places=places, flood_geometry=flood_metric, max_snap_m=max_snap_m,
        allow_empty_flood_geometry=True,
    )
    if flood_geometry is None:
        result["warnings"].append("The approved real mask has no flood-class pixels; this is distinct from missing or invalid mask data.")
    run_id = str(uuid.uuid4())
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "schema_version": CONNECTIVITY_SCHEMA,
        "run_id": run_id,
        "event_id": str(event_id),
        "event_date": str(event_date),
        "aoi_bbox_wgs84": list(bbox),
        "generated_at": retrieved,
        "interpretation": "counterfactual_access_scenario_not_observed_closure",
        "osm_snapshot_id": snapshot.get("snapshot_id"),
        "osm_snapshot_timestamp": snapshot.get("verified_underlying_snapshot_timestamp"),
        "osm_snapshot_status": snapshot.get("temporal_verification_status"),
        "prepared_geometry_sha256": prepared.get("output_sha256"),
        "flood_mask_inference_run_id": mask["manifest"].get("inference_run_id"),
        "flood_mask_artifact_sha256": mask["manifest"].get("artifact_sha256"),
        "flood_mask_validation_status": "approved_real_inference_artifact",
        "measurement_crs": metric_crs.to_string(),
        "max_snap_m": max_snap_m,
        "scenario_result": result,
        "attribution": snapshot.get("attribution"),
        "attribution_url": snapshot.get("attribution_url"),
    }
    root = Path(output_root).expanduser().resolve()
    run_directory = root / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    output_path = run_directory / "connectivity_scenario.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["output_path"] = str(output_path)
    payload["output_sha256"] = sha256_file(output_path)
    return payload
