# JalDrishti

Standalone flood intelligence MVP for the Multimodal AI Hackathon 2026, Track B.

## Current milestone

Phase 1/2 catalogue, controlled-download, validation, and SNAP preprocessing workflows are implemented. Phase 3 now includes an audited, deliberately partial Kuro Siwo training pipeline and a research U-Net checkpoint; its one-event held-out evaluation is documented, but the checkpoint is not approved for Nepal inference. Phase 4 OSM preparation, independent-mask validation handoff, and exposure gates are implemented. The dashboard supports editable AOI/date search, pair ranking, explicit size preflight, sequential selected-product downloads, SNAP preprocessing, and asynchronous analysis status. The actual Nepal mask, flood area, and infrastructure exposure remain pending because the real SNAP pair is not yet validated for this model and there is no independent Nepal flood reference accepted for evaluation.

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

### Real case study and interactive workflow

With the API and Vite servers running, the dashboard requests the latest completed, checksum-matching SNAP pair from `GET /api/v1/case-study/latest` and displays actual Sigma0 VV quicklooks. The previews are served from `GET /api/v1/case-study/latest/rasters/pre_event.png` and `/post_event.png`. They are display images, not georeferenced analysis rasters. The JSON includes scene IDs, acquisition times, CRS, bounds, raster dimensions, attribution, warnings, and any matching prepared OSM inventory. Flood area, mask, probability, and boundaries are `null` until an accepted model produces validated results.

Check the summary and a preview directly:

```sh
curl -s http://127.0.0.1:8765/api/v1/case-study/latest
curl -I http://127.0.0.1:8765/api/v1/case-study/latest/rasters/pre_event.png
```

The case-study quicklook card reads existing local artifacts. The separate interactive workflow below it accepts an editable WGS84 bounding box and event date. It searches CDSE pair candidates, allows selection only of metadata-compatible pairs, and calls `POST /api/v1/downloads/preview` to revalidate product availability and show exact raw sizes and free disk space. That preview creates no download jobs. A second explicit button downloads the selected native `Product` assets one at a time and polls their validation status. After both pass, another explicit action starts the SNAP job. Auxiliary orbit/DEM downloads are off by default and require the checkbox. After SNAP completes, the analysis action calls the asynchronous analysis API and displays its real status; with current model/input approval settings it correctly reports blocked/pending rather than fabricating a result. The UI accepts a bbox, not a place-name geocoder.

Successful inference artifacts (when all gates pass) are served from API routes, not public cloud URLs. The dashboard exposes a mask or boundary link only when the API returns a validated result. The app has no authentication layer and should not be exposed to an untrusted network. Raw products and derived rasters remain outside Git.

On 2026-10-14, the full available suites passed: API **88/88**, ML **43/43**, geospatial **34/34**, and the web production build passed. The API tests emitted Rasterio `NotGeoreferencedWarning` messages for deliberate synthetic invalid-raster fixtures; there were no test failures. The current local case-study endpoint reports the exploratory model and keeps flood area, mask, and boundaries pending. The current API smoke check is listed in the final milestone record below.

The API suite including the new case-study routes passed **75/75** tests; the ML suite passed **27/27**; the geospatial suite passed **34/34**; and the web production build passed. A local TestClient smoke check against the real manifests and rasters returned HTTP 200 for health, the case-study JSON, and both PNG previews. The JSON truthfully reports `flooded_area_km2: null`, no accepted checkpoint, and the provider-dated OSM inventory only.

## Milestone record

