# Phase 3 model and evaluation status

## 2026-10-14 validation-bearing archive audit

The explicitly approved pinned `train_GRD/shard-00001.tar` completed at
10,914,938,880 bytes. SHA-256
`f6a3d55c0c36497a310f34d91b6a476af926e261c5047ce6045016f45c18930d` matches
the pinned blob digest, and `tar -tf` completed successfully. The sample
reader decoded all 6,000 samples without extracting archive paths. Every
sample exposed four 224 × 224 float32 input channels and valid labels from the
three-class source contract. The detailed ignored report is
`data/raw/kuro-siwo/subsets/track-b-initial/shard-00001-sample-audit.json`.

The shard contains train events 1111004 (840 samples), 1111005 (620), 1111006
(313), and 1111009 (1,817); validation event 1111008 (81); and test event
1111007 (2,329). Its folder name is not authoritative: event 1111007 is in the
author test split and 1111008 is in validation. Metadata geometry places
1111007 approximately at `[84.8636, 24.9218, 86.4935, 26.1564]` WGS84 and
1111008 at `[-83.6453, 13.9452, -83.4240, 14.2185]`; these are derived from
the source `geom` under the documented EPSG:3857 convention, not independent
geolocation checks. The source acquisition dates and label totals are in the
ignored report. Normalized channel extrema include large outliers, so this
audit alone does not establish robust model behavior.

The refreshed partial audit of the three selected training/evaluation shards
finds 16 known author-split event IDs, no unknown IDs, and events in train,
validation, and test. Nine events are under a folder whose name disagrees with
the author split. The audit is
`data/raw/kuro-siwo/subsets/track-b-initial/event-audit-2026-10-14.json`.
The training/evaluation guards can now run in explicit partial-subset mode;
this is not full-corpus benchmark coverage. The South Asian test event 1111007
is held out and must not be used for training or validation. The target 2026
Nepal scenes are also separate from this training subset.

## 2026-10-14 selected shard-00002 audit

The pinned `train_GRD/shard-00002.tar` completed at exactly 10,914,897,920
bytes. Its SHA-256 was verified as
`448fc21a67dc1b4d5fd4cf8c3bb4f926a129ca5cf13899c9f7d6667bb72b74a3`, matching
the pinned Hugging Face blob digest. The streaming reader decoded all 6,000
samples without extracting archive paths. Every sample had four 224 × 224
float32 input channels and usable water labels; 255 is retained as the
ignore/invalid value. The raw per-event content audit is kept outside Git at
`data/raw/kuro-siwo/subsets/track-b-initial/shard-00002-sample-audit.json`.

The shard contains 5,739 samples for event 1111009 (flood date 2022-09-10)
and 261 samples for event 1111010 (flood date 2017-03-10). The pinned author
configuration assigns both to **train**; there are no validation or test
events in this shard. `geom` metadata interpreted under the dataset's
EPSG:3857 convention gives approximate WGS84 envelopes `[67.4431, 25.5557,
69.5358, 28.6363]` for event 1111009 and `[-80.9050, -5.9011, -80.4422,
-5.3605]` for 1111010. These are metadata-derived location checks, not
independent geolocation validation. Event 1111009 is in South Asia; 1111010
is in South America. The normalized input values are finite, but their ranges
include large outliers; robust model behavior and preprocessing compatibility
still require validation.

The partial curated audit in
`data/raw/kuro-siwo/subsets/track-b-initial/event-audit.json` combines this
shard with the previously verified `test_GRD/shard-00000.tar`. It finds 11
known event IDs, no unknown IDs, and no author-validation event. Some files
are stored under folders that disagree with the author split, reinforcing
that samples must be assigned by `actid`. The full dataset is not present.
Training remains blocked by the existing guard until a validation-bearing
archive is explicitly selected, downloaded and audited. Do not create a
chip-level split from these same events to bypass the event-held-out policy.

## 2026-10-11 scout archive audit

The pinned Hugging Face listing contains five `train_GRD` archives. The
smallest is `train_GRD/shard-00004.tar` at 3,105,280,000 bytes. Its downloaded
SHA-256 matches the pinned file digest and all 17,070 TAR members were
readable. The archive contains 1,707 samples, all with `actid=1111013` and
flood date `2017-05-03`; the pinned author split assigns event 1111013 to
**test**. This train-folder/test-split mismatch means this shard must not be
used for training or validation. A coordinate envelope derived from the
sample `geom` metadata (interpreted as EPSG:3857 and transformed to WGS84) is
approximately `[-91.1902, 37.4850, -89.1176, 39.1894]`, in the central United
States and not South Asia. The source sample does not carry a separate CRS
field, so treat that envelope as a metadata-derived location check, not an
authoritative named-region label.

