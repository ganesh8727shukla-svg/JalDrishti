"""Audit the contents of local Kuro Siwo shards against event-level splits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ml.src.dataset import EventSplitError, audit_event_inventory, load_split_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-root", type=Path, required=True, help="Root containing the downloaded TAR shards.")
    parser.add_argument("--output", type=Path, required=True, help="Path for the event audit JSON; must be outside Git-tracked source.")
    parser.add_argument("--split-manifest", type=Path, default=None)
    parser.add_argument(
        "--allow-partial-subset",
        action="store_true",
        help="Audit a curated partial archive inventory; unknown actids still fail and missing split events are reported.",
    )
    args = parser.parse_args(argv)

    manifest = load_split_manifest(args.split_manifest)
    shard_root = args.shard_root.resolve()
    shards = sorted(shard_root.rglob("*.tar"))
    try:
        result = audit_event_inventory(shards, manifest["splits"])
    except EventSplitError as exc:
        parser.error(str(exc))
    result["dataset_revision"] = manifest["dataset"]["revision"]
    result["author_split_source_revision"] = manifest["author_split_source"]["revision"]
    result["splits"] = manifest["splits"]
    result["audit_scope"] = "partial_subset" if args.allow_partial_subset else "full_inventory"
    result["shard_root"] = str(shard_root)
    result["shard_sizes_bytes"] = {
        str(path.relative_to(shard_root)): path.stat().st_size
        for path in shards
    }
    result["shards"] = {
        str(Path(path).relative_to(shard_root)): event_ids
        for path, event_ids in result["shards"].items()
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "event_filtering_safe": result["event_filtering_safe"],
        "complete_event_coverage": result["complete_event_coverage"],
        "audit_scope": result["audit_scope"],
        "shard_count": len(shards),
        "event_ids_found": result["event_ids_found"],
        "unknown_event_ids": result["unknown_event_ids"],
        "missing_event_ids_by_split": result["missing_event_ids_by_split"],
        "folder_mismatch_count": len(result["folder_mismatches"]),
        "audit_path": str(args.output),
    }, indent=2))
    scope_is_allowed = result["complete_event_coverage"] or args.allow_partial_subset
    return 0 if result["event_filtering_safe"] and scope_is_allowed else 2


if __name__ == "__main__":
    sys.exit(main())
