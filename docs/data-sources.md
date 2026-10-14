# Dataset and License Requirements

This file records the mandatory JalDrishti dataset conditions supplied for the project. Check the source's current terms and preserve its required citation before introducing any dataset into a later milestone.

| Dataset or source | Permitted use and conditions | Required attribution or citation |
| --- | --- | --- |
| Sentinel-1 and Sentinel-2 | Use Copernicus Data Space Ecosystem data, free and open under the applicable Copernicus data policy. | When applicable: “Contains modified Copernicus Sentinel data 2026.” |
| Copernicus DEM GLO-30 / WorldDEM-30 | Use the Copernicus DEM under its applicable free license. Verify CDSE contributing-missions profile access before requesting 30 m data. | Public-source notice: “© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved”. For adapted/modified WorldDEM-30: “produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved”. |
| OpenStreetMap | Use an appropriate snapshot dated before the 2026-08-26 event; do not treat later edits as pre-event evidence. Follow ODbL attribution and share-alike requirements where applicable. | “© OpenStreetMap contributors.” |
| Kuro Siwo | Follow the applicable repository license before use. | Cite Bountos et al. (2024). |
| Sen1Floods11 | Verify the actual source and license before use. Do not assume an unlicensed original repository permits unrestricted reuse. | Cite Bonafilia et al. (2020). |
| Copernicus EMS, including EMSR927 | Independent validation only. Never use as prediction-pipeline input, training labels, or source flood masks. | “European Union, Copernicus Emergency Management Service data” |
| UNOSAT and other published flood or damage maps | Independent result checking only; keep references separate from model inputs and prediction logic. | Preserve each source's applicable credit when used for independent checking. |

## Milestone 2 use

The catalogue and pair-search milestone queried only Sentinel-1 GRD metadata. It did not use Sentinel-2, OSM, Kuro Siwo, Sen1Floods11, Copernicus EMS/EMSR927, or UNOSAT data. No published flood or damage map was used in operational search or pair ranking.

The required Sentinel attribution is retained for applicable downstream Sentinel-derived products; catalogue metadata alone is not represented as modified Sentinel imagery. Before any later dataset is added, verify its current source terms and include the required attribution/citation in the relevant outputs and documentation.

## Milestone 3 use

The controlled download service uses CDSE STAC item metadata to confirm the explicitly selected Sentinel-1 GRD pair and the native `Product` ZIP asset, then uses the documented CDSE OData product metadata and `$value` download endpoint. CDSE OIDC credentials are read from environment variables and are required for the download. The service records the Sentinel attribution “Contains modified Copernicus Sentinel data 2026.” in the local download manifest. Raw archives and the manifest are stored under `JALDRISHTI_DATA_DIR` (default `data/raw/sentinel1`) and ignored by Git.

The live Rank 1 metadata re-query on 2026-10-09 found both native Product archives online. The user subsequently downloaded both archives through the controlled service: the pre-event archive was 1,282,468,984 bytes and the post-event archive was 1,285,884,915 bytes. Their provider MD5 checksums, local SHA-256 checksums, SAFE structure, and ZIP CRCs passed. Their measurement GeoTIFF metadata were inspected but their pixel values were not read during download validation. No Sentinel-2, OSM, Kuro Siwo, Sen1Floods11, Copernicus EMS/EMSR927, UNOSAT, or other validation/training data were ingested.

Integrity checks cover transfer length, supported provider checksums, local SHA-256/MD5, SAFE ZIP structure and CRC, and measurement GeoTIFF metadata where Rasterio can inspect it. If CDSE provides a checksum algorithm unsupported by the runtime, its value is retained in the manifest but is not called verified. Raster metadata inspection does not imply that the product has been preprocessed or is analysis-ready.

## SAR preprocessing implementation status

The backend-only preprocessing path revalidates the explicitly selected pre/post downloads against their exact CDSE STAC items before starting. It reads SAFE annotation flags first and rejects a pair whose VV/VH or pre/post thermal-noise correction histories differ; the SNAP thermal-noise operator is omitted when noise correction was already performed. The graph applies orbit information, GRD-border-noise removal, linear Sigma0 calibration, and terrain correction with the requested Copernicus 30 m global DEM. Rasterio reprojects both outputs onto the same requested projected CRS/grid and calculates valid-area coverage and band statistics. It rejects missing/unrecognized calibrated band names rather than guessing. It does not run speckle filtering, create AI tensors, or label outputs analysis-ready.

