# Dataset and License Requirements

This file records the mandatory JalDrishti dataset conditions supplied for the project. Check the source's current terms and preserve its required citation before introducing any dataset into a later milestone.

| Dataset or source | Permitted use and conditions | Required attribution or citation |
| --- | --- | --- |
| Sentinel-1 and Sentinel-2 | Use Copernicus Data Space Ecosystem data, free and open under the applicable Copernicus data policy. | When applicable: “Contains modified Copernicus Sentinel data 2026.” |
| OpenStreetMap | Use an appropriate snapshot dated before the 2026-08-26 event; do not treat later edits as pre-event evidence. Follow ODbL attribution and share-alike requirements where applicable. | “© OpenStreetMap contributors.” |
| Kuro Siwo | Follow the applicable repository license before use. | Cite Bountos et al. (2024). |
| Sen1Floods11 | Verify the actual source and license before use. Do not assume an unlicensed original repository permits unrestricted reuse. | Cite Bonafilia et al. (2020). |
| Copernicus EMS, including EMSR927 | Independent validation only. Never use as prediction-pipeline input, training labels, or source flood masks. | “European Union, Copernicus Emergency Management Service data” |
| UNOSAT and other published flood or damage maps | Independent result checking only; keep references separate from model inputs and prediction logic. | Preserve each source's applicable credit when used for independent checking. |

## Milestone 2 use

This milestone queries only Sentinel-1 GRD catalogue metadata from CDSE to verify and rank observation pairs. It does not download raster products or introduce Sentinel-2, OSM, Kuro Siwo, Sen1Floods11, Copernicus EMS/EMSR927, or UNOSAT data. No published flood or damage map is used in the operational search or pair ranking.

The required Sentinel attribution is retained for applicable downstream Sentinel-derived products; catalogue metadata alone is not represented as modified Sentinel imagery. Before any later dataset is added, verify its current source terms and include the required attribution/citation in the relevant outputs and documentation.
