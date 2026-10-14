"""Command line interface for the gated Phase 5 road-access scenario."""
from __future__ import annotations

import argparse
import json
import sys

from .connectivity import ConnectivityError, run_connectivity_scenario
from .infrastructure import InfrastructureError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m geospatial.phase5_cli",
        description="Compare road-network access before and after an explicit flood-mask edge-removal scenario.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scenario = subparsers.add_parser(
        "scenario",
        help="Run only with a verified historical OSM snapshot and an approved real flood mask.",
    )
    scenario.add_argument("snapshot_dir")
    scenario.add_argument("prepared_dir")
    scenario.add_argument("mask_manifest")
    scenario.add_argument("--event-id", required=True)
    scenario.add_argument("--event-date", required=True)
    scenario.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    scenario.add_argument("--output-root", default="data/processed/connectivity")
    scenario.add_argument("--max-snap-m", type=float, default=500.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_connectivity_scenario(
            snapshot_dir=args.snapshot_dir,
            prepared_dir=args.prepared_dir,
            mask_manifest_path=args.mask_manifest,
            event_id=args.event_id,
            event_date=args.event_date,
            aoi_bbox_wgs84=args.bbox,
            output_root=args.output_root,
            max_snap_m=args.max_snap_m,
        )
    except (ConnectivityError, InfrastructureError) as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