On 2026-10-10, real preprocessing completed for the explicitly selected Rank 1 pair with SNAP 14.0.0, using separate COG2GRD staging copies and an API instance on port 8766. The request explicitly enabled SNAP auxiliary downloads; the graph completed with the configured Copernicus 30 m Global DEM and no fallback. Both output rasters were reopened and verified as two-band float32 linear Sigma0 products on the same EPSG:32645, 10 m grid, with 100% valid coverage of the requested AOI. SNAP is still an external dependency and auxiliary data is fetched only when a request explicitly sets `allow_snap_auxiliary_downloads=true`. The CDSE notice dated 2026-08-25 restricts access to the Copernicus DEM 30 m view service to accounts with the contributing-missions access setting. See the [official CDSE access notice](https://dataspace.copernicus.eu/news/2026-8-25-copernicus-dem-30m-view-service-update) and [Copernicus DEM license and attribution rules](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM).

The configured graph completed its Sigma0 calibration and terrain-correction operators without a calibration LUT warning, and the output rasters contain finite positive linear-power values. This verifies graph execution and raster structure, not absolute radiometric accuracy against a reference target. The product must not be treated as model-ready: there is no checked model input contract or checkpoint, and the manifest retains `blocked_unknown_model_contract` and `analysis_ready: false`.

The output GeoTIFF tags and provenance manifest record “Contains modified Copernicus Sentinel data 2026.” and both applicable Copernicus WorldDEM-30 source/adaptation notices above. The synthetic raster tests verify reprojection/grid alignment logic only; they are not satellite-derived outputs.

## Phase 3 candidate dataset

Kuro Siwo is the candidate for later training, pinned to Hugging Face revision
`e8e61b7b1254b04bfa2b5e26d43db90cb86b5004`. Its dataset card declares CC BY
4.0; cite Bountos et al. (2024) and retain the license attribution/terms. The
author training/event split configuration is separately pinned to
`Orion-AI-Lab/KuroSiwo` commit
`4347ed173c4e48f5a9d578bb5fe8453706b08e5e`. Repository code is MIT licensed,
but that does not replace the dataset's CC BY 4.0 terms. The exact source and
split details are recorded in `ml/configs/kuro_siwo_splits.json` and
`docs/model-evaluation.md`.

On 2026-10-11, the 3,105,280,000-byte Kuro Siwo scout and a 10,914,856,960-byte
pinned cache archive were downloaded and SHA-256 verified. The scout contains
author-test event `1111013`, despite its `train_GRD` folder. The second archive
contains nine events (118, 130, 147, 174, 205, 267, 273, 275, 277), including
both author-train and author-test events; it contains no validation event and
is not a complete training subset. A separate 167,325,696-byte partial for
`test_GRD/shard-00001.tar` has no active downloader and is incomplete. No
model training or evaluation has used these archives. The checkpoint links
still do not have a verified weight license/checksum for this project, so the
custom model remains untrained. Sample assignment must use `actid`, never the
archive folder. Sen1Floods11 remains excluded until its dataset license is
verified. Copernicus EMS/EMSR927 and other published flood/damage products were
not ingested and remain restricted to independent validation only.

On 2026-10-14, the user explicitly approved and downloaded the pinned
`train_GRD/shard-00001.tar` only. Its size is 10,914,938,880 bytes; SHA-256
`f6a3d55c0c36497a310f34d91b6a476af926e261c5047ce6045016f45c18930d` matches
the pinned blob digest, the TAR listing check passed, and all 6,000 samples
decoded. The shard contains author-train events 1111004, 1111005, 1111006,
1111009; author-validation event 1111008; and author-test event 1111007. The
refreshed three-shard partial inventory now includes all three author splits,
16 known event IDs, and no unknown IDs. It is not the full benchmark. The
validation portion is only 81 samples from one event; this is enough to
exercise the guarded prototype pipeline, not to claim stable model selection.
The 2026 Nepal case-study imagery remains excluded from training and
validation. No other Kuro Siwo shard was downloaded in this step.
