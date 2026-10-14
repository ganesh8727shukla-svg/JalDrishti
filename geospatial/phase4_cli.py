"""Command-line entry points for Phase 4 OSM and infrastructure groundwork."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from .infrastructure import (
    DEFAULT_CATEGORIES,
    FEATURE_FILTERS,
    InfrastructureError,
    acquire_ohsome_snapshot,
    audit_snapshot,
    calculate_exposure,
    import_local_snapshot,
    import_geofabrik_pbf,
    prepare_infrastructure,
    validate_flood_mask,
)


def _bbox_arg(values: list[float] | None):
    if values is None:
        return None
    return tuple(values)


def _json_print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m geospatial.phase4_cli", description="JalDrishti OSM snapshot and infrastructure preparation tools. No flood prediction is created.")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch-osm", help="Fetch a bounded historical ohsome v1 GeoJSON snapshot.")
    fetch.add_argument("--event-id", required=True)
    fetch.add_argument("--event-date", required=True, help="Event date YYYY-MM-DD.")
    fetch.add_argument("--event-cutoff", help="Optional UTC timestamp; default is 00:00 UTC at event_date.")
    fetch.add_argument("--snapshot-time", required=True, help="Requested OSM history time in UTC, strictly before event cutoff.")
    fetch.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"), help="WGS84 AOI bounding box.")
    fetch.add_argument("--output-root", default="data/raw/osm")
    fetch.add_argument("--categories", nargs="+", choices=tuple(FEATURE_FILTERS), default=list(DEFAULT_CATEGORIES))
    fetch.add_argument("--max-response-mib", type=int, default=256)

    local = commands.add_parser("import-osm", help="Copy a local GeoJSON extract; local timestamps remain unverified.")
    local.add_argument("--input", required=True)
    local.add_argument("--event-id", required=True)
    local.add_argument("--event-date", required=True)
    local.add_argument("--snapshot-time", help="Claimed snapshot time; it is not trusted historical evidence.")
    local.add_argument("--source-crs", required=True, help="Explicit CRS; never guessed from coordinates.")
    local.add_argument("--provider", default="local import")
    local.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    local.add_argument("--output-root", default="data/raw/osm")

    geofabrik = commands.add_parser("import-geofabrik", help="Verify and AOI-filter an already downloaded, dated Geofabrik OSM PBF.")
    geofabrik.add_argument("--input", required=True, help="Completed local .osm.pbf under --output-root; the importer never downloads it.")
    geofabrik.add_argument("--md5-sidecar", required=True, help="Locally saved official Geofabrik .md5 sidecar.")
    geofabrik.add_argument("--source-url", required=True, help="Exact official HTTPS URL used for this PBF.")
    geofabrik.add_argument("--checksum-url", required=True, help="The source URL followed by .md5.")
    geofabrik.add_argument("--source-page-url", required=True, help="Official Geofabrik region archive HTML page.")
    geofabrik.add_argument("--event-id", required=True)
    geofabrik.add_argument("--event-date", required=True, help="Event date YYYY-MM-DD.")
    geofabrik.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"), help="WGS84 AOI bounding box used to limit extracted features.")
    geofabrik.add_argument("--output-root", default="data/raw/osm")
    geofabrik.add_argument("--categories", nargs="+", choices=tuple(FEATURE_FILTERS), default=[*DEFAULT_CATEGORIES, "settlements"])
    geofabrik.add_argument("--max-features", type=int, default=2_000_000)

    audit = commands.add_parser("audit-osm", help="Verify snapshot file checksum and recorded temporal evidence.")
    audit.add_argument("snapshot_dir")

    prepare = commands.add_parser("prepare", help="Validate, clip and normalize OSM features for an AOI.")
    prepare.add_argument("snapshot_dir")
    prepare.add_argument("--output-dir")
    prepare.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    prepare.add_argument("--measurement-crs", help="Optional projected meter CRS; otherwise UTM is selected from AOI.")

    mask = commands.add_parser("check-mask", help="Check the future real flood-mask readiness contract.")
    mask.add_argument("mask_manifest")
    mask.add_argument("--event-id", required=True)
    mask.add_argument("--event-date", required=True)
    mask.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))

    exposure = commands.add_parser("exposure", help="Calculate exposure only after verified OSM and validated mask gates pass.")
    exposure.add_argument("snapshot_dir")
    exposure.add_argument("prepared_dir")
    exposure.add_argument("mask_manifest")
    exposure.add_argument("--event-id", required=True)
    exposure.add_argument("--event-date", required=True)
    exposure.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    exposure.add_argument("--output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch-osm":
            result = acquire_ohsome_snapshot(
                event_id=args.event_id,
                event_date=args.event_date,
                event_cutoff_timestamp=args.event_cutoff,
                snapshot_timestamp=args.snapshot_time,
                bbox_wgs84=args.bbox,
                output_root=args.output_root,
                categories=args.categories,
                max_response_bytes=args.max_response_mib * 1024 * 1024,
            )
        elif args.command == "import-osm":
            result = import_local_snapshot(
                input_path=args.input,
                output_root=args.output_root,
                event_id=args.event_id,
                event_date=args.event_date,
                snapshot_timestamp=args.snapshot_time,
                source_crs=args.source_crs,
                provider=args.provider,
                bbox_wgs84=_bbox_arg(args.bbox),
            )
        elif args.command == "import-geofabrik":
            result = import_geofabrik_pbf(
                input_path=args.input,
                checksum_path=args.md5_sidecar,
                output_root=args.output_root,
                event_id=args.event_id,
                event_date=args.event_date,
                bbox_wgs84=args.bbox,
                source_url=args.source_url,
                checksum_url=args.checksum_url,
                source_page_url=args.source_page_url,
                categories=args.categories,
                max_features=args.max_features,
            )
        elif args.command == "audit-osm":
            result = audit_snapshot(args.snapshot_dir)
        elif args.command == "prepare":
            snapshot_dir = Path(args.snapshot_dir)
            output = Path(args.output_dir) if args.output_dir else Path("data/processed/infrastructure") / snapshot_dir.name
            result = prepare_infrastructure(snapshot_dir=snapshot_dir, output_dir=output, aoi_bbox_wgs84=_bbox_arg(args.bbox), measurement_crs=args.measurement_crs)
            result["output_directory"] = str(output)
        elif args.command == "check-mask":
            result = validate_flood_mask(args.mask_manifest, event_id=args.event_id, event_date=args.event_date, aoi_bbox_wgs84=_bbox_arg(args.bbox))
            result = {"analysis_ready": result["analysis_ready"], "manifest_path": result["manifest_path"], "raster": result["raster"]}
        elif args.command == "exposure":
            output = args.output or str(Path("data/processed/exposure") / args.event_id / str(uuid.uuid4()) / "exposure_summary.json")
            result = calculate_exposure(
                snapshot_dir=args.snapshot_dir,
                prepared_dir=args.prepared_dir,
                flood_mask_manifest_path=args.mask_manifest,
                event_id=args.event_id,
                event_date=args.event_date,
                output_path=output,
                aoi_bbox_wgs84=args.bbox,
            )
            result["output_path"] = output
        else:
            parser.error("Unknown command")
            return 2
        _json_print(result)
        return 0
    except InfrastructureError as exc:
        print(json.dumps({"status": "blocked" if exc.code in {"historical_snapshot_required", "flood_mask_not_ready", "flood_mask_missing", "pbf_not_found", "checksum_sidecar_missing"} else "failed", "code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "code": "operation_failed", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
