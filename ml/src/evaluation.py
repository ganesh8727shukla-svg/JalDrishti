"""Validation-only threshold selection followed by one held-out event test pass."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.torch_version import TorchVersion
from torch.utils.data import DataLoader

from ml.src.dataset import IGNORE_INDEX
from ml.src.metrics import (
    ThresholdSweep,
    bootstrap_event_flood_intervals,
    class_metrics,
    confusion_matrix,
)
from ml.src.model import UNet
from ml.src.training import KuroTileDataset, sha256_file


def _load_checkpoint_safely(checkpoint_path: Path) -> dict[str, Any]:
    """Load the project-generated checkpoint using PyTorch's restricted unpickler.

    Older checkpoint metadata may contain ``torch.__version__`` as its
    ``TorchVersion`` str subclass. Allowlist only that harmless metadata type;
    tensor weights and the rest of the checkpoint remain under weights_only.
    """
    try:
        with torch.serialization.safe_globals([TorchVersion]):
            checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    except (OSError, RuntimeError, ValueError, TypeError, pickle.UnpicklingError) as exc:
        raise ValueError("Could not load the model checkpoint safely.") from exc
    if not isinstance(checkpoint, dict):
        raise ValueError("Model checkpoint must contain a dictionary.")
    return checkpoint


def _evaluate_split(
    model: UNet,
    shard_paths: list[Path],
    split_manifest: dict[str, Any],
    split_name: str,
    *,
    batch_size: int,
    tile_size: int,
    overlap: int = 0,
    selected_threshold: float | None = None,
) -> dict[str, Any]:
    device = next(model.parameters()).device
    loader = DataLoader(
        KuroTileDataset(
            shard_paths,
            split_manifest,
            split_name,
            tile_size=tile_size,
            overlap=overlap,
        ),
        batch_size=batch_size,
        num_workers=0,
    )
    aggregate_matrix = np.zeros((3, 3), dtype=np.int64)
    event_matrices: dict[int, np.ndarray] = defaultdict(lambda: np.zeros((3, 3), dtype=np.int64))
    event_flood_matrices: dict[int, np.ndarray] = defaultdict(lambda: np.zeros((2, 2), dtype=np.int64))
    event_sweeps: dict[int, ThresholdSweep] = {}
    aggregate_sweep = ThresholdSweep() if selected_threshold is None else ThresholdSweep([selected_threshold])
    valid_pixels = 0
    ignored_pixels = 0
    tile_pixels = 0
    uncertainty_threshold = 0.5 if selected_threshold is None else selected_threshold

    def sweep_summary(sweep: ThresholdSweep) -> dict[str, Any]:
        result = sweep.result()
        if selected_threshold is None:
            return result
        chosen = result["candidates"][0]
        return {
            "threshold": selected_threshold,
            "f1_at_threshold": chosen["f1"],
            "tp": chosen["tp"],
            "fp": chosen["fp"],
            "fn": chosen["fn"],
            "threshold_source": "validation_only",
        }
    model.eval()
    with torch.inference_mode():
        for inputs, labels, event_ids in loader:
            inputs = inputs.to(device=device, dtype=torch.float32)
            logits = model(inputs)
            probabilities = torch.softmax(logits, dim=1).cpu().numpy()
            predictions = logits.argmax(dim=1).cpu().numpy()
            labels_np = labels.numpy()
            event_ids_np = event_ids.numpy()
            for index, raw_event_id in enumerate(event_ids_np):
                event_id = int(raw_event_id)
                truth = labels_np[index]
                prediction = predictions[index]
                flood_probability = probabilities[index, 2]
                valid = truth != IGNORE_INDEX
                matrix = confusion_matrix(truth, prediction, valid_mask=valid)
                event_matrices[event_id] += matrix
                aggregate_matrix += matrix
                binary_truth = np.where(truth == IGNORE_INDEX, IGNORE_INDEX, truth == 2)
                binary_prediction = (flood_probability >= uncertainty_threshold).astype(np.uint8)
                event_flood_matrices[event_id] += confusion_matrix(
                    binary_truth,
                    binary_prediction,
                    num_classes=2,
                    valid_mask=valid,
                )
                valid_pixels += int(valid.sum())
                ignored_pixels += int((~valid).sum())
                tile_pixels += int(truth.size)
                if event_id not in event_sweeps:
                    event_sweeps[event_id] = (
                        ThresholdSweep() if selected_threshold is None else ThresholdSweep([selected_threshold])
                    )
                event_sweep = event_sweeps[event_id]
                event_sweep.update(truth, flood_probability, valid)
                aggregate_sweep.update(truth, flood_probability, valid)

    if valid_pixels == 0:
        raise ValueError(f"No valid labeled pixels were yielded from {split_name} events.")
    return {
        "split": split_name,
        "event_ids": sorted(event_matrices),
        "valid_pixel_count": valid_pixels,
        "ignored_pixel_count_including_tile_padding": ignored_pixels,
        "tile_pixel_count_including_padding": tile_pixels,
        "labeled_pixel_fraction_of_tile_area": valid_pixels / tile_pixels,
        "confusion_matrix": aggregate_matrix.tolist(),
        "class_metrics": class_metrics(aggregate_matrix),
        "flood_metric_uncertainty": bootstrap_event_flood_intervals(
            dict(event_flood_matrices), seed=17, replicates=1000
        ),
        "uncertainty_threshold": uncertainty_threshold,
        "flood_threshold_sweep": sweep_summary(aggregate_sweep),
        "per_event": {
            str(event_id): {
                "confusion_matrix": matrix.tolist(),
                "class_metrics": class_metrics(matrix),
                "flood_threshold_sweep": sweep_summary(event_sweeps[event_id]),
            }
            for event_id, matrix in sorted(event_matrices.items())
        },
    }


def evaluate_checkpoint_once(
    checkpoint_path: Path,
    shard_paths: list[Path],
    split_manifest: dict[str, Any],
    *,
    batch_size: int = 4,
    tile_size: int = 256,
) -> dict[str, Any]:
    """Sweep threshold on validation then make a single pass over test events."""
    checkpoint = _load_checkpoint_safely(checkpoint_path)
    if checkpoint.get("dataset_revision") != split_manifest["dataset"]["revision"]:
        raise ValueError("Checkpoint dataset revision does not match the selected split manifest.")
    if checkpoint.get("splits") != split_manifest["splits"]:
        raise ValueError("Checkpoint event splits do not match the current split manifest.")
    if checkpoint.get("author_split_source_revision") != split_manifest["author_split_source"]["revision"]:
        raise ValueError("Checkpoint was trained against a different author event-split source revision.")
    test_marker = checkpoint_path.with_suffix(checkpoint_path.suffix + ".test-split-access.json")
    if test_marker.exists():
        raise ValueError("The held-out test split was already accessed for this checkpoint; refusing a second pass.")
    model_config = checkpoint.get("model_config", {})
    if model_config.get("architecture") != "jaldrishti_unet_v1":
        raise ValueError("Checkpoint architecture is unsupported.")
    model = UNet(
        in_channels=int(model_config.get("in_channels", 0)),
        num_classes=int(model_config.get("num_classes", 0)),
        base_channels=int(model_config.get("base_channels", 0)),
    )
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (KeyError, RuntimeError) as exc:
        raise ValueError("Checkpoint weights do not match the declared U-Net model.") from exc
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)

    validation = _evaluate_split(
        model,
        shard_paths,
        split_manifest,
        "validation",
        batch_size=batch_size,
        tile_size=tile_size,
    )
    selected_threshold = validation["flood_threshold_sweep"]["selected_threshold"]
    if selected_threshold is None:
        raise ValueError("Validation events contain no positive flood labels; threshold selection is undefined.")
    marker = {
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "selected_threshold_from_validation": selected_threshold,
        "test_split_access_count": 1,
    }
    try:
        with test_marker.open("x", encoding="utf-8") as stream:
            json.dump(marker, stream, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise ValueError("The held-out test split was already accessed for this checkpoint.") from exc
    # This is the only test pass in this evaluation run. Test labels do not
    # affect the validation-selected threshold or any training decision.
    test = _evaluate_split(
        model,
        shard_paths,
        split_manifest,
        "test",
        batch_size=batch_size,
        tile_size=tile_size,
        selected_threshold=selected_threshold,
    )
    marker["status"] = "completed"
    marker["completed_at"] = datetime.now(timezone.utc).isoformat()
    test_marker.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "evaluation_status": "completed_pending_review",
        "deployment_approved": False,
        "test_access_count_for_this_run": 1,
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "test_split_access_record": str(test_marker),
        "dataset_id": split_manifest["dataset"]["id"],
        "dataset_revision": split_manifest["dataset"]["revision"],
        "threshold_selection": {
            "source": "validation_only",
            "selected_threshold": selected_threshold,
            "validation_f1": validation["flood_threshold_sweep"]["validation_f1"],
        },
        "validation": validation,
        "test": test,
        "probability_calibrated": False,
        "inference_eligible": False,
        "limitations": [
            "Evaluation on Kuro Siwo is independent of training events but does not establish transfer performance on the current Nepal pair.",
            "The current pair preprocessing differs from the dataset preprocessing, including speckle filtering and map projection.",
            "Raw softmax probabilities are not calibrated confidence.",
            "This report does not itself define or grant deployment acceptance.",
        ],
    }
