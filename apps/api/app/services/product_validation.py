from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

import rasterio


class ProductValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compute_checksums(
    path: Path,
    provider_checksums: list[dict[str, str]],
) -> dict[str, str]:
    algorithms = {"sha256": hashlib.sha256(), "md5": hashlib.md5(usedforsecurity=False)}
    for checksum in provider_checksums:
        algorithm = re.sub(r"[^a-z0-9]", "", checksum.get("algorithm", "").lower())
        if algorithm not in algorithms:
            try:
                algorithms[algorithm] = hashlib.new(algorithm)
            except (ValueError, TypeError):
                continue

    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                for digest in algorithms.values():
                    digest.update(chunk)
    except OSError as exc:
        raise ProductValidationError("file_unreadable", "The downloaded file could not be read for integrity checks.") from exc
    return {name: digest.hexdigest() for name, digest in algorithms.items()}


def verify_provider_checksums(
    provider_checksums: list[dict[str, str]],
    computed: dict[str, str],
) -> tuple[str, list[str]]:
    if not provider_checksums:
        return "unavailable", ["CDSE did not provide a product checksum; a local SHA-256 was computed."]

    verified = 0
    warnings: list[str] = []
    for checksum in provider_checksums:
        algorithm = checksum.get("algorithm", "").strip()
        value = checksum.get("value", "").strip().lower()
        normalized = re.sub(r"[^a-z0-9]", "", algorithm.lower())
        local_value = computed.get(normalized)
        if local_value is None:
            warnings.append(f"The provider's {algorithm or 'unknown'} checksum was recorded but could not be verified by this runtime.")
            continue
        if not value or local_value.lower() != value:
            raise ProductValidationError("checksum_mismatch", f"The downloaded product did not match the provider's {algorithm} checksum.")
        verified += 1

    if verified:
        return "verified", warnings
    return "unavailable", warnings or ["No supported provider checksum was available; a local SHA-256 was computed."]


