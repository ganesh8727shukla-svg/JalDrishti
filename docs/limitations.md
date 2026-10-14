# Phase 3 limitations and remaining gates

As of 2026-10-14, JalDrishti has one exploratory three-class U-Net checkpoint
trained on a partial Kuro Siwo subset and evaluated once on one held-out event.
This is not an approved or deployment-eligible model. No Nepal prediction,
probability raster, or flood mask has been produced. The current Sentinel pair
remains tagged `ANALYSIS_READY=false` in its provenance and no inference on
that pair has run.

The selected Kuro Siwo dataset declares CC BY 4.0. Its full pinned train/test
shard folders are approximately 157.9 GiB. Four complete archives have been
audited in this project: the 3.105 GB scout, `test_GRD/shard-00000.tar`,
`train_GRD/shard-00001.tar`, and `train_GRD/shard-00002.tar`. The 2026-10-14
three-shard training/evaluation subset has 16 known event IDs, no unknown IDs,
and contains author-train, validation, and held-out test events. It is a
partial inventory, not a full-benchmark audit. A 167,325,696-byte partial for
`test_GRD/shard-00001.tar` has no open downloader and remains unused.

The validation event 1111008 has only 81 samples. The selected subset can
exercise the guarded training and one-time test path, but that small validation
set is weak evidence for robust threshold selection. Event 1111007 is assigned
to the author test split despite appearing in `train_GRD`; event 118 is
assigned to train despite appearing in `test_GRD`. The scout event 1111013 is
also author-test despite its `train_GRD` location. Source folders must never
determine split assignment. The audit utility reads per-sample `actid`, fails
on unknown IDs, and partial training requires both train and validation
events; evaluation requires validation and held-out test events. A full event
inventory audit has not run.

The selected custom U-Net is randomly initialized; the public Kuro Siwo
weights are not used because the relevant checkpoint license/checksum was not
verified. The model is a two-date adaptation (one pre-event and one post-event
VV/VH observation), while the dataset includes two pre-event observations and
one flood-time observation. The second pre-event observation is deliberately
not used by this adaptation. It needs actual training and independent event
evaluation before anyone can claim useful performance.

There is also a preprocessing/domain gap. The current Sentinel pair is
terrain-corrected and aligned in EPSG:32645, without speckle filtering. Kuro
Siwo's published GRD preprocessing uses Lee Sigma filtering and EPSG:3857.
Matching nominal polarization and linear Sigma0 units alone does not erase
that domain difference. No radiometric reference comparison has independently
validated the current pair's absolute values.

No numeric acceptance threshold for deploying to an unseen Nepal event was
specified. The evaluator therefore records results as pending review and
never sets `deployment_approved` or `inference_eligible` to true. Do not
generate a flood mask for the current pair until an agreed acceptance decision
also addresses Kuro-to-Nepal transfer and the processing gap.

## 2026-10-14 subset integrity and clip-domain findings

A separate, Git-ignored training subset uses only the two verified train-folder
shards that decode cleanly: 9,590 author-train samples, 81 validation samples
from one author-validation event, and 2,329 held-out samples from one author-test
event. The subset event audit is
`data/raw/kuro-siwo/subsets/track-b-trainable-2026-10-14/event-audit.json`.
The other cached test-folder archive remains untouched but is excluded because
651 samples contain an undocumented mask value 3 on valid pixels. The reader
rejects that value, consistent with the source's documented 0/1/2 classes.

The reader clips linear Sigma0 at 0.15 before the pinned mean/std normalization,
matching the published BlackBench training description. In the current Nepal
pair, 48.4% of pre-event VV, 51.2% of post-event VV, 10.3% of pre-event VH and
11.8% of post-event VH common-valid values exceed 0.15. This alone is not proof
that the pair is unusable, but it is a measured distribution mismatch requiring
review before inference. The trained checkpoint on this partial subset remains
a prototype and cannot authorize Nepal inference or infrastructure-exposure
counts.

## 2026-10-14 exploratory training and evaluation

The CPU training run requested 10 epochs but was stopped after epoch 5 when
validation loss worsened after epoch 1. The best checkpoint therefore contains
epoch 1 weights; the later epochs were not saved as model weights. The observed
losses are recorded in the ignored run report
`reports/generated/jaldrishti-training-20261014.json`. A single held-out test
pass was then completed and is recorded at
`reports/generated/jaldrishti-evaluation-20261014.json`. Its checkpoint SHA-256
is `ceb99b36cc6818076623a9d365bfeb338939fea018881a803d61904bc6a6345e`.

The validation split contains only event 1111008 (81 samples); its selected
flood threshold was 0.30, chosen on validation only. At the three-class
argmax, flood F1/IoU were 0.6433/0.4742 on validation. The one held-out test
event 1111007 (2,329 samples) gave flood F1/IoU 0.5401/0.3700 at argmax and
0.5791 F1 at the validation-selected 0.30 threshold. Permanent-water recall
was zero on both events. These event-level results are too narrow to establish
generalization; the bootstrap interval has one event and is not evidence of
broad transfer. The report marks `deployment_approved=false` and
`inference_eligible=false`. Do not evaluate this same checkpoint on the test
split again: the one-time test-access marker is present. Any follow-up model
iteration needs new independent evaluation data or a predeclared evaluation
plan that preserves the held-out test set.

Routine tests use small synthetic samples and rasters; they do not validate
satellite science, licensing interpretation, event geography, or flood
generalization. Sen1Floods11 remains excluded pending authoritative dataset
license clarification. Copernicus EMS/EMSR927 and other published damage maps
remain validation-only and have not been ingested.
