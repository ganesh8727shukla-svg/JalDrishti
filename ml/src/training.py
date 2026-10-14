"""Reproducible event-filtered training helpers for JalDrishti's U-Net."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, IterableDataset

from ml.src.dataset import (
    CLASS_NAMES,
    IGNORE_INDEX,
    LINEAR_SIGMA0_CLIP_MAX,
    POLARIZATION_MEAN,
    POLARIZATION_STD,
    iter_event_split,
)
from ml.src.model import UNet
from ml.src.tiling import iter_tiles


class KuroTileDataset(IterableDataset):
    """Stream event-filtered fixed-size patches from an audited shard inventory."""

    def __init__(
        self,
        shard_paths: Iterable[Path],
        split_manifest: dict[str, Any],
        split_name: str,
        *,
        tile_size: int,
        overlap: int,
    ) -> None:
        super().__init__()
        self.shard_paths = tuple(shard_paths)
        self.split_manifest = split_manifest
        self.split_name = split_name
        self.tile_size = tile_size
        self.overlap = overlap

    def __iter__(self):
        if torch.utils.data.get_worker_info() is not None:
            raise RuntimeError("Use num_workers=0 to avoid duplicate streaming shard reads.")
        for example in iter_event_split(self.shard_paths, self.split_name, self.split_manifest):
            for inputs, labels, _ in iter_tiles(
                example.inputs,
                example.labels,
                tile_size=self.tile_size,
                overlap=self.overlap,
            ):
                if not np.any(labels != IGNORE_INDEX):
                    continue
                yield (
                    torch.from_numpy(inputs),
                    torch.from_numpy(labels.astype(np.int64, copy=False)),
                    int(example.event_id),
                )


def seed_everything(seed: int) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed)
    if torch.backends.mps.is_available():
        torch.mps.manual_seed(seed)


def resolve_audited_shards(
    audit_path: Path,
    dataset_root: Path,
    expected_revision: str,
    expected_splits: dict[str, Any] | None = None,
    expected_split_source_revision: str | None = None,
    *,
    allow_partial: bool = False,
    required_splits: tuple[str, ...] = ("train", "validation"),
) -> list[Path]:
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("The dataset event audit is missing or invalid.") from exc
    if audit.get("dataset_revision") != expected_revision:
        raise ValueError("The shard audit was produced for a different pinned dataset revision.")
    if expected_splits is not None and audit.get("splits") != expected_splits:
        raise ValueError("The shard audit event IDs do not match the pinned author split manifest.")
    if (
        expected_split_source_revision is not None
        and audit.get("author_split_source_revision") != expected_split_source_revision
    ):
        raise ValueError("The shard audit used a different author split configuration revision.")
    if audit.get("event_filtering_safe") is not True:
        raise ValueError("Training is blocked because the shard audit found unknown or unassigned event IDs.")
    if audit.get("complete_event_coverage") is not True:
        if not allow_partial or audit.get("audit_scope") != "partial_subset":
            raise ValueError("Training is blocked until a full inventory audit or explicit partial-subset audit is supplied.")
        found_ids = {int(value) for value in audit.get("event_ids_found", [])}
        split_ids = audit.get("splits")
        if not found_ids or not isinstance(split_ids, dict):
            raise ValueError("Partial-subset audit is missing its event IDs or pinned split lists.")
        known_ids = {int(value) for values in split_ids.values() for value in values}
        if not found_ids.issubset(known_ids):
            raise ValueError("Partial-subset audit includes an event not assigned by the pinned author splits.")
        present_splits = {
            name for name, values in split_ids.items()
            if found_ids & {int(value) for value in values}
        }
        missing_required = set(required_splits) - present_splits
        if missing_required:
            raise ValueError(
                "Partial subset lacks required author split events: " + ", ".join(sorted(missing_required))
            )
    if Path(audit.get("shard_root", "")).resolve() != dataset_root.resolve():
        raise ValueError("Training dataset root differs from the audited shard root; rerun the audit at this location.")
    paths = [dataset_root / relative for relative in audit.get("shards", {})]
    if not paths or any(not path.is_file() for path in paths):
        raise ValueError("The audited shard inventory is incomplete or files have moved.")
    sizes = audit.get("shard_sizes_bytes", {})
    if any(sizes.get(str(path.relative_to(dataset_root))) != path.stat().st_size for path in paths):
        raise ValueError("A shard size changed after the event audit; rerun the audit before training.")
    return paths


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def train_model(
    *,
    shard_paths: list[Path],
    split_manifest: dict[str, Any],
    model: UNet,
    output_path: Path,
    epochs: int = 10,
    batch_size: int = 4,
    tile_size: int = 256,
    overlap: int = 32,
    learning_rate: float = 1e-3,
    seed: int = 17,
    early_stopping_patience: int | None = 3,
    audit_sha256: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if epochs < 1 or batch_size < 1 or learning_rate <= 0:
        raise ValueError("epochs, batch_size, and learning_rate must be positive.")
    if early_stopping_patience is not None and early_stopping_patience < 1:
        raise ValueError("early_stopping_patience must be positive or None.")
    if output_path.exists():
        raise FileExistsError("Refusing to overwrite an existing checkpoint.")
    seed_everything(seed)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    loss_fn = nn.CrossEntropyLoss(ignore_index=IGNORE_INDEX, reduction="sum")
    history: list[dict[str, Any]] = []
    best_validation_loss = float("inf")
    epochs_without_improvement = 0
    early_stopped = False

    def loader(split: str) -> DataLoader:
        return DataLoader(
            KuroTileDataset(shard_paths, split_manifest, split, tile_size=tile_size, overlap=overlap),
            batch_size=batch_size,
            num_workers=0,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, epochs + 1):
        model.train()
        train_loss_sum = 0.0
        train_pixels = 0
        for inputs, labels, _event_ids in loader("train"):
            inputs = inputs.to(device=device, dtype=torch.float32)
            labels = labels.to(device=device, dtype=torch.long)
            optimizer.zero_grad(set_to_none=True)
            logits = model(inputs)
            valid_count = int((labels != IGNORE_INDEX).sum().item())
            if valid_count == 0:
                continue
            loss_sum = loss_fn(logits, labels)
            (loss_sum / valid_count).backward()
            optimizer.step()
            train_loss_sum += float(loss_sum.detach().item())
            train_pixels += valid_count

        if train_pixels == 0:
            raise ValueError("No valid training pixels were yielded from the audited train events.")

        model.eval()
        validation_loss_sum = 0.0
        validation_pixels = 0
        with torch.inference_mode():
            for inputs, labels, _event_ids in loader("validation"):
                inputs = inputs.to(device=device, dtype=torch.float32)
                labels = labels.to(device=device, dtype=torch.long)
                logits = model(inputs)
                valid_count = int((labels != IGNORE_INDEX).sum().item())
                if valid_count:
                    validation_loss_sum += float(loss_fn(logits, labels).item())
                    validation_pixels += valid_count
        if validation_pixels == 0:
            raise ValueError("No valid validation pixels were yielded from the audited validation events.")
        validation_loss = validation_loss_sum / validation_pixels
        epoch_record = {
            "epoch": epoch,
            "train_loss_per_valid_pixel": train_loss_sum / train_pixels,
            "validation_loss_per_valid_pixel": validation_loss,
            "train_valid_pixels": train_pixels,
            "validation_valid_pixels": validation_pixels,
        }
        history.append(epoch_record)
        improved = validation_loss < best_validation_loss
        if improved:
            best_validation_loss = validation_loss
            epochs_without_improvement = 0
            checkpoint = {
                "checkpoint_schema": 1,
                "model_state_dict": model.state_dict(),
                "model_config": {
                    "architecture": "jaldrishti_unet_v1",
                    "in_channels": 4,
                    "num_classes": 3,
                    "base_channels": model.base_channels,
                },
                "input_contract": "jaldrishti_s1_two_date_v1",
                "input_contract_details": {
                    "channel_order": ["pre_vv", "pre_vh", "post_vv", "post_vh"],
                    "units": "linear Sigma0 power clipped at 0.15 then normalized with Kuro Siwo v2 author statistics",
                    "linear_sigma0_clip_max": LINEAR_SIGMA0_CLIP_MAX,
                    "polarization_means": list(POLARIZATION_MEAN),
                    "polarization_standard_deviations": list(POLARIZATION_STD),
                    "labels": {str(index): name for index, name in enumerate(CLASS_NAMES)},
                    "ignore_index": IGNORE_INDEX,
                },
                "dataset_id": split_manifest["dataset"]["id"],
                "dataset_revision": split_manifest["dataset"]["revision"],
                "author_split_source_revision": split_manifest["author_split_source"]["revision"],
                "shard_audit_sha256": audit_sha256,
                "shard_inventory": [
                    {"path": str(path), "size_bytes": path.stat().st_size}
                    for path in shard_paths
                ],
                "splits": split_manifest["splits"],
                "training_config": {
                    "seed": seed,
                    "epoch": epoch,
                    "epochs_requested": epochs,
                    "batch_size": batch_size,
                    "tile_size": tile_size,
                    "overlap": overlap,
                    "learning_rate": learning_rate,
                    "early_stopping_patience": early_stopping_patience,
                    "optimizer": "AdamW",
                    "loss": "CrossEntropyLoss(ignore_index=255)",
                    "device": str(device),
                    "best_validation_loss_per_valid_pixel": validation_loss,
                    "torch_version": str(torch.__version__),
                    "numpy_version": np.__version__,
                    "python_version": platform.python_version(),
                },
            }
            temporary_path = output_path.with_suffix(output_path.suffix + ".part")
            torch.save(checkpoint, temporary_path)
            temporary_path.replace(output_path)
        else:
            epochs_without_improvement += 1
        if progress_callback is not None:
            progress_callback(dict(epoch_record))
        if (
            early_stopping_patience is not None
            and epochs_without_improvement >= early_stopping_patience
        ):
            early_stopped = True
            break

    return {
        "status": "early_stopped" if early_stopped else "trained_checkpoint_created",
        "checkpoint_path": str(output_path),
        "checkpoint_sha256": sha256_file(output_path),
        "best_validation_loss_per_valid_pixel": best_validation_loss,
        "epochs_requested": epochs,
        "epochs_completed": len(history),
        "early_stopped": early_stopped,
        "early_stopping_patience": early_stopping_patience,
        "history": history,
        "inference_eligible": False,
        "note": "Training loss selection alone is not independent evaluation or model acceptance.",
    }