def validate_native_archive(
    path: Path,
    expected_size_bytes: int,
    provider_checksums: list[dict[str, str]],
    computed_checksums: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise ProductValidationError("file_missing", "The completed temporary product file is missing.") from exc
    if actual_size <= 0:
        raise ProductValidationError("empty_file", "CDSE returned an empty product file.")
    if actual_size != expected_size_bytes:
        raise ProductValidationError("size_mismatch", "The downloaded file size does not match CDSE catalogue metadata.")

    computed = computed_checksums or compute_checksums(path, provider_checksums)
    checksum_status, checksum_warnings = verify_provider_checksums(provider_checksums, computed)

    if not zipfile.is_zipfile(path):
        raise ProductValidationError("invalid_archive", "The selected Product asset is not a readable ZIP archive.")

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise ProductValidationError("empty_archive", "The selected ZIP archive contains no files.")
            for member in members:
                normalized = member.filename.replace("\\", "/")
                if normalized.startswith("/") or ".." in Path(normalized).parts:
                    raise ProductValidationError("unsafe_archive_path", "The SAFE archive contains an unsafe member path.")

            manifest_names = [
                member.filename
                for member in members
                if member.filename.lower().endswith("manifest.safe")
            ]
            if not manifest_names:
                raise ProductValidationError("missing_safe_manifest", "The ZIP archive does not contain a SAFE manifest.")
            try:
                manifest_content = archive.read(manifest_names[0])
                ElementTree.fromstring(manifest_content)
            except (KeyError, ElementTree.ParseError, OSError) as exc:
                raise ProductValidationError("invalid_safe_manifest", "The SAFE manifest is missing or invalid XML.") from exc

            measurement_tiffs = sorted(
                member.filename
                for member in members
                if "measurement/" in member.filename.lower()
                and member.filename.lower().endswith((".tif", ".tiff"))
                and not member.is_dir()
            )
            if not measurement_tiffs:
                raise ProductValidationError("missing_measurement_raster", "The SAFE archive contains no measurement GeoTIFFs.")

            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ProductValidationError("corrupt_archive_member", "The SAFE archive failed its ZIP CRC check.")
    except ProductValidationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ProductValidationError("invalid_archive", "The SAFE ZIP archive is truncated or corrupt.") from exc

    return {
        "integrity": "valid",
        "archive_format": "ZIP containing SAFE product",
        "file_size_bytes": actual_size,
        "computed_checksums": {
            "SHA-256": computed.get("sha256"),
            "MD5": computed.get("md5"),
        },
        "provider_checksum_status": checksum_status,
        "manifest_count": len(manifest_names),
        "measurement_raster_count": len(measurement_tiffs),
        "measurement_raster_paths": measurement_tiffs,
        "warnings": checksum_warnings,
    }


def inspect_geospatial_metadata(path: Path, integrity: dict[str, Any]) -> dict[str, Any]:
    rasters: list[dict[str, Any]] = []
    used_gcps = False
    for member in integrity["measurement_raster_paths"]:
        virtual_path = f"/vsizip/{path.resolve().as_posix()}/{member}"
        try:
            with rasterio.open(virtual_path) as dataset:
                if dataset.width <= 0 or dataset.height <= 0 or dataset.count <= 0:
                    raise ProductValidationError("invalid_raster_metadata", "A measurement raster has incomplete or invalid geospatial metadata.")

                georeferencing: str
                bounds_source: str
                gcp_count = 0
                if dataset.crs is not None:
                    bounds = [float(value) for value in dataset.bounds]
                    crs = dataset.crs
                    georeferencing = "affine_transform"
                    bounds_source = "affine_transform"
                    valid_bounds = (
                        all(math.isfinite(value) for value in bounds)
                        and bounds[0] < bounds[2]
                        and bounds[1] < bounds[3]
                    )
                else:
                    gcps, gcp_crs = dataset.gcps
                    gcp_count = len(gcps)
                    coordinates = [
                        (float(gcp.x), float(gcp.y), float(gcp.row), float(gcp.col))
                        for gcp in gcps
                    ]
                    if gcp_crs is None or len(coordinates) < 3 or not all(
                        all(math.isfinite(value) for value in coordinate)
                        for coordinate in coordinates
                    ):
                        raise ProductValidationError("invalid_raster_metadata", "A measurement raster has neither a valid affine CRS nor usable ground control points.")
                    xs = [coordinate[0] for coordinate in coordinates]
                    ys = [coordinate[1] for coordinate in coordinates]
                    bounds = [min(xs), min(ys), max(xs), max(ys)]
                    crs = gcp_crs
                    georeferencing = "ground_control_points"
                    bounds_source = "ground_control_points"
                    valid_bounds = bounds[0] < bounds[2] and bounds[1] < bounds[3]
                    used_gcps = True

                if not valid_bounds:
                    raise ProductValidationError("invalid_raster_metadata", "A measurement raster has incomplete or invalid geospatial metadata.")

                nodata = dataset.nodata
                raster = {
                    "path_in_archive": member,
                    "crs": crs.to_string(),
                    "georeferencing": georeferencing,
                    "bounds": bounds,
                    "bounds_source": bounds_source,
                    "width": dataset.width,
                    "height": dataset.height,
                    "band_count": dataset.count,
                    "data_types": list(dataset.dtypes),
                    "nodata": float(nodata) if nodata is not None and math.isfinite(nodata) else None,
                }
                if georeferencing == "ground_control_points":
                    raster["gcp_count"] = gcp_count
                rasters.append(raster)
        except ProductValidationError:
            raise
        except Exception as exc:
            raise ProductValidationError("raster_unreadable", "Rasterio could not open a SAFE measurement raster for metadata inspection.") from exc

    warnings = [
        "This check reads raster metadata only; it does not apply orbit correction, calibration, noise removal, terrain correction, or image alignment."
    ]
    if used_gcps:
        warnings.append(
            "Ground-control-point bounds summarize the listed control points; the raster has not been warped, and these are not validated pixel-edge bounds."
        )

    return {
        "status": "inspected",
        "raster_count": len(rasters),
        "rasters": rasters,
        "pixel_values_read": False,
        "analysis_ready": False,
        "warnings": warnings,
    }