- Completed: standalone scaffold, Phase 1 health route and frontend shell, live CDSE Sentinel-1 metadata search, Milestone 2 pair-verification API, and Milestone 3 controlled native-product download implementation with mocked validation tests.
- Latest live pair search completed at `2026-10-09T11:41:52.283349Z` against the official CDSE STAC catalogue. It returned 2 pre-event and 4 post-event Sentinel-1 GRD records (6 total), with 8 scored combinations and no additional-page indicators. The top two were metadata-compatible; the other six were marked questionable because of orbit mismatch and/or partial AOI overlap.
- Rank 1: `S1D_IW_GRDH_1SDV_20260821T122946_20260821T123011_004224_007C0F_BB02_COG` (2026-08-21 12:29:46.705159 UTC) to `S1D_IW_GRDH_1SDV_20260902T122947_20260902T123012_004399_00822F_7364_COG` (2026-09-02 12:29:47.191710 UTC). Both are IW_GRDH_1S, ascending relative orbit 158, VV/VH; elapsed time is `1,036,800.486551` seconds (`12.000005631` days). Both footprints and their common intersection cover the configured diagnostic AOI.
- Rank 2: `S1D_IW_GRDH_1SDV_20260824T001844_20260824T001909_004260_007D5D_45FC_COG` (2026-08-24 00:18:44.555468 UTC) to `S1D_IW_GRDH_1SDV_20260905T001844_20260905T001909_004435_00837B_60FD_COG` (2026-09-05 00:18:44.831563 UTC). Both are IW_GRDH_1S, descending relative orbit 19, VV/VH; elapsed time is `1,036,800.276095` seconds (`12.000003196` days). Both footprints and their common intersection cover the configured diagnostic AOI.
- The earlier `Request Rejected` HTML response was an access-path issue: after the network changed, the public collection endpoint returned JSON and the live pair search completed. The exact cause of the earlier rejection is unknown. These results establish metadata and footprint compatibility only; raster quality and final study-area suitability remain unverified. The AOI is diagnostic, not a validated flood boundary. At the time of this catalogue search no product had yet been downloaded; later entries record the actual archive downloads. At that point, SAR preprocessing had not yet run.
- On `2026-10-09`, the Rank 1 pair was re-queried through official CDSE STAC item URLs and OData. Both products were returned as online and exposed native SAFE ZIP assets. Pre: OData ID `9ac4bede-15a3-4ddc-bf73-7e3660be233c`, 1,282,468,984 bytes, MD5 `11f0a65d9415be61a9094ad0c5fd957e`. Post: OData ID `6634dff9-500f-4c3b-af67-b499b36b8a71`, 1,285,884,915 bytes, MD5 `dcf32b0dc7bbf0866edd20877ffae4e2`. STAC acquisition times, ascending relative orbit 158, IW_GRDH_1S product type, and VV/VH polarization still match. STAC also lists separate VV and VH cloud-optimized GeoTIFF assets, but this milestone explicitly selects only the native `Product` archive and does not substitute assets.
- On `2026-10-09`, the user completed a live download of the selected pre-event `Product` SAFE archive (1,282,468,984 bytes; job `0e0ae6c7-064b-4e8e-af81-cb0c05397ac0`). Its size and provider MD5 (`11f0a65d9415be61a9094ad0c5fd957e`) matched; computed SHA-256 was `2872c3fc91c198b857d4aa07105583cc904be2373ec7bb392af560a9fd7b48a8`. ZIP CRCs and SAFE manifest checks passed. Its two measurement rasters use 210 EPSG:4326 ground control points rather than an affine CRS. Metadata-only inspection succeeded with `pixel_values_read: false` and `analysis_ready: false`; GCP bounds summarize control points and are not warped pixel-edge bounds.
- The selected post-event `Product` SAFE archive also completed live download on `2026-10-09` (1,285,884,915 bytes; job `3a7508d9-dd51-4e06-a8b5-f8842f807cd0`). Its size and provider MD5 (`dcf32b0dc7bbf0866edd20877ffae4e2`) matched; computed SHA-256 was `c4a5915a7eb19284da7cf8b30d3322dd36a8e0660cc2827584e97de2b03800c5`. ZIP and SAFE checks passed. Both measurement rasters were metadata-inspected as EPSG:4326 with 210 GCPs each; `pixel_values_read: false` and `analysis_ready: false`. Provider BLAKE3 checksums are recorded but were not verified by this runtime. Earlier interrupted and token-rejected download attempts were not accepted as products; the final jobs above are the verified downloads.
- Live metadata revalidation on `2026-10-10` queried both exact CDSE STAC items and OData product records. Both still exist as `IW_GRDH_1S`, ascending relative orbit 158, VV/VH, with STAC footprints covering the requested AOI. The pre-event product remains online at 1,282,468,984 bytes (OData ID `9ac4bede-15a3-4ddc-bf73-7e3660be233c`, MD5 `11f0a65d9415be61a9094ad0c5fd957e`); the post-event product remains online at 1,285,884,915 bytes (OData ID `6634dff9-500f-4c3b-af67-b499b36b8a71`, MD5 `dcf32b0dc7bbf0866edd20877ffae4e2`). This was metadata only; no additional product or auxiliary data was downloaded.

