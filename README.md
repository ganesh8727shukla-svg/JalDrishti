# JalDrishti

Standalone flood intelligence MVP for the Multimodal AI Hackathon 2026, Track B.

## Current milestone

Phase 1 foundation plus Phase 2 Milestones 1–2: real CDSE Sentinel-1 metadata search and configurable pre/post pair verification and ranking. The frontend remains unchanged and does not load observations.

## Run locally

Use two Terminal tabs from the repository root (`~/Desktop/jaldrishti`).

### API

```sh
source .venv/bin/activate
python -m pip install -r apps/api/requirements.txt
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8765
```

Health check: `curl -i http://127.0.0.1:8765/api/v1/health`

### Search real Sentinel-1 observations

In another Terminal tab, run a bounded metadata-only search:

```sh
curl -G 'http://127.0.0.1:8765/api/v1/observations/search' \
  --data-urlencode 'bbox=84.04,28.20,84.15,28.31' \
  --data-urlencode 'start_date=2026-08-20' \
  --data-urlencode 'end_date=2026-09-10' \
  --data-urlencode 'limit=10'
```

The bounding box is an initial diagnostic area near the reported Rasuwa event location, not a validated flood boundary or final study-area definition. The event date reference is the [Rasuwa District Administration notice](https://daosolu.moha.gov.np/en/post/ha-ra-tha-ka-apa-l-11). Results come from the live CDSE catalogue; an empty result is returned as `no_data`. This endpoint searches metadata only and does not download satellite products. See the [official CDSE STAC documentation](https://documentation.dataspace.copernicus.eu/APIs/STAC.html) for the catalogue API.

### Verify and rank pre/post pairs

The pair endpoint searches CDSE separately for the configured pre-event and post-event windows. It checks product type, event ordering, exact elapsed time, orbit direction and relative orbit, polarization, and the actual intersection of each STAC footprint with the configured AOI. Ranking prioritizes verification status, common AOI coverage, orbit and polarization matches, then closeness to 12 days in 0.1-hour bands; scene IDs break remaining ties deterministically. This is an ordered compatibility ranking, not a probability or flood-detection accuracy score.

```sh
curl -G 'http://127.0.0.1:8765/api/v1/observations/pairs' \
  --data-urlencode 'bbox=84.04,28.20,84.15,28.31' \
  --data-urlencode 'event_date=2026-08-26' \
  --data-urlencode 'pre_event_start=2026-08-20' \
  --data-urlencode 'pre_event_end=2026-08-25' \
  --data-urlencode 'post_event_start=2026-08-27' \
  --data-urlencode 'post_event_end=2026-09-10' \
  --data-urlencode 'limit_per_period=10'
```

`common_aoi_coverage` reports `full`, `partial`, `none`, or `unknown` from polygon operations. The endpoint returns the actual common intersection geometry when computable and does not invent an area percentage. `compatible` means metadata and AOI checks support downstream review; it does not establish raster quality or analysis readiness. If a catalogue period is truncated, the response is `partial` and includes a warning.

Dataset restrictions and required credits are recorded in [docs/data-sources.md](docs/data-sources.md). No training datasets, OSM snapshots, or published flood/damage maps are used in this milestone.

Run the mocked service and API checks from the repository root:

```sh
python -m unittest discover -s apps/api/tests -v
```

### Web

```sh
cd apps/web
npm install
npm run dev
```

Open the URL printed by Vite, normally `http://127.0.0.1:5173`. The Vite development server proxies `/api` requests to the API on port 8765.

## Milestone record

- Completed: standalone scaffold, Phase 1 health route and frontend shell, live CDSE Sentinel-1 metadata search, and Milestone 2 pair-verification API.
- Last successful live catalogue response: 6 genuine Sentinel-1 GRD records at `2026-10-09T11:15:51.825865Z`. Using the actual acquisition timestamps and GeoJSON footprints from that response, both same-orbit candidate pairs fully covered the diagnostic AOI individually and in common. Candidate A (ascending, relative orbit 158) spans `2026-08-21T12:29:46.705159Z` to `2026-09-02T12:29:47.191710Z` (`1,036,800.486551` seconds; `12.000005631` days). Candidate B (descending, relative orbit 19) spans `2026-08-24T00:18:44.555468Z` to `2026-09-05T00:18:44.831563Z` (`1,036,800.276095` seconds; `12.000003196` days). Both have matching VV/VH polarization and are metadata-compatible candidates for the diagnostic AOI; raster quality and final study-area suitability remain unverified. These are last-known catalogue results, not a refreshed availability check.
- Current pair-search refresh attempt: CDSE returned HTTP 200 with `text/html` containing “Request Rejected” instead of STAC JSON. The API correctly returned HTTP 503 (`invalid_catalogue_response`, upstream status 200); the exact upstream blocking rule is unknown, and this is not a `no_data` result. Current record availability could not be reconfirmed.
- Next: stop at the end of Milestone 2. Downloads, raster checks, and SAR preprocessing remain later milestones.
