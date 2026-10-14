"""Train the custom U-Net using a fully audited, event-filtered Kuro Siwo copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ml.src.dataset import load_split_manifest
from ml.src.model import UNet
from ml.src.training import resolve_audited_shards, sha256_file, train_model
from ml.src.training_records import TrainingProgressRecorder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True, help="JSON created by ml.src.audit_dataset")
    parser.add_argument("--output", type=Path, required=True, help="Checkpoint path under ignored ml/checkpoints or external storage")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--overlap", type=int, default=32)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument(
        "--early-stopping-patience",
        type=int,
        default=3,
        help="Stop after this many completed epochs without validation-loss improvement; use 0 to disable.",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--base-channels", type=int, default=16)
    parser.add_argument(
        "--allow-partial-subset",
        action="store_true",
        help="Opt in to curated shards; the audit must contain known author-split events for both train and validation.",
    )
    args = parser.parse_args()

    history_path = args.output.with_suffix(args.output.suffix + ".training.json")
    if args.output.exists() or history_path.exists():
        parser.error("Refusing to overwrite an existing checkpoint or training record.")
    if args.early_stopping_patience < 0:
        parser.error("--early-stopping-patience must be zero or greater.")

    split_manifest = load_split_manifest()
    shard_paths = resolve_audited_shards(
        args.audit,
        args.dataset_root,
        split_manifest["dataset"]["revision"],
        split_manifest["splits"],
        split_manifest["author_split_source"]["revision"],
        allow_partial=args.allow_partial_subset,
        required_splits=("train", "validation"),
    )
    recorder = TrainingProgressRecorder(
        history_path,
        {
            "dataset_revision": split_manifest["dataset"]["revision"],
            "audit_sha256": sha256_file(args.audit),
            "checkpoint_path": str(args.output),
            "configuration": {
                "epochs_requested": args.epochs,
                "batch_size": args.batch_size,
                "tile_size": args.tile_size,
                "overlap": args.overlap,
                "learning_rate": args.learning_rate,
                "seed": args.seed,
                "base_channels": args.base_channels,
                "early_stopping_patience": args.early_stopping_patience or None,
                "allow_partial_subset": args.allow_partial_subset,
            },
        },
    )
    recorder.start()
    try:
        result = train_model(
            shard_paths=shard_paths,
            split_manifest=split_manifest,
            model=UNet(base_channels=args.base_channels),
            output_path=args.output,
            epochs=args.epochs,
            batch_size=args.batch_size,
            tile_size=args.tile_size,
            overlap=args.overlap,
            learning_rate=args.learning_rate,
            seed=args.seed,
            early_stopping_patience=args.early_stopping_patience or None,
            audit_sha256=sha256_file(args.audit),
            progress_callback=lambda record: (
                recorder.epoch_completed(record),
                print(json.dumps({"event": "epoch_completed", **record}), flush=True),
            ),
        )
    except KeyboardInterrupt:
        recorder.finish("interrupted", interruption="KeyboardInterrupt")
        print(json.dumps({"training_record_path": str(history_path), "run_status": "interrupted"}), flush=True)
        return 130
    except Exception as exc:
        recorder.finish("failed", error_type=type(exc).__name__)
        raise
    recorder.finish(
        result["status"],
        checkpoint_sha256=result["checkpoint_sha256"],
        best_validation_loss_per_valid_pixel=result["best_validation_loss_per_valid_pixel"],
        epochs_completed=result["epochs_completed"],
        early_stopped=result["early_stopped"],
        inference_eligible=False,
    )
    print(json.dumps({**result, "training_record_path": str(history_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
