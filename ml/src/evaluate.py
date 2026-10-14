"""Evaluate a trained checkpoint once: validation tuning, then held-out test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ml.src.dataset import load_split_manifest
from ml.src.evaluation import evaluate_checkpoint_once
from ml.src.training import resolve_audited_shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New report path; existing reports are never overwritten.")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument(
        "--allow-partial-subset",
        action="store_true",
        help="Opt in to a curated audit containing known validation and held-out test events.",
    )
    args = parser.parse_args()

    if args.output.exists():
        parser.error("Refusing to overwrite an evaluation report. Use a new run-specific path.")
    split_manifest = load_split_manifest()
    shards = resolve_audited_shards(
        args.audit,
        args.dataset_root,
        split_manifest["dataset"]["revision"],
        split_manifest["splits"],
        split_manifest["author_split_source"]["revision"],
        allow_partial=args.allow_partial_subset,
        required_splits=("validation", "test"),
    )
    result = evaluate_checkpoint_once(
        args.checkpoint,
        shards,
        split_manifest,
        batch_size=args.batch_size,
        tile_size=args.tile_size,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({
        "evaluation_status": result["evaluation_status"],
        "deployment_approved": result["deployment_approved"],
        "checkpoint_sha256": result["checkpoint_sha256"],
        "selected_validation_threshold": result["threshold_selection"]["selected_threshold"],
        "validation_event_ids": result["validation"]["event_ids"],
        "test_event_ids": result["test"]["event_ids"],
        "report": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
