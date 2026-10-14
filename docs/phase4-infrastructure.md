# Phase 4 — Infrastructure impact groundwork

This document describes the additive Phase 4 CLI in `geospatial/`. It prepares OSM infrastructure and defines a strict flood-mask handoff. It does **not** report event exposure: the current Sentinel-1 pre/post rasters remain `analysis_ready: false`; Phase 3 has an exploratory trained/evaluated checkpoint, but it is not deployment-approved and no real Nepal flood mask exists.

## What is implemented

- A bounded ohsome v1 historical GeoJSON acquisition adapter. It records the exact query, retrieval time, AOI, source metadata, feature IDs/tags, checksum, ODbL license, attribution, and the requested/verified history timestamp.
- A local Geofabrik `.osm.pbf` importer for regional snapshots when ohsome history is unavailable. It requires the completed PBF, the official `.md5` sidecar, exact official HTTPS URLs and a matching regional archive page; verifies the provider MD5 before reading; computes SHA-256; and streams only selected tagged features intersecting the supplied AOI into a separate derived GeoJSON. It leaves the source PBF untouched and never downloads it.
- Geofabrik provenance is recorded as `provider_dated_historical` with **calendar-day precision** from the provider filename. It records that it has no per-feature snapshot timestamps. This dated, checksum-verified provider snapshot is accepted by the exposure/connectivity provenance gates as pre-event evidence when its date precedes the event; reports must retain the date-level precision and coverage caveats. It is not represented as ohsome's exact `verified_historical` timestamp evidence.
- Historical verification checks that the requested timestamp is strictly before the configured event cutoff, falls within the source `/metadata` temporal extent, and matches every feature's `@snapshotTimestamp`. Where present, `@lastEdit` must not be later than the snapshot. This verifies the queried database state, not OSM completeness or whether every mapped feature physically existed.
- Local GeoJSON import requires an explicit CRS and OSM element IDs. A supplied filename or timestamp never verifies history; local imports remain `historical_unverified`.
- Feature tagging preserves source tags and element IDs. Selection supports roads, buildings, bridges, healthcare facilities, emergency services, railway stations, and explicitly tagged settlements. Bridges tagged on a road remain a separate subset; don't add bridge counts to road totals.
- AOI clipping, geometry validation/repair, reprojection to WGS84 output coordinates, projected-meter road length/building area measurements, and a machine-readable processing audit.
- A fail-closed flood-mask validator and exposure calculator. Production calculations require a real inference raster, a matching event and AOI, raster/manifest consistency, explicit class and NoData encoding, artifact/checkpoint hashes, an approved acceptance policy, and a checksummed independent-validation report tied to the same mask, event, and AOI. The referenced validation raster must remain under the Git-ignored `data/validation/` tree and be marked validation-only. Missing or unapproved masks are errors, never zero-exposure results.
- A reference-comparison and explicit review handoff in `ml.src.independent_validation`. The first command computes metrics but leaves the mask pending review. A second command applies human-selected thresholds and policy; only then can `check-mask`, exposure, and accepted analysis-artifact API URLs proceed.
- Synthetic geometry and raster fixtures test arithmetic only. They are not copied to project data folders or emitted as event results.

## OSM source and migration

