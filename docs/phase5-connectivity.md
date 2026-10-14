# Phase 5 — Road connectivity groundwork

This is a separate, additive connectivity service. It does not claim that a road is closed or that a settlement has lost real-world access.

## Implemented

- The OSM query/category rules can include explicitly tagged city, town, village, hamlet, and isolated-dwelling places. Geometry alone never classifies a settlement.
- Prepared OSM road lines are projected to the recorded metric CRS, planar intersections are noded, and graph edges retain their OSM way IDs and road classes.
- Village, hamlet, and isolated-dwelling features are candidate origins. Explicitly tagged city/town places, hospitals, and other healthcare points are separate destination classes. Polygon features use a representative point, and all points snap to a graph vertex only within the configured distance.
- A comparison computes baseline shortest-path reachability, then a hypothetical scenario removes each road graph segment with positive-length overlap with an approved real flood mask. It reports a settlement as potentially disconnected only when a route existed in the baseline graph and none remains in that scenario.
- Production execution checks a checksum-verified OSM snapshot and prepared-geometry manifest, verified pre-event temporal status, matching event ID/date/AOI, and the Phase 4 approved real-inference mask contract.
- Results retain snapshot and model-mask provenance and are labeled as a counterfactual access scenario.

## Important limits

The scenario treats mask overlap as an assumed road unavailability; it is not an observed road closure or damage. The graph is undirected, does not account for one-way tags, vehicle restrictions, seasonal access, road condition, bridges/tunnels at different levels, travel time, or whether a facility is operating. It snaps points by straight-line distance to the nearest road vertex and does not model an entrance or last-mile path. A planar crossing may create a false graph connection, especially around a bridge or tunnel. OSM is incomplete and a verified history timestamp does not prove every real asset is mapped.

The ohsome live check on 2026-10-10 did not produce a snapshot: provider metadata ended on 2026-07-27, before the requested 2026-08-25 snapshot, and the extraction endpoint returned HTTP 403. A Geofabrik fallback was later imported and prepared for this AOI (snapshot `214b1809-502b-469f-bcdb-647e0e723d67`, provider date 2026-08-01, calendar-day precision). The current Sentinel pair remains marked `analysis_ready: false`; no approved model checkpoint or flood mask exists. Therefore the production CLI is still gated and there are no event-specific connectivity figures.

## Commands

From the repository root, install the backend dependencies (NetworkX is declared in apps/api/requirements.txt) and inspect the command:

    source .venv/bin/activate
    python -m pip install -r apps/api/requirements.txt
    python -m geospatial.phase5_cli --help

After a verified OSM snapshot with roads, settlements, and healthcare selected has been acquired and prepared, and Phase 3 provides an approved real mask manifest, run:

    python -m geospatial.phase5_cli scenario \
      data/raw/osm/<snapshot-id> \
      data/processed/infrastructure/<snapshot-id> \
      /path/to/approved-flood-mask-manifest.json \
      --event-id <event-id> \
      --event-date <YYYY-MM-DD> \
      --bbox <west> <south> <east> <north> \
      --output-root data/processed/connectivity \
      --max-snap-m 500

The scenario output is written to a new run directory under data/processed/connectivity/. It contains route reachability before and after the hypothetical edge-removal assumption, snap distances, graph counts, source IDs, warnings, and provenance. It does not output confirmed access loss.

## Tests

The tests use tiny synthetic projected roads and points in temporary directories. They cover intersection noding, a route removed by the hypothetical scenario, alternate routes, baseline disconnection, excessive snap distance, missing masks, explicit settlement tags, and the production gate rejecting a locally imported snapshot whose history is unverified. They do not generate project data or claim real infrastructure results.