## Milestone 3: controlled product downloads

The API accepts one explicitly selected product per request, with its pre/post role, both scene IDs from a compatible pair, event date, AOI, and the native `Product` asset. It re-fetches both STAC items, checks pair compatibility, confirms the selected archive asset and exact OData product are still online, checks size and available disk space, then starts the download in a FastAPI background task. It will not substitute an asset. The official [CDSE OData API](https://documentation.dataspace.copernicus.eu/APIs/OData.html) documents product metadata and `$value` downloads; the [CDSE token API](https://documentation.dataspace.copernicus.eu/APIs/Token.html) documents account authentication.

### Configure access and storage

From the repository root, create a local environment file and edit it with your CDSE account details. Do not commit or share `.env`:

```sh
cp .env.example .env
```

`CDSE_USERNAME` and `CDSE_PASSWORD` are needed only for downloads. CDSE credentials are sent to its documented OIDC token endpoint by the backend; they are not accepted from API requests or exposed to the frontend. For current account requirements, use the [official CDSE documentation](https://documentation.dataspace.copernicus.eu/).

`JALDRISHTI_DATA_DIR` defaults to `data/raw/sentinel1` under this repository. Set it in `.env` to an absolute external storage directory if preferred, or to another relative path from the repository root. Raw data, manifests, and partial files are ignored by Git. `JALDRISHTI_MAX_DOWNLOAD_BYTES` defaults to 5 GiB; increase it deliberately only after checking the selected product size and available disk space.

### Start the API with local configuration

From the repository root:

```sh
source .venv/bin/activate
python -m pip install -r apps/api/requirements.txt
set -a
source .env
set +a
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8765
```

### Select one product and check its status

The following request shows the verified Rank 1 pair and selects only its pre-event SAFE archive. It would download about 1.28 GB if run with valid CDSE credentials. Confirm the product size and disk space and intentionally choose the role before sending it. To select the post-event archive, set `role` to `post_event`; use only the scene IDs from a pair returned by the pair-verification endpoint.

```sh
curl -X POST 'http://127.0.0.1:8765/api/v1/downloads' \
  -H 'Content-Type: application/json' \
  --data '{
    "pre_scene_id": "S1D_IW_GRDH_1SDV_20260821T122946_20260821T123011_004224_007C0F_BB02_COG",
    "post_scene_id": "S1D_IW_GRDH_1SDV_20260902T122947_20260902T123012_004399_00822F_7364_COG",
    "event_date": "2026-08-26",
    "bbox": [84.04, 28.20, 84.15, 28.31],
    "role": "pre_event",
    "asset_key": "Product"
  }'
```

Use the returned `download_id` to poll status:

```sh
curl 'http://127.0.0.1:8765/api/v1/downloads/<download_id>'
```

If a product reached `validation_failed` after its integrity checks passed, use the metadata-only re-inspection route after updating the API code. It opens the already-downloaded local archive and does not download it again:

```sh
curl -X POST 'http://127.0.0.1:8765/api/v1/downloads/<download_id>/inspect'
```

The local manifest is written to `data/raw/sentinel1/manifest.json` by default. Product archives are stored below `products/`; temporary `.part` files are removed on failed attempts. The API service is a single-process local prototype: jobs run in FastAPI background tasks and status is persisted in the JSON manifest, without a task queue. If the API process stops during a transfer, the next service start marks the old job `failed`, removes its incomplete temporary file, and allows an explicit new request to retry from byte zero.

### Validation stages and limits

The transfer is streamed to a temporary file, size-checked against OData metadata, and compared with supported provider checksums. Local SHA-256 and MD5 are computed; a provider BLAKE3 checksum is recorded but reported as unverified when no BLAKE3 implementation is installed. ZIP integrity, SAFE manifest XML, measurement-file presence, and ZIP CRCs are checked before atomic placement in `products/`. Rasterio then inspects measurement GeoTIFF metadata (CRS, georeferencing method, bounds or GCP summary, dimensions, bands, types, and NoData) without reading pixel values. GCP bounds summarize listed control points; the service does not warp the raster or claim pixel-edge bounds. Status `geospatial_inspected` does not mean analysis-ready. Orbit correction, calibration, noise removal, terrain correction, alignment, and flood analysis are not performed.

The downloader uses bounded retries for transient failures, honors `Retry-After` within the configured wait limit, and restarts interrupted transfers from byte zero. If CDSE returns HTTP 401 for the product transfer, it invalidates the cached token, obtains one fresh token, and retries once; repeated 401 responses stop the job. HTTP 403 is reported as a product access denial without retry. It follows HTTPS redirects only within CDSE/Copernicus Data Space or CloudFerro domains; it does not send bearer tokens to unrelated domains. Product archives and the manifest retain the applicable credit: “Contains modified Copernicus Sentinel data 2026.”

Install current Python requirements and run mocked download plus regression tests from the repository root:

```sh
python -m unittest discover -s apps/api/tests -v
```

These tests do not contact CDSE or download large products. Both selected live SAFE archives downloaded successfully and passed the recorded integrity and metadata checks above. SAR preprocessing and its current test results are documented below. OSM, Copernicus EMS/EMSR927, UNOSAT, and training datasets are not part of the download milestone; their restrictions remain in [docs/data-sources.md](docs/data-sources.md).

## SAR preprocessing (backend only)

The backend adds `POST /api/v1/preprocessing` to create a job from two already validated download IDs, and `GET /api/v1/preprocessing/{job_id}` to read its status. It reopens the selected SAFE archives, verifies the pair against the live CDSE STAC items, checks event bracketing, orbit, polarization and footprint coverage, then constructs an ESA SNAP graph for orbit-file application, metadata-gated thermal-noise removal, GRD border-noise removal, linear Sigma0 calibration, and terrain correction with the explicitly selected Copernicus 30 m DEM. Rasterio then crops/reprojects both calibrated VV/VH outputs to one projected AOI grid and uses a shared valid-data mask. The current output remains blocked from model use until the model-specific input and domain-shift review passes.

CDSE documents COG_SAFE creation as converting the original measurement GeoTIFFs to COG and updating the SAFE manifest; it does not describe that conversion as radiometric calibration. Based on that documented conversion process, the graph applies Sigma0 calibration once. It reads each SAFE annotation's per-polarization `thermalNoiseCorrectionPerformed` flag and only inserts SNAP's thermal-noise operator when both selected products report the correction has not already been performed. [CDSE COG_SAFE documentation](https://documentation.dataspace.copernicus.eu/Data/Others/Sentinel1_COG.html), [SNAP Thermal Noise Removal help](https://step.esa.int/main/wp-content/help/versions/14.0.0/snap-toolboxes/eu.esa.microwavetbx.sar.op.calibration.ui/operators/ThermalNoiseRemoval.html).

Real processing needs ESA SNAP's Graph Processing Tool (`gpt`). SNAP is an external application and is not installed by `pip`. Preserve the existing `.env` and set these values for future local processing:

```dotenv
JALDRISHTI_SNAP_GPT=/full/path/to/esa-snap/bin/gpt
JALDRISHTI_PROCESSED_DIR=data/processed/sentinel1
JALDRISHTI_SNAP_TIMEOUT_SECONDS=21600
```

`JALDRISHTI_SNAP_GPT` is optional if `gpt` is in a SNAP installation directory on `PATH`; a system utility also named `gpt` is not accepted. The default processing directory is `data/processed/sentinel1`; it can be changed to another relative repository path or an external absolute path. Generated rasters and per-job provenance manifests are ignored by Git. Raw SAFE inputs remain in `JALDRISHTI_DATA_DIR`.

Before requesting a job, check SNAP and DEM requirements. Setting `allow_snap_auxiliary_downloads` to `true` explicitly permits SNAP to retrieve missing orbit files and DEM tiles; this can transfer external data. The requested DEM is fixed to Copernicus 30 m with no silent fallback. CDSE's [Copernicus DEM notice](https://dataspace.copernicus.eu/news/2026-8-25-copernicus-dem-30m-view-service-update) says access to its 30 m view service became restricted to accounts with the contributing-missions access setting on 2026-08-25. Update the account profile if CDSE denies the DEM request. See [Copernicus DEM terms and attribution](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM).

From the repository root, start the API with the existing environment file loaded:

```sh
cd ~/Desktop/jaldrishti
source .venv/bin/activate
set -a
source .env
set +a
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8765
```

The real run used a separate API instance on port 8766 so the user's existing port 8765 service was not disturbed. The selected COG2GRD staging copies were explicitly configured as its data directory. From the repository root, the startup command was:

```sh
cd ~/Desktop/jaldrishti
source .venv/bin/activate
export JALDRISHTI_DATA_DIR='data/processed/sentinel1/7ebd0585-c678-47ff-baa0-d2d1405ce469/cog2grd/pair_inputs'
export JALDRISHTI_PROCESSED_DIR='data/processed/sentinel1'
export JALDRISHTI_SNAP_GPT='/Applications/esa-snap/bin/gpt'
export JALDRISHTI_SNAP_TIMEOUT_SECONDS='21600'
python -m uvicorn apps.api.app.main:app --host 127.0.0.1 --port 8766
```

Startup output confirmed `Application startup complete` and `Uvicorn running on http://127.0.0.1:8766`. The following request submitted the explicitly selected, already validated pair. SNAP auxiliary orbit/DEM downloads were explicitly approved for this job:

```sh
curl -X POST 'http://127.0.0.1:8766/api/v1/preprocessing' \
  -H 'Content-Type: application/json' \
  --data '{
    "pre_download_id": "39f32047-90c0-4452-b795-4a94e63516be",
    "post_download_id": "946ebb85-98c0-4e58-aff6-21ef6c79a3b3",
    "bbox": [84.04, 28.20, 84.15, 28.31],
    "target_crs": "EPSG:32645",
    "pixel_spacing_m": 10,
    "allow_snap_auxiliary_downloads": true
  }'
```

Use the returned job ID to poll status:

```sh
curl 'http://127.0.0.1:8766/api/v1/preprocessing/<job_id>'
```

By default, outputs are written below `data/processed/sentinel1/<job_id>/`; small manifests are written below `data/manifests/sentinel1_preprocessing/`. The manifest records exact source IDs and acquisition/orbit/polarization metadata, processing parameters, software versions when SNAP reports one, common-grid details, per-band statistics, validity coverage and warnings. It does not store STAC asset URLs or credentials. Outputs preserve the required Sentinel credit and the Copernicus WorldDEM-30 credits listed in [docs/data-sources.md](docs/data-sources.md).

### Actual run and output validation (2026-10-10)

The corrected job `98d8cdb3-d6f5-4d2d-b961-a1295ca60a3d` finished with API status `completed` and stage `geospatially_validated`. Pre and post SNAP writers ran sequentially and both logs ended with `done`; no calibration LUT warning appeared. The SNAP graph applied precise orbit files, annotation-required thermal-noise removal, GRD border-noise removal, linear Sigma0 calibration (not dB), and terrain correction using the configured Copernicus 30 m Global DEM. It then explicitly selected `Sigma0_VV,Sigma0_VH`; Rasterio warped both scenes to the same 10 m grid.

Independent Rasterio checks reopened `data/processed/sentinel1/98d8cdb3-d6f5-4d2d-b961-a1295ca60a3d/pre_event.tif` and `post_event.tif`. Both are two-band float32 EPSG:32645 rasters, 1110 × 1247 pixels, with identical affine transforms, `Sigma0_VV,Sigma0_VH` band descriptions, NaN NoData, matching SHA-256 values from the manifest, and identical valid-data masks. The common mask contains 1,317,470 pixels and covers 100% of the requested AOI; all valid VV/VH values are finite and positive. The output grid bounds in WGS84 are approximately `[84.03693, 28.19760, 84.15300, 28.31242]`, enclosing the requested AOI. Pre SHA-256: `7809a02620c34e01f86ff24e3d0f2e820a81be77383a527f5e8d59611b80a148`; post SHA-256: `063d527992a66aadc39ab1846a0c1e4de79db9ac2cc0b705002d5dd1a41b67f2`.

The original downloaded CDSE ZIP archives remain unchanged: their SHA-256 values still match the download manifest (`2872c3fc91c198b857d4aa07105583cc904be2373ec7bb392af560a9fd7b48a8` pre and `c4a5915a7eb19284da7cf8b30d3322dd36a8e0660cc2827584e97de2b03800c5` post). The COG2GRD working copies were separate staging inputs. The old partial output from the failed first run was preserved. The outputs demonstrate successful execution of the configured calibration and terrain-correction graph, but there was no independent radiometric reference comparison; therefore this is not a full scientific calibration QA. An exploratory Kuro Siwo checkpoint now exists, but it is not approved for Nepal inference; the SNAP manifest still sets `model_input_status` to `blocked_unknown_model_contract` and the rasters remain `analysis_ready: false`. No Nepal inference, flood mask, or flood estimate was produced.

The regression command on 2026-10-10 was `./.venv/bin/python -m unittest discover -s apps/api/tests -v`: **70 tests passed**. This includes 11 preprocessing tests and the existing health, catalogue, pair-ranking, and download tests. The run emitted the existing Starlette/httpx deprecation warning and Rasterio `NotGeoreferencedWarning` from synthetic fixtures; there were no failures. Unit tests did not contact CDSE or download auxiliary data; the separate real preprocessing job above did use the selected satellite products and SNAP auxiliary data.

## Phase 3: segmentation foundation (exploratory model, not deployment-approved)

The Phase 3 implementation adds a pinned Kuro Siwo source/split manifest,
strict TAR sample reader with event-ID filtering, a small newly initialized
four-channel U-Net, tiling, input-raster checks, metrics, an event-shard audit
command, and guarded training/evaluation commands. Details are in
[docs/model-evaluation.md](docs/model-evaluation.md) and
[docs/limitations.md](docs/limitations.md).

The Kuro Siwo dataset card declares CC BY 4.0; cite Bountos et al. (2024) and
preserve its attribution. Sen1Floods11 is excluded pending license
verification. The 3.105 GB scout is an author-test event despite its
`train_GRD` folder. The verified 10.915 GB `test_GRD/shard-00000.tar` contains
seven author-train events and two author-test events. The verified
`train_GRD/shard-00001.tar` is 10,914,938,880 bytes with SHA-256
`f6a3d55c0c36497a310f34d91b6a476af926e261c5047ce6045016f45c18930d`; its
6,000 decoded samples include author-train events 1111004, 1111005, 1111006,
and 1111009, validation event 1111008, and held-out test event 1111007. The
verified `train_GRD/shard-00002.tar` has SHA-256
`448fc21a67dc1b4d5fd4cf8c3bb4f926a129ca5cf13899c9f7d6667bb72b74a3` and
contains 6,000 samples from author-train events 1111009 and 1111010. Detailed,
Git-ignored sample audits are in
`data/raw/kuro-siwo/subsets/track-b-initial/`. The refreshed partial audit
finds 16 known event IDs, no unknown IDs, and train, validation, and test
events; nine events are stored under folders that disagree with their author
split. Samples are assigned by `actid`, not folder. Validation is currently
only one event with 81 samples, so this subset supports a guarded prototype
run but not robust model acceptance. A separate 167,325,696-byte partial
test-shard transfer remains incomplete and is not used.

The test-folder shard also has an unresolved data-quality issue: 651 samples
contain `mask.npy` value `3` on pixels whose `valid_mask.npy` says valid. The
published label contract lists only 0/1/2, so those labels are not remapped.
That archive is excluded from the selected training inventory. The
Git-ignored, symlink-only training subset at
`data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/` uses only verified
`train_GRD/shard-00001.tar` and `shard-00002.tar`; its fresh event audit is
`event-audit.json`. It contains 9,590 author-train samples, 81 validation
samples from one event, and 2,329 held-out test samples from one event. This
is a small prototype subset, not a representative full-corpus evaluation.
The loader now clips linear Sigma0 at 0.15 before applying the pinned
per-polarization normalization, matching the published BlackBench setup
([Kuro Siwo supplementary material](https://proceedings.neurips.cc/paper_files/paper/2024/file/43612b0662cb6a4986edf859fd6ebafe-Supplemental-Datasets_and_Benchmarks_Track.pdf)).
On the current Nepal pair, 48.4% of common-valid pre-event VV and 51.2% of
post-event VV pixels exceed that ceiling; this is a material domain gap and
keeps Nepal inference blocked pending review.

The pinned Kuro Siwo train/test shard folders total about 157.9 GiB. The full
corpus is not required for an initial subset experiment. Four complete
archives have been inspected, including the separate 3.105 GB held-out scout.
The current partial train/validation/test inventory is audited but does not
cover the full benchmark. The subset training and evaluation commands may run
with `--allow-partial-subset`; their results must be labeled partial and are
not deployment approval. No automatic dataset transfer runs from the
application. Any further archive requires a specific documented need and
explicit approval if it exceeds 10 GB.

Install the model dependency and run the synthetic-only ML unit tests from the
repository root:

```sh
source .venv/bin/activate
python -m pip install -r ml/requirements.txt
python -m unittest discover -s ml/tests -v
```

Earlier suite counts are historical; use the latest run reported in the
current-milestone section above for the current regression result. The training
and held-out evaluation were run separately; see the results below.

The current local subset is linked and audited here:

```sh
python -m ml.src.audit_dataset \
  --shard-root data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14 \
  --output data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/event-audit.json \
  --allow-partial-subset
```

This audit finds author-train, validation, and held-out test events, so guarded
partial-subset training and evaluation are available. Unknown event IDs still
fail, and samples are filtered by `actid`, not archive folder. Validation is
currently limited to one event and 81 samples; all results remain research
prototype results pending independent review and preprocessing-compatibility
checks.

```sh
python -m ml.src.train \
  --dataset-root data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14 \
  --audit data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/event-audit.json \
  --output ml/checkpoints/jaldrishti-unet-kuro-siwo-20261014.pt \
  --allow-partial-subset

python -m ml.src.evaluate \
  --dataset-root data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14 \
  --audit data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/event-audit.json \
  --checkpoint ml/checkpoints/jaldrishti-unet-kuro-siwo-20261014.pt \
  --output reports/generated/jaldrishti-evaluation.json \
  --allow-partial-subset
```

The evaluator tunes the flood threshold on validation events, reports
per-event and aggregate scores plus a descriptive event-cluster bootstrap
interval, then makes one test pass per checkpoint and records a test-access
marker. The 2026-10-14 CPU run requested 10 epochs and was stopped after epoch
5 as validation loss worsened after epoch 1; the best saved checkpoint is
epoch 1. One test pass completed with flood F1 0.5791 at the validation-selected
0.30 threshold on held-out event 1111007. This is a small partial-subset result,
not deployment approval; see [docs/model-evaluation.md](docs/model-evaluation.md)
and [docs/limitations.md](docs/limitations.md). Training now records each epoch
atomically and supports early stopping; the existing interrupted training run
was not resumed or replaced.

### Inference API and validation handoff

An additive, asynchronous inference API is available for an existing completed
preprocessing job. It accepts the preprocessing job ID, not caller-supplied
paths or URLs:

```sh
curl -X POST 'http://127.0.0.1:8765/api/v1/analyses' \
  -H 'Content-Type: application/json' \
  --data '{"preprocessing_job_id":"<completed-preprocessing-job-uuid>"}'

curl 'http://127.0.0.1:8765/api/v1/analyses/<analysis-job-uuid>'
```

The POST returns HTTP 202 and a job ID. Poll the GET route for `queued`,
`running`, `blocked`, `pending_independent_validation`, `completed`, or
`failed`. A result is deliberately blocked unless the preprocessing manifest
has `model_input_status: validated_for_model`, the exact checkpoint and
evaluation report are approved, and an exact-pair reviewer approval manifest
is present. Configure optional server-side paths with
`JALDRISHTI_MODEL_CHECKPOINT`, `JALDRISHTI_MODEL_EVALUATION`, and
`JALDRISHTI_INFERENCE_APPROVAL`; paths must remain inside the repository.
The default approval file is
`ml/approvals/inference-approval.json`; no approval file is currently present.
It must use `schema_version: jaldrishti.inference-approval.v1`, explicitly set
`deployment_approved` and `inference_eligible` to true, identify reviewer,
review time, event ID and `jaldrishti_s1_two_date_v1`, and bind the approval to
the exact checkpoint SHA-256, evaluation-report SHA-256, pre/post raster
SHA-256 values, event date and AOI bbox. The evaluation report must separately
approve the same checkpoint. No credentials belong in these files; do not
create an approval for the current exploratory checkpoint.

When inference eventually runs, it writes a binary GeoTIFF flood mask, a
three-class GeoTIFF, an uncalibrated softmax-score GeoTIFF, GeoJSON boundaries,
and provenance under `data/processed/inference/<analysis-id>/`. Softmax is not
called probability or confidence. The API keeps flood area, mask and boundary
URLs null and denies artifact downloads until the independent-reference
validation report is reviewed and the Phase 4 mask gate accepts it. The current
Nepal analysis API request returns a blocked job because the existing SNAP
manifest still marks the pair `blocked_unknown_model_contract`; there is no
real Nepal inference output.

Preserve any independent reference map under Git-ignored `data/validation/`.
It must not be used for training or prediction. After an approved inference
candidate exists, first create a separate nearest-neighbour aligned copy of a
categorical reference raster. Keep its original file unchanged and provide the
reference's actual class mapping:

```sh
python -m ml.src.independent_validation align-reference \
  --reference-raster data/validation/<event>/reference_original.tif \
  --mask-manifest data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --output data/validation/<event>/reference_aligned.tif \
  --source-flood-value <reference-flood-class> \
  --source-non-flood-values <reference-non-flood-class> [<another-non-flood-class> ...] \
  --source-nodata-value <reference-nodata-class>
```

The alignment command records both hashes, class mapping and resampling in a
sidecar. It does not create a reference map or establish its license. Then
calculate validation metrics without auto-approving the mask:

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
```

The validator requires identical CRS, transform and dimensions; it never
silently resamples the independent reference. Inspect the report, then a human
reviewer can apply a named policy and explicit thresholds:

```sh
python -m ml.src.independent_validation accept-mask \
  --mask-manifest data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --report reports/generated/<event>-independent-validation.json \
  --reviewed-by '<reviewer>' --acceptance-policy-id '<policy-id>' \
  --minimum-flood-iou <0-to-1> --minimum-flood-recall <0-to-1> \
  --minimum-valid-coverage <0-to-1>

python -m geospatial.phase4_cli check-mask \
  data/processed/inference/<analysis-id>/phase4_flood_mask_manifest.json \
  --event-id <event-id> --event-date <YYYY-MM-DD> \
  --bbox <west> <south> <east> <north>
```

Only after `check-mask` passes should the existing Phase 4 `exposure` command
be run with the matching prepared OSM snapshot. It calculates potential spatial
exposure, not confirmed damage, road closure, or lost access. API raster and
GeoJSON URLs are served by this local API only after acceptance; this service
has no authentication layer and must not be exposed to an untrusted network.
## Phase 4: infrastructure groundwork

The additive OSM snapshot, geometry-preparation, mask-readiness, and exposure groundwork is documented in [docs/phase4-infrastructure.md](docs/phase4-infrastructure.md). On 2026-10-11, the official Geofabrik Nepal PBF was checksum-verified and imported for the Rasuwa AOI. `audit-osm` passed for snapshot `214b1809-502b-469f-bcdb-647e0e723d67` with 8,147 features; `prepare` produced `data/processed/infrastructure/214b1809-502b-469f-bcdb-647e0e723d67/infrastructure_prepared.geojson` (EPSG:4326 output; EPSG:32645 measurements). The provider date is 2026-08-01, so this is date-level pre-event evidence, not exact per-feature history. The raw PBF and derived data are Git-ignored. The live ohsome history attempt did not produce a snapshot. No real exposure results are available because the model is not approved for Nepal and no independently validated flood mask exists.

## Phase 5: road connectivity groundwork

The additive NetworkX road-access scenario and its assumptions are documented in [docs/phase5-connectivity.md](docs/phase5-connectivity.md). The prepared, provider-dated OSM snapshot is available as an input, with date-level provenance caveats. Real connectivity results remain gated because there is no approved real flood mask; no event-specific access figures have been generated.