The shard reader successfully decoded a sample to four 224 × 224 float32
channels and the source labels; this checks parsing only. The archive is
reserved as held-out event material and has not produced a Nepal prediction.
At the time of the 2026-10-11 audit, the other four train-folder archives had
not been downloaded and their sample event IDs were unknown. The first
candidate was
[`train_GRD/shard-00002.tar`](https://huggingface.co/datasets/orion-ai-lab/Kuro-Siwo-Webdataset/resolve/e8e61b7b1254b04bfa2b5e26d43db90cb86b5004/train_GRD/shard-00002.tar)
at 10,914,897,920 bytes; the subsequent audit is recorded above.

## Dataset decision

Kuro Siwo is the current candidate for later supervised training because its
dataset card declares CC BY 4.0 and it contains Sentinel-1 GRD temporal inputs
and water labels. The source is pinned in
[`ml/configs/kuro_siwo_splits.json`](../ml/configs/kuro_siwo_splits.json):
Hugging Face revision `e8e61b7b1254b04bfa2b5e26d43db90cb86b5004`, with the
author split configuration pinned to KuroSiwo commit
`4347ed173c4e48f5a9d578bb5fe8453706b08e5e`. Cite Bountos et al. (2024),
“Kuro Siwo: 33 billion m² under the water. A global multi-temporal satellite
dataset for rapid flood mapping,” NeurIPS 2024. Preserve CC BY 4.0 attribution
and license terms for the dataset and annotations. The KuroSiwo repository
code has a separate MIT license; that code license does not replace the
dataset's CC BY license.

The pinned author split is by event ID, not by random chips. The reader filters
every sample's `actid` through those train, validation and test lists. A bounded
metadata read found event `118` in a shard under `test_GRD`, although the pinned
author config assigns 118 to training. The archive directory name is therefore
not accepted as a split authority. Full benchmark coverage still requires a
full event-ID inventory. A curated partial subset may instead be audited with
the explicit `--allow-partial-subset` mode: unknown event IDs fail, the report
lists missing events, training requires at least one author-train and one
author-validation event, and evaluation requires validation and held-out test
events. The subset result must be labeled partial; it is not full-benchmark
evaluation. The split lists are geographically/event separated by the dataset
authors, but JalDrishti has not independently re-established their geographic
distances.

The exact `train_GRD` and `test_GRD` folders total about 157.9 GiB at the pinned
revision. The complete benchmark is not needed for an initial model. Four
archives have been obtained and inspected without downloading the full set:
the 3,105,280,000-byte train-folder scout described above, the
10,914,856,960-byte `test_GRD/shard-00000.tar` cache archive with SHA-256
`4b2b5c9294b50cdc09f9d7c5105eb95f5fd02601e776f8131af4bf48c9e1db2a`, and
`train_GRD/shard-00001.tar` and `train_GRD/shard-00002.tar` described above.
The test-folder archive contains
events `118`, `130`, `147`, `174`, `205`, `267`, `273`, `275`, and `277`: the
pinned author split assigns seven to train and two to test, with no validation
event in that archive. Its directory name is not a split authority, and the
archive as a whole is not a valid training subset. A separate
`test_GRD/shard-00001.tar` transfer has only
167,325,696 bytes of its expected 10,914,887,680 bytes and has no open
downloader; it is stopped and is not being treated as complete.

The partial inventory now contains author-train, validation, and held-out test
events, so guarded prototype training and one partial held-out evaluation can
be run. The validation subset currently contains only one event with 81
samples, which is too small to establish robust threshold selection or model
acceptance. The full inventory remains unaudited. Any further archive should
be selected only if a documented need for more independent validation or
training-event diversity justifies another large transfer.
Sen1Floods11 is not selected because an authoritative dataset license was not
verified. No pretrained Kuro Siwo checkpoint is selected: the public checkpoint
links do not provide a sufficiently verified weight license/checksum for this
use.

## Declared JalDrishti model input contract

The additive U-Net is a newly initialized, lightweight three-class model. It is
not the authors' pretrained network. One exploratory checkpoint has since
been trained and evaluated as documented below; it is not accepted for
deployment.
Inputs are four float32 channels ordered `pre-VV, pre-VH, post-VV, post-VH`;
labels are `0=no-water`, `1=permanent-water`, `2=flood`, with `255` ignored.
Kuro Siwo source arrays are linear Sigma0. The reader clips at a maximum of
`0.15` linear Sigma0, then normalizes per polarization using the pinned author
configuration means `(0.0953, 0.0264)` and standard deviations `(0.0427,
0.0215)`, repeated for the two selected dates. This clipping step follows the
published BlackBench training setup; see the [Kuro Siwo supplementary
material](https://proceedings.neurips.cc/paper_files/paper/2024/file/43612b0662cb6a4986edf859fd6ebafe-Supplemental-Datasets_and_Benchmarks_Track.pdf).
There is no dB conversion, local-statistics normalization or test-set fitting
in this reader. Non-finite and non-positive inputs, and pixels invalid in the source
mask, are excluded from the loss. The selected two-date adaptation uses `sec1`
and `flood` arrays from the Kuro example; it does not use the dataset's second
pre-event acquisition. This is a new model contract, not compatibility with a
published checkpoint.

The current JalDrishti pair is structurally compatible at the raster level:
the real rasters expose VV/VH linear Sigma0, share an EPSG:32645 10 m grid, and
have a common finite-positive mask. That is not enough to establish model
compatibility. Kuro's documented preprocessing includes Lee Sigma filtering
and EPSG:3857 terrain correction; JalDrishti's current outputs have no speckle
filter and use EPSG:32645. The current pair also has no independent absolute
radiometric validation. Input-contract checking therefore reports format
compatibility but keeps inference ineligible pending training, held-out
evaluation, preprocessing/domain-shift review and model acceptance criteria.

## Metrics and threshold policy

`ml/src/metrics.py` computes a multiclass confusion matrix and per-class
precision, recall, F1 and IoU, plus binary flood precision, recall, F1 and IoU.
No-water and permanent water are treated as non-flood in the binary metric;
label 255 and any explicit invalid mask are excluded. Empty denominators are
reported as `null`, not silently converted into zero. A flood threshold can be
selected from validation examples only; the held-out test split must not be
used to choose it. Raw softmax scores are marked uncalibrated.
The evaluation code can report a 95% percentile interval from a deterministic
event-cluster bootstrap, resampling whole events (not pixels) at the declared
threshold. This is descriptive uncertainty over the selected test events, not
a calibrated probability interval or a guarantee of geographic transfer.

## 2026-10-14 exploratory training and one-time held-out evaluation

The partial-subset CPU run requested 10 epochs and completed epochs 1–5. It
was interrupted after epoch 5 because validation loss rose after epoch 1. The
best-checkpoint policy retained the epoch 1 weights (best validation loss
0.322029 per valid pixel); no final epoch-5 checkpoint was promoted. The
observed epoch losses are preserved in
`reports/generated/jaldrishti-training-20261014.json`, including the note that
the run ended early. The checkpoint is
`ml/checkpoints/jaldrishti-unet-kuro-siwo-20261014.pt`, SHA-256
`ceb99b36cc6818076623a9d365bfeb338939fea018881a803d61904bc6a6345e`.

One held-out test pass was completed. The validation-only threshold selection
chose 0.30 with validation binary flood F1 0.6857. At the three-class argmax,
validation event 1111008 (81 samples) had flood precision 0.6919, recall
0.6011, F1 0.6433 and IoU 0.4742. Held-out test event 1111007 (2,329 samples)
had argmax flood precision 0.5215, recall 0.5601, F1 0.5401 and IoU 0.3700;
at the validation-selected 0.30 threshold its flood F1 was 0.5791. Permanent
water recall was zero in both splits. See
`reports/generated/jaldrishti-evaluation-20261014.json` for full confusion
matrices and limitations. The event-cluster bootstrap has one event per split,
so it cannot support broad generalization claims.

The report explicitly sets `deployment_approved=false`,
`inference_eligible=false`, and `probability_calibrated=false`. This evaluation
does not establish transfer to the Nepal pair; no Nepal inference, mask,
probability raster or flood estimate has been generated. The checkpoint's
one-time held-out test access marker is present; do not rerun evaluation on
that same checkpoint. Continue to keep Phase 4 exposure results pending.

## Reproducibility artifacts

The split manifest records immutable dataset/source revisions and event IDs.
The sample reader streams TAR members without extracting their paths. On
2026-10-10, the ML suite ran with pinned PyTorch 2.10.0: **24 tests passed**.
This includes the U-Net forward/backward check, strict sample parsing,
invalid-pixel handling, event filtering/audit, tile reconstruction,
input-raster contract checks and metric calculations. The existing API
regression suite also passed **70 tests** unchanged. A separate metadata and
statistics pass over the real pre/post rasters found 1,317,470 common valid
pixels (95.18% of the shared grid); it reported structural format compatibility
but explicitly left inference ineligible. Neither test suite trained or
evaluated a model or generated a flood output. Unit tests use small synthetic
samples/rasters and are not model evaluation or real-dataset validation.

After adding explicit partial-subset audit support on 2026-10-11, the ML
suite passed **27/27** and the API regression suite passed **70/70**. On
2026-10-14, after the safe checkpoint loader fix, the ML suite passed
**30/30**. The
read-only case-study integration subsequently passed the expanded API suite
at **75/75**. The 2026-10-14 archive audit has since supplied an author
validation event, so guarded partial-subset training is available. Any
resulting model remains a research prototype until it passes independent
event evaluation, explicit acceptance criteria, and preprocessing/domain-shift
review against the Nepal pair. The Nepal case-study scenes remain independent
from training and validation.

## 2026-10-14 training subset and input-gap audit

The trainable subset is explicitly limited to the verified
`train_GRD/shard-00001.tar` and `train_GRD/shard-00002.tar` symlinks under
`data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/`. Its fresh partial
event audit found seven known events, no unknown IDs, five author-train events,
one author-validation event (1111008, 81 samples), and one held-out test event
(1111007, 2,329 samples). The 6,000-sample `test_GRD/shard-00000.tar` remains
preserved in the cache but excluded: 651 samples include label 3 where the
valid mask is 1, although the published class contract specifies 0/1/2. The
code continues to reject such labels; no remapping or silent discard was made.

The real Nepal pair uses the same nominal linear Sigma0 units but is not
distribution-matched. On common valid pixels, 48.4% of pre-event VV and 51.2%
of post-event VV exceed the published 0.15 training clip ceiling (pre/post VH:
10.3%/11.8%). The current pair also lacks the Kuro Siwo Lee Sigma filtering and
uses EPSG:32645 instead of the dataset's EPSG:3857 terrain-corrected grid.
Raster structural checks pass; they do not establish model compatibility or
radiometric accuracy. Nepal inference and Phase 4 exposure remain blocked
pending domain review and an explicit acceptance gate.

## Inference and independent-validation handoff

`ml.src.inference` provides tiled inference for the exact four-channel model
contract: pre-event VV, pre-event VH, post-event VV, post-event VH; calibrated
linear Sigma0 power is clipped at the published Kuro Siwo ceiling and
normalized with the dataset statistics. It refuses jobs unless the
preprocessing manifest explicitly says `validated_for_model`, both output
rasters are analysis-ready, the scenes bracket the event and match on orbit,
polarization, common AOI and grid, and the selected checkpoint, evaluation
report, and reviewer approval manifest all match their recorded SHA-256
evidence. The current Nepal job fails this gate with
`model_input_review_required`; the runner has not created a Nepal prediction.
The reviewer JSON schema is `jaldrishti.inference-approval.v1`; it must bind
the exact checkpoint/evaluation/pre/post hashes, event date and AOI, and name
the reviewer/time, event ID and accepted input contract. The approval and
evaluation report must separately declare deployment and inference
eligibility. No approval JSON is present; the current report explicitly keeps
both booleans false.

When all gates eventually pass, the runner writes a binary flood-mask GeoTIFF,
a three-class GeoTIFF, an uncalibrated softmax-score GeoTIFF, vector boundaries,
and provenance under `data/processed/inference/<analysis-id>/`. Those are
candidate outputs with `pending_independent_validation` readiness, not
analysis-ready products. The uncalibrated softmax score is not model confidence
or a calibrated probability.

The asynchronous API contract is `POST /api/v1/analyses` with
`{"preprocessing_job_id":"<completed-job-uuid>"}` followed by polling
`GET /api/v1/analyses/{analysis_id}`. It does not accept arbitrary URLs or local
paths. The API returns null flood area, mask URL, probability URL, and boundary
URL while blocked or pending review; it only serves a binary mask GeoTIFF and
GeoJSON boundary after the Phase 4 validator accepts a reviewer-approved
independent validation report. No calibrated probability artifact is served.

`ml.src.independent_validation validate-mask` compares an inference candidate
with a separate, license- and source-attributed reference raster under
`data/validation/`. It requires exact grid equality and does not silently
resample. It computes confusion counts, flood IoU/F1/precision/recall and
valid-pixel coverage, then writes a report marked pending review. The separate
`accept-mask` command requires a human reviewer, a policy ID, and explicit
minimum IoU, recall and coverage thresholds. Phase 4 rechecks the report hash,
reference hash, event/AOI, and mask hash before it permits exposure. This
workflow is implemented but has not been run on JalDrishti's Nepal case because
there is no approved Nepal prediction and no independent Nepal reference map in
`data/validation/`.

Primary sources:

* [Kuro Siwo dataset card and license](https://huggingface.co/datasets/orion-ai-lab/Kuro-Siwo-Webdataset)
* [Pinned author training configuration](https://github.com/Orion-AI-Lab/KuroSiwo/blob/4347ed173c4e48f5a9d578bb5fe8453706b08e5e/configs/train/data_config.json)
* [Kuro Siwo paper](https://arxiv.org/abs/2408.09923)
* [Sen1Floods11 source repository](https://github.com/cloudtostreet/Sen1Floods11) and [license status discussion](https://github.com/cloudtostreet/Sen1Floods11/issues/18)