The adapter currently uses the official ohsome v1 `/metadata` and `/elements/geometry` endpoints. The v1 extraction supports a historical `time` parameter, OSM tags/metadata, and GeoJSON. The verifier checks the returned feature snapshot timestamp and the OSHDB temporal extent. See [ohsome v1 endpoints](https://docs.ohsome.org/ohsome-api/development/endpoints.html), [response metadata](https://docs.ohsome.org/ohsome-api/development/response-parameters.html), and [UTC time syntax](https://docs.ohsome.org/ohsome-api/development/time.html).

This is a **transition adapter**: the official migration guide says ohsome v1 shuts down on **November 30, 2026**. ohsome v2 requires an API key and its extraction result is GeoParquet, so v2 migration and GeoParquet import are still needed before long-term use. See the [official migration guide](https://docs.ohsome.org/ohsome-api/v2/migration_guide.html). No API key is needed by the current v1 adapter. Fetching is capped at 256 MiB by default; lower the AOI/category scope or explicitly raise the cap only after checking disk space.

## Dependencies and storage

The implementation uses `httpx`, `shapely`, `rasterio`, and PyOsmium (`osmium`). PyOsmium reads the local PBF using a file-backed sparse node-location index; the command checks free space and caps derived GeoJSON output. It does not add GeoPandas, SNAP, a database, or a task queue. Keep the downloaded regional PBF under `data/raw/osm/`. Derived source snapshots are written under `data/raw/osm/<snapshot-id>/`; prepared geometry is written under `data/processed/infrastructure/<snapshot-id>/`. Existing `.gitignore` rules exclude raw and processed data. Manifests and attribution files stay next to their artifacts, but satellite data and large OSM inputs are not committed.

Required attribution is `© OpenStreetMap contributors.` with <https://www.openstreetmap.org/copyright>. The ohsome API's own source response is checked against its documented text (without the final period), then JalDrishti writes the required credit with the period to its manifest and attribution file. OSM data is under ODbL; assess attribution and share-alike obligations for any distributed derived database. This OSM credit does not imply that OSM contributors validated a flood mask or impact estimate. Sentinel-derived products retain their separate attribution: `Contains modified Copernicus Sentinel data 2026.` See [dataset and license rules](data-sources.md).

## Commands

Run commands from `~/Desktop/jaldrishti` with the project environment active:

```sh
source .venv/bin/activate
python -m geospatial.phase4_cli --help
```

Example bounded historical query (the bbox is the project's diagnostic AOI, not a confirmed flood boundary). Choose a snapshot time strictly before the event cutoff. The request will fail if the live provider metadata does not cover that date; it will not fall back to current OSM.

```sh
python -m geospatial.phase4_cli fetch-osm \
  --event-id rasuwa-2026-08-26 \
  --event-date 2026-08-26 \
  --snapshot-time 2026-08-25T00:00:00Z \
  --bbox 84.04 28.20 84.15 28.31 \
  --output-root data/raw/osm
```

The output prints a manifest summary including `snapshot_id` and `snapshot_directory`. A successful fetch is evidence only for the requested historical OSM database state and selected AOI/features. The actual feature count and response must come from the live request.

Audit and prepare the returned snapshot using its printed directory:

```sh
python -m geospatial.phase4_cli audit-osm data/raw/osm/<snapshot-id>
python -m geospatial.phase4_cli prepare data/raw/osm/<snapshot-id>
```

A local OSM GeoJSON can be copied without granting it historical status. Specify the actual CRS; never use a timestamp in a filename as proof of history:

```sh
python -m geospatial.phase4_cli import-osm \
  --input /path/to/infrastructure.geojson \
  --event-id <event-id> --event-date <YYYY-MM-DD> \
  --source-crs EPSG:4326 --output-root data/raw/osm
```

For a completed Geofabrik PBF, first confirm the PBF transfer and sidecar request finished, then compare the printed MD5 from `md5 -q` with the hash in the sidecar. Run the importer only after that check. It also independently validates that hash, the official URLs, the date embedded in the filename, available disk space, PBF readability, and selected AOI feature extraction. The following command matches the user's downloaded Nepal archive; change the event, region URLs, categories, and bbox to the actual analysis request:

```sh
python -m geospatial.phase4_cli import-geofabrik \
  --input data/raw/osm/nepal-260801.osm.pbf \
  --md5-sidecar /tmp/nepal-260801.osm.pbf.md5 \
  --source-url https://download.geofabrik.de/asia/nepal-260801.osm.pbf \
  --checksum-url https://download.geofabrik.de/asia/nepal-260801.osm.pbf.md5 \
  --source-page-url https://download.geofabrik.de/asia/nepal.html \
  --event-id rasuwa-2026-08-26 --event-date 2026-08-26 \
  --bbox 84.04 28.20 84.15 28.31 \
  --categories roads buildings healthcare bridges critical settlements \
  --output-root data/raw/osm
```

The PBF is never modified or copied. The import writes `infrastructure.geojson`, `snapshot_metadata.json`, and `attribution.txt` in the printed snapshot directory. Check the result before preparing it:

```sh
python -m geospatial.phase4_cli audit-osm data/raw/osm/<snapshot-id>
python -m geospatial.phase4_cli prepare data/raw/osm/<snapshot-id>
```

Audit rechecks the derived GeoJSON hash and, for a Geofabrik snapshot, re-hashes the original PBF against the import manifest. Do not delete or move the PBF while relying on this manifest. A PBF can be read only after its download is complete; an interrupted file will fail the provider checksum or PBF parser checks. The Nepal PBF transfer and import were completed and verified on this machine; see the live-data status below.

Check a future model output's readiness after Phase 3 writes a mask manifest that satisfies the documented contract:

```sh
python -m geospatial.phase4_cli check-mask /path/to/flood-mask-manifest.json \
  --event-id <event-id> --event-date <YYYY-MM-DD> \
  --bbox <west> <south> <east> <north>
```

For a model-inference mask, the required validation record is generated by
comparing it with a separate, license-verified reference raster aligned to the
same exact grid. Place the original reference and any aligned comparison copy
under `data/validation/<event>/`; the validator records hashes and provenance
and requires explicit attestations that the reference was not used as a model
input or training label. The first command creates a report with
`completed_pending_review` status. It does not approve a mask:

```sh
python -m ml.src.independent_validation validate-mask \
  --mask-manifest data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --reference-raster data/validation/<event>/reference_aligned.tif \
  --output reports/generated/<event>-independent-validation.json \
  --event-id <event-id> --event-date <YYYY-MM-DD> \
  --bbox <west> <south> <east> <north> \
  --reference-name '<reference product>' \
  --reference-url '<official HTTPS source page>' \
  --reference-license '<applicable license>' \
  --reference-flood-value 1 --reference-nodata-value 255 \
  --attest-independent-of-training-data \
  --attest-not-used-as-prediction-input

python -m ml.src.independent_validation accept-mask \
  --mask-manifest data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --report reports/generated/<event>-independent-validation.json \
  --reviewed-by '<reviewer>' --acceptance-policy-id '<policy-id>' \
  --minimum-flood-iou <0-to-1> --minimum-flood-recall <0-to-1> \
  --minimum-valid-coverage <0-to-1>
```

The separate alignment command creates that validation-only copy using nearest
neighbour for categorical classes, preserves the original, and writes a sidecar
with the source/output hashes and class mapping. The operator must provide the
actual source class values; the tool does not infer them:

```sh
python -m ml.src.independent_validation align-reference \
  --reference-raster data/validation/<event>/reference_original.tif \
  --mask-manifest data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --output data/validation/<event>/reference_aligned.tif \
  --source-flood-value <source-flood-class> \
  --source-non-flood-values <source-non-flood-class> \
  --source-nodata-value <source-nodata-class>
```

Review the class semantics and AOI before using the aligned copy for scoring.

Exposure is deliberately blocked until both an auditable pre-event OSM snapshot (`verified_historical` from ohsome or `provider_dated_historical` from a provider-dated, checksum-verified Geofabrik export) and the approved real mask contract exist:

```sh
python -m geospatial.phase4_cli exposure \
  data/raw/osm/<snapshot-id> \
  data/processed/infrastructure/<snapshot-id> \
  /path/to/flood-mask-manifest.json \
  --event-id <event-id> --event-date <YYYY-MM-DD> \
  --bbox <west> <south> <east> <north>
```

## Current live-data status and limits

A bounded live ohsome check was attempted on 2026-10-10 for the diagnostic AOI [84.04, 28.20, 84.15, 28.31], requesting 2026-08-25T00:00:00Z before the 2026-08-26 event cutoff. The official v1 /metadata endpoint returned HTTP 200 (API 1.10.4, attribution present), but its reported temporal extent ends at 2026-07-27T09:00:00Z; it does not cover the requested snapshot date. The historical /elements/geometry extraction returned HTTP 403 for both the documented POST and GET forms. The provider's generic HTML response says access is forbidden but does not identify the cause. That ohsome attempt created no snapshot and did not fall back to current OSM.

The ohsome blockers were the source's reported history ending before the requested date and the extraction service denying access. The local Geofabrik fallback has since been completed for the same AOI. On 2026-10-11, `data/raw/osm/nepal-260801.osm.pbf` was imported into snapshot `214b1809-502b-469f-bcdb-647e0e723d67`; the official MD5 (`c717967403ca87f24373d66165f54d29`) was verified, and `audit-osm` confirmed the snapshot artifact and source PBF integrity. The manifest records 8,147 selected features and provider date 2026-08-01. `prepare` completed 8,147 features into `data/processed/infrastructure/214b1809-502b-469f-bcdb-647e0e723d67/infrastructure_prepared.geojson`, with EPSG:4326 output coordinates and EPSG:32645 measurement CRS. The raw and prepared data are excluded from Git. This fallback provides calendar-day provenance only: it does not prove exact per-feature edit timestamps or map completeness. ohsome v2 credentials and a v2/GeoParquet adapter remain future work; no key was configured here.

No project flood mask exists: the latest Sentinel preprocessing manifest records matched, finite linear Sigma0 outputs but sets analysis_ready: false and model_input_status: blocked_unknown_model_contract. An exploratory model has one partial-subset held-out evaluation, but its report sets deployment_approved=false and inference_eligible=false; no Nepal acceptance threshold or transfer validation exists. Therefore a real impact report is blocked, by design; the prepared OSM data alone must not be used to infer flood exposure.

OSM extraction, OSM preparation, and exposure remain CLI operations; there is no endpoint that starts those potentially expensive jobs. A separate read-only case-study API exposes inventory counts only when an already prepared snapshot matches the completed Sentinel pair's event date and AOI. It does not intersect infrastructure with a flood mask or calculate exposure/access impacts. The asynchronous `POST /api/v1/analyses` and `GET /api/v1/analyses/{analysis_id}` endpoints run approval-gated model inference for an existing preprocessing job. They keep results unavailable and return a blocked status while model/input readiness fails. After inference, mask and boundary artifacts remain unavailable until the Phase 4 independent-validation and acceptance gate passes. This local API has no authentication middleware; do not expose it to an untrusted network.

Routine tests are synthetic/mock-only and do not establish real OSM availability, accuracy, completeness, licensing advice, satellite model validity, damage, road closure, or service disruption.
