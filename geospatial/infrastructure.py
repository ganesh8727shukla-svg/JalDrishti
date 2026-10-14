"""Auditable OSM infrastructure preparation and flood-mask exposure gates.

This module does not create flood predictions. Historical OSM data can only be
verified by the acquisition adapter after it checks ohsome's temporal extent
and the returned per-feature snapshot timestamps.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterable
from urllib.parse import urlsplit
import uuid

import httpx
import rasterio
from rasterio.crs import CRS
from rasterio.features import shapes
from rasterio.warp import transform_geom
from shapely import make_valid
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

OHSOME_V1_ROOT = "https://api.ohsome.org/v1"
OSM_ATTRIBUTION = "© OpenStreetMap contributors."
OHSOME_RESPONSE_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
SENTINEL_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2026."
SCHEMA_VERSION = "jaldrishti.osm-snapshot.v1"
PREPARED_SCHEMA_VERSION = "jaldrishti.infrastructure-prepared.v1"

FEATURE_FILTERS = {
    "roads": "(highway=* and (type:way or type:relation))",
    "buildings": "(building=* and (type:way or type:relation))",
    "healthcare": "((amenity in (hospital,clinic,doctors) or healthcare in (hospital,clinic,doctor,health_post)) and (type:node or type:way or type:relation))",
    "bridges": "((bridge!=no and bridge=*) or man_made=bridge) and (type:way or type:relation)",
    "critical": "((emergency in (fire_station,ambulance_station,police) or railway=station) and (type:node or type:way or type:relation))",
    "settlements": "(place in (city,town,village,hamlet,isolated_dwelling) and (type:node or type:way or type:relation))",
}
DEFAULT_CATEGORIES = ("roads", "buildings", "healthcare", "bridges", "critical")


class InfrastructureError(ValueError):
    """Actionable validation or processing failure with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_utc(value: str | date | datetime, field: str = "timestamp") -> datetime:
    """Parse a date or timestamp and normalize it to UTC; reject local times."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        raw = str(value).strip()
        if not raw:
            raise InfrastructureError("invalid_timestamp", f"{field} is required.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InfrastructureError("invalid_timestamp", f"{field} must be an ISO-8601 date or timestamp.") from exc
        if parsed.tzinfo is None:
            if len(raw) == 10:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                raise InfrastructureError("invalid_timestamp", f"{field} must include a UTC timezone (Z or +00:00).")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise InfrastructureError("invalid_timestamp", f"{field} must use UTC; local-time timestamps are not accepted.")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: str | date | datetime) -> str:
    return parse_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def event_cutoff(event_date: str | date, cutoff: str | datetime | None = None) -> datetime:
    if cutoff is not None:
        return parse_utc(cutoff, "event cutoff")
    if isinstance(event_date, datetime):
        return parse_utc(event_date, "event date")
    if isinstance(event_date, date):
        return datetime.combine(event_date, time.min, tzinfo=timezone.utc)
    try:
        return datetime.combine(date.fromisoformat(str(event_date)), time.min, tzinfo=timezone.utc)
    except ValueError as exc:
        raise InfrastructureError("invalid_event_date", "event_date must be YYYY-MM-DD.") from exc


def validate_bbox(bounds: Iterable[float]) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(v) for v in bounds)
    except (TypeError, ValueError) as exc:
        raise InfrastructureError("invalid_aoi", "AOI bbox must contain four numeric WGS84 bounds.") from exc
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise InfrastructureError("invalid_aoi", "AOI bbox must contain four finite WGS84 bounds.")
    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise InfrastructureError("invalid_aoi", "AOI bbox bounds are invalid or cross the antimeridian.")
    return west, south, east, north


def bbox_geometry(bounds: Iterable[float]):
    return box(*validate_bbox(bounds))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any], *, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise InfrastructureError("output_exists", f"Refusing to overwrite existing file: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _required_metadata_extent(metadata: dict[str, Any]) -> tuple[datetime, datetime]:
    extent = metadata.get("extractRegion", {}).get("temporalExtent", {})
    try:
        return parse_utc(extent["fromTimestamp"], "OSHDB temporal start"), parse_utc(extent["toTimestamp"], "OSHDB temporal end")
    except (KeyError, TypeError) as exc:
        raise InfrastructureError("temporal_extent_missing", "ohsome /metadata did not provide extractRegion.temporalExtent.") from exc


def verify_ohsome_temporal_evidence(
    feature_collection: dict[str, Any],
    source_metadata: dict[str, Any],
    requested_timestamp: str | datetime,
    event_date: str | date,
    event_cutoff_timestamp: str | datetime | None = None,
) -> dict[str, Any]:
    """Verify the requested snapshot falls in OSHDB coverage before the event.

    The returned @snapshotTimestamp proves which historical state was queried;
    /metadata's temporal extent proves the backing OSHDB covers that timestamp.
    This does not prove that OSM mapped every asset on the ground.
    """
    requested = parse_utc(requested_timestamp, "requested snapshot timestamp")
    cutoff = event_cutoff(event_date, event_cutoff_timestamp)
    if requested >= cutoff:
        raise InfrastructureError("snapshot_not_pre_event", "The requested OSM snapshot must be strictly earlier than the event cutoff.")
    from_ts, to_ts = _required_metadata_extent(source_metadata)
    if not (from_ts <= requested <= to_ts):
        raise InfrastructureError("snapshot_outside_source_extent", "The ohsome OSHDB temporal extent does not cover the requested snapshot timestamp.")
    if source_metadata.get("attribution", {}).get("text") != OHSOME_RESPONSE_ATTRIBUTION:
        raise InfrastructureError("source_attribution_missing", "The ohsome metadata response lacks the expected OpenStreetMap attribution.")
    if feature_collection.get("type") != "FeatureCollection" or not isinstance(feature_collection.get("features"), list):
        raise InfrastructureError("invalid_geojson", "The ohsome extraction response is not a GeoJSON FeatureCollection.")
    features = feature_collection["features"]
    for index, feature in enumerate(features):
        props = feature.get("properties") or {}
        stamp = props.get("@snapshotTimestamp")
        if stamp is None or parse_utc(stamp, f"feature {index} snapshot timestamp") != requested:
            raise InfrastructureError("feature_snapshot_mismatch", "Every OSM feature must carry @snapshotTimestamp matching the requested historical time.")
        last_edit = props.get("@lastEdit")
        if last_edit and parse_utc(last_edit, f"feature {index} last edit") > requested:
            raise InfrastructureError("feature_edit_after_snapshot", "An OSM feature has @lastEdit later than the requested snapshot.")
    return {
        "status": "verified_historical",
        "requested_snapshot_timestamp": iso_utc(requested),
        "verified_underlying_snapshot_timestamp": iso_utc(requested),
        "event_cutoff_timestamp": iso_utc(cutoff),
        "source_temporal_extent": {"fromTimestamp": iso_utc(from_ts), "toTimestamp": iso_utc(to_ts)},
        "feature_snapshot_timestamps_checked": len(features),
        "source_semantics": "ohsome v1 /elements/geometry historical OSM state at time; checked against /metadata OSHDB temporal extent and per-feature @snapshotTimestamp",
        "coverage_limit": "Temporal verification confirms the queried OSM database state, not completeness or physical existence of every mapped asset.",
    }


def build_ohsome_filter(categories: Iterable[str]) -> str:
    selected = tuple(dict.fromkeys(categories))
    if not selected or any(category not in FEATURE_FILTERS for category in selected):
        raise InfrastructureError("invalid_feature_categories", "Choose one or more supported OSM categories: " + ", ".join(FEATURE_FILTERS))
    return " or ".join(FEATURE_FILTERS[name] for name in selected)


def acquire_ohsome_snapshot(
    *, event_id: str, event_date: str, snapshot_timestamp: str,
    bbox_wgs84: Iterable[float], output_root: str | Path,
    categories: Iterable[str] = DEFAULT_CATEGORIES,
    event_cutoff_timestamp: str | None = None,
    client: httpx.Client | None = None,
    max_response_bytes: int = 256 * 1024 * 1024,
) -> dict[str, Any]:
    """Acquire a bounded historical GeoJSON extract through official ohsome v1."""
    event_id = str(event_id).strip()
    if not event_id or len(event_id) > 128:
        raise InfrastructureError("invalid_event_id", "event_id must be a non-empty identifier up to 128 characters.")
    bounds = validate_bbox(bbox_wgs84)
    requested = parse_utc(snapshot_timestamp, "requested snapshot timestamp")
    cutoff = event_cutoff(event_date, event_cutoff_timestamp)
    categories = tuple(dict.fromkeys(categories))
    if requested >= cutoff:
        raise InfrastructureError("snapshot_not_pre_event", "Select a historical snapshot strictly before the event cutoff.")
    if max_response_bytes <= 0:
        raise InfrastructureError("invalid_size_limit", "max_response_bytes must be positive.")
    output_root = Path(output_root).expanduser().resolve()
    existing_probe = output_root
    while not existing_probe.exists() and existing_probe != existing_probe.parent:
        existing_probe = existing_probe.parent
    free_bytes = shutil.disk_usage(existing_probe).free
    if free_bytes < max_response_bytes + 32 * 1024 * 1024:
        raise InfrastructureError("insufficient_disk_space", "Available disk space is below the configured response cap plus 32 MiB staging headroom.")
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), follow_redirects=False)
    staging_path: Path | None = None
    try:
        meta_response = client.get(f"{OHSOME_V1_ROOT}/metadata")
        meta_response.raise_for_status()
        source_metadata = meta_response.json()
        params = {
            "bboxes": ",".join(format(v, ".8f").rstrip("0").rstrip(".") for v in bounds),
            "time": iso_utc(requested),
            "filter": build_ohsome_filter(categories),
            "properties": "tags,metadata",
            "showMetadata": "yes",
            "clipGeometry": "false",
        }
        with client.stream("POST", f"{OHSOME_V1_ROOT}/elements/geometry", data=params) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_response_bytes:
                raise InfrastructureError("response_too_large", "ohsome response exceeds max_response_bytes; reduce AOI or categories.")
            fd, staging_name = tempfile.mkstemp(prefix="jaldrishti-osm-", suffix=".geojson.part")
            staging_path = Path(staging_name)
            byte_count = 0
            with os.fdopen(fd, "wb") as staging:
                for chunk in response.iter_bytes():
                    byte_count += len(chunk)
                    if byte_count > max_response_bytes:
                        raise InfrastructureError("response_too_large", "ohsome response exceeds max_response_bytes; reduce AOI or categories.")
                    staging.write(chunk)
                staging.flush()
                os.fsync(staging.fileno())
        if byte_count == 0:
            raise InfrastructureError("invalid_geojson", "ohsome extraction returned an empty response.")
        try:
            with staging_path.open("r", encoding="utf-8") as staging:
                feature_collection = json.load(staging)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InfrastructureError("invalid_geojson", "ohsome extraction response was not valid JSON.") from exc
        evidence = verify_ohsome_temporal_evidence(feature_collection, source_metadata, requested, event_date, cutoff)
        feature_count = len(feature_collection["features"])
        snapshot_id = str(uuid.uuid4())
        final_dir = output_root / snapshot_id
        if final_dir.exists():
            raise InfrastructureError("output_exists", f"Snapshot directory already exists: {final_dir}")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=final_dir.parent))
        try:
            geojson_path = temp_dir / "infrastructure.geojson"
            shutil.copyfile(staging_path, geojson_path)
            retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "event_id": event_id,
                "event_date": str(event_date),
                "event_cutoff_timestamp": iso_utc(cutoff),
                "requested_snapshot_timestamp": iso_utc(requested),
                "verified_underlying_snapshot_timestamp": evidence["verified_underlying_snapshot_timestamp"],
                "retrieval_timestamp": retrieved,
                "provider": "ohsome API v1 / HeiGIT OSHDB",
                "provider_metadata_url": f"{OHSOME_V1_ROOT}/metadata",
                "source_url": f"{OHSOME_V1_ROOT}/elements/geometry",
                "source_artifact": "infrastructure.geojson",
                "source_query": {**params, "bboxes": list(bounds)},
                "aoi": {"bbox_wgs84": list(bounds), "crs": "EPSG:4326"},
                "source_format": "GeoJSON FeatureCollection",
                "source_crs": "EPSG:4326",
                "license": "Open Database License (ODbL) 1.0",
                "license_url": ODBL_URL,
                "attribution": OSM_ATTRIBUTION,
                "attribution_url": OSM_COPYRIGHT_URL,
                "source_artifact_size_bytes": geojson_path.stat().st_size,
                "source_artifact_sha256": sha256_file(geojson_path),
                "acquisition_method": "bounded HTTPS POST; ohsome v1 historical extraction; response retained unchanged",
                "temporal_verification": evidence,
                "temporal_verification_status": evidence["status"],
                "source_metadata": source_metadata,
                "attribution_response": feature_collection.get("attribution"),
                "selection": {"categories": list(categories), "feature_count": feature_count, "filter": params["filter"]},
                "geometry_validation_status": "pending_preparation",
                "software": {"python": __import__("platform").python_version(), "rasterio": rasterio.__version__, "shapely": __import__("shapely").__version__},
                "warnings": ["OSM completeness and physical existence are not established by a historical database snapshot.", "ohsome v1 is a transition adapter; migrate to ohsome v2 before its published 2026-11-30 shutdown."],
            }
            (temp_dir / "attribution.txt").write_text(f"{OSM_ATTRIBUTION}\n{OSM_COPYRIGHT_URL}\n", encoding="utf-8")
            _write_json_atomic(temp_dir / "snapshot_metadata.json", manifest)
            os.replace(temp_dir, final_dir)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        manifest["snapshot_directory"] = str(final_dir)
        manifest["source_artifact_path"] = str(final_dir / "infrastructure.geojson")
        return manifest
    except httpx.HTTPStatusError as exc:
        raise InfrastructureError("ohsome_http_error", f"ohsome returned HTTP {exc.response.status_code}.") from exc
    except httpx.TimeoutException as exc:
        raise InfrastructureError("ohsome_timeout", "The ohsome request timed out; reduce the AOI or retry later.") from exc
    except httpx.HTTPError as exc:
        raise InfrastructureError("ohsome_network_error", f"Could not complete the ohsome request ({type(exc).__name__}).") from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, InfrastructureError):
            raise
        raise InfrastructureError("invalid_ohsome_response", "ohsome returned malformed metadata or extraction JSON.") from exc
    finally:
        if staging_path is not None:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass
        if own_client:
            client.close()


def import_local_snapshot(
    *, input_path: str | Path, output_root: str | Path, event_id: str,
    event_date: str, source_crs: str, snapshot_timestamp: str | None = None,
    provider: str = "local import", bbox_wgs84: Iterable[float] | None = None,
) -> dict[str, Any]:
    """Copy a local GeoJSON snapshot and record it as unverified provenance.

    A user-supplied timestamp or filename cannot establish historical status.
    """
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise InfrastructureError("source_not_found", "Input GeoJSON file does not exist.")
    try:
        source_crs_value = CRS.from_user_input(source_crs)
    except Exception as exc:
        raise InfrastructureError("source_crs_invalid", "source_crs must identify a valid CRS; it is never guessed.") from exc
    if source_crs_value is None:
        raise InfrastructureError("source_crs_missing", "source_crs is required for local OSM import.")
    try:
        collection = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfrastructureError("invalid_geojson", "Input file is unreadable or is not valid JSON.") from exc
    if collection.get("type") != "FeatureCollection" or not isinstance(collection.get("features"), list):
        raise InfrastructureError("invalid_geojson", "Input must be a GeoJSON FeatureCollection.")
    for index, feature in enumerate(collection["features"]):
        props = feature.get("properties") or {}
        if not (props.get("@osmId") or props.get("osm_id")):
            raise InfrastructureError("osm_id_missing", f"Local feature {index} lacks a namespaced OSM element ID.")
    if not str(event_id).strip():
        raise InfrastructureError("invalid_event_id", "event_id is required.")
    bounds = validate_bbox(bbox_wgs84) if bbox_wgs84 is not None else None
    claimed_snapshot = iso_utc(snapshot_timestamp) if snapshot_timestamp else None
    snapshot_id = str(uuid.uuid4())
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / snapshot_id
    target.mkdir()
    dest = target / "infrastructure.geojson"
    shutil.copyfile(source, dest)
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    status = "historical_unverified"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "event_id": str(event_id),
        "event_date": str(event_date),
        "event_cutoff_timestamp": iso_utc(event_cutoff(event_date)),
        "requested_snapshot_timestamp": claimed_snapshot,
        "verified_underlying_snapshot_timestamp": None,
        "retrieval_timestamp": retrieved,
        "provider": provider,
        "provider_metadata_url": None,
        "source_url": None,
        "source_local_filename": source.name,
        "source_artifact": "infrastructure.geojson",
        "source_artifact_size_bytes": dest.stat().st_size,
        "source_artifact_sha256": sha256_file(dest),
        "source_format": "GeoJSON FeatureCollection",
        "source_crs": source_crs_value.to_string(),
        "aoi": {"bbox_wgs84": list(bounds) if bounds else None, "crs": "EPSG:4326"},
        "license": "Open Database License (ODbL) 1.0; source license must be confirmed by importer",
        "license_url": ODBL_URL,
        "attribution": OSM_ATTRIBUTION,
        "attribution_url": OSM_COPYRIGHT_URL,
        "acquisition_method": "local copy; manually supplied metadata is not accepted as historical evidence",
        "temporal_verification_status": status,
        "temporal_verification": {"status": status, "reason": "No trusted source metadata and per-feature historical snapshot evidence were supplied."},
        "selection": {"feature_count": len(collection["features"])},
        "geometry_validation_status": "pending_preparation",
        "warnings": ["Local import does not verify snapshot time or underlying OSM history.", "Confirm source provenance and applicable license before using this extract."],
    }
    try:
        (target / "attribution.txt").write_text(f"{OSM_ATTRIBUTION}\n{OSM_COPYRIGHT_URL}\n", encoding="utf-8")
        _write_json_atomic(target / "snapshot_metadata.json", manifest)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    manifest["snapshot_directory"] = str(target)
    manifest["source_artifact_path"] = str(dest)
    return manifest


def _geofabrik_snapshot_date(filename: str) -> date:
    match = re.search(r"-(\d{6})\.osm\.pbf$", filename)
    if not match:
        raise InfrastructureError("geofabrik_snapshot_date_missing", "Expected a Geofabrik filename ending in -YYMMDD.osm.pbf.")
    raw = match.group(1)
    try:
        return date(int("20" + raw[:2]), int(raw[2:4]), int(raw[4:6]))
    except ValueError as exc:
        raise InfrastructureError("geofabrik_snapshot_date_invalid", "The date encoded in the Geofabrik filename is invalid.") from exc


def _parse_geofabrik_md5(path: Path, expected_filename: str) -> str:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise InfrastructureError("checksum_sidecar_invalid", "The Geofabrik MD5 sidecar is missing or unreadable.") from exc
    match = re.fullmatch(r"([0-9a-fA-F]{32})(?:\s+\*?(.+?))?", text)
    if not match:
        raise InfrastructureError("checksum_sidecar_invalid", "The MD5 sidecar must contain one 32-character MD5 and optional filename.")
    sidecar_filename = (match.group(2) or expected_filename).strip()
    if Path(sidecar_filename).name != expected_filename:
        raise InfrastructureError("checksum_sidecar_mismatch", "The MD5 sidecar names a different file than the selected PBF.")
    return match.group(1).lower()


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_sha256_file(path: Path) -> tuple[str, str]:
    md5_digest = hashlib.md5()
    sha256_digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            md5_digest.update(chunk)
            sha256_digest.update(chunk)
    return md5_digest.hexdigest(), sha256_digest.hexdigest()


def _safe_geofabrik_urls(source_url: str, checksum_url: str, page_url: str, filename: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(source_url)
    checksum = urlparse(checksum_url)
    page = urlparse(page_url)
    expected_page = re.sub(r"-\d{6}\.osm\.pbf$", ".html", parsed.path)
    if (
        parsed.scheme != "https" or parsed.hostname != "download.geofabrik.de"
        or parsed.query or parsed.fragment or parsed.username or parsed.password
        or parsed.path.rsplit("/", 1)[-1] != filename
        or not re.search(r"-\d{6}\.osm\.pbf$", parsed.path)
    ):
        raise InfrastructureError("geofabrik_source_invalid", "Source URL must be the HTTPS Geofabrik download URL for the selected PBF filename.")
    if checksum_url != source_url + ".md5" or checksum.scheme != "https" or checksum.hostname != "download.geofabrik.de" or checksum.query or checksum.fragment:
        raise InfrastructureError("geofabrik_checksum_url_invalid", "Checksum URL must be the selected Geofabrik source URL followed by .md5.")
    if page.scheme != "https" or page.hostname != "download.geofabrik.de" or page.path != expected_page or page.query or page.fragment:
        raise InfrastructureError("geofabrik_source_page_invalid", "Source page must be the matching HTTPS Geofabrik regional archive page.")


def import_geofabrik_pbf(
    *, input_path: str | Path, checksum_path: str | Path, output_root: str | Path,
    event_id: str, event_date: str, bbox_wgs84: Iterable[float],
    source_url: str, checksum_url: str, source_page_url: str,
    categories: Iterable[str] = (*DEFAULT_CATEGORIES, "settlements"),
    max_features: int = 2_000_000,
) -> dict[str, Any]:
    """Validate and extract a dated Geofabrik PBF to AOI-filtered OSM GeoJSON.

    The provider date is calendar-day provenance, not a precise per-feature
    history timestamp. The raw PBF remains unchanged and is never copied.
    """
    source = Path(input_path).expanduser().resolve()
    sidecar = Path(checksum_path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    if not source.is_file() or source.suffix != ".pbf" or not source.name.endswith(".osm.pbf"):
        raise InfrastructureError("pbf_not_found", "A local .osm.pbf source file is required; downloads are not started by this command.")
    if not sidecar.is_file():
        raise InfrastructureError("checksum_sidecar_missing", "Fetch the official Geofabrik .md5 sidecar before importing the PBF.")
    if not str(event_id).strip() or len(str(event_id)) > 128:
        raise InfrastructureError("invalid_event_id", "event_id must be a non-empty identifier up to 128 characters.")
    if max_features <= 0:
        raise InfrastructureError("invalid_feature_limit", "max_features must be a positive integer.")
    bounds = validate_bbox(bbox_wgs84)
    cutoff = event_cutoff(event_date)
    snapshot_date = _geofabrik_snapshot_date(source.name)
    if snapshot_date >= cutoff.date():
        raise InfrastructureError("snapshot_not_pre_event", "The Geofabrik snapshot date must be strictly earlier than the event date.")
    _safe_geofabrik_urls(source_url, checksum_url, source_page_url, source.name)
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise InfrastructureError("pbf_outside_data_root", "Keep the downloaded PBF under output_root so its manifest can use a project-relative path.") from exc
    if source.stat().st_size <= 0:
        raise InfrastructureError("pbf_empty", "The selected PBF file is empty or incomplete.")
    expected_md5 = _parse_geofabrik_md5(sidecar, source.name)
    source_size_bytes = source.stat().st_size
    observed_md5, observed_sha256 = _md5_sha256_file(source)
    if observed_md5 != expected_md5:
        raise InfrastructureError("pbf_checksum_mismatch", "The downloaded PBF MD5 does not match the official Geofabrik sidecar.")
    categories = tuple(dict.fromkeys(categories))
    if not categories or any(category not in FEATURE_FILTERS for category in categories):
        raise InfrastructureError("invalid_feature_categories", "Choose one or more supported OSM categories: " + ", ".join(FEATURE_FILTERS))
    try:
        import osmium
    except ImportError as exc:
        raise InfrastructureError("osmium_missing", "Install backend requirements (including osmium) before importing an OSM PBF.") from exc

    root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(root).free
    index_budget = max(2 * 1024 * 1024 * 1024, source.stat().st_size * 5)
    if free_bytes < index_budget + 256 * 1024 * 1024:
        raise InfrastructureError("insufficient_disk_space", "Available space must cover an estimated sparse OSM node index, filtered output, and 256 MiB staging reserve.")
    snapshot_id = str(uuid.uuid4())
    final_dir = root / snapshot_id
    bbox = bbox_geometry(bounds)
    factory = osmium.geom.GeoJSONFactory()
    selected_categories = set(categories)
    counters = {"node": 0, "way": 0, "relation_area": 0, "excluded_tag": 0, "excluded_outside_aoi": 0, "invalid_geometry": 0}
    seen: set[str] = set()
    max_bytes = min(2 * 1024 * 1024 * 1024, free_bytes - index_budget - 128 * 1024 * 1024)
    if max_bytes < 128 * 1024 * 1024:
        raise InfrastructureError("insufficient_disk_space", "Less than 128 MiB remains for the AOI-filtered GeoJSON after reserving PBF index space.")
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=root))
    geojson_path = temp_dir / "infrastructure.geojson"

    def write_feature(stream, osm_type: str, osm_id: int, tags, version: int, timestamp, geometry_json: str, counter_key: str):
        tag_map = {str(tag.k): str(tag.v) for tag in tags}
        found_categories = [item for item in categorize_tags(tag_map) if item in selected_categories]
        if not found_categories:
            counters["excluded_tag"] += 1
            return
        osm_identity = f"{osm_type}/{int(osm_id)}"
        if osm_identity in seen:
            return
        try:
            geometry = shape(json.loads(geometry_json))
        except (ValueError, TypeError, json.JSONDecodeError):
            counters["invalid_geometry"] += 1
            return
        if geometry.is_empty or not geometry.is_valid:
            counters["invalid_geometry"] += 1
            return
        if not geometry.intersects(bbox):
            counters["excluded_outside_aoi"] += 1
            return
        stamp = None
        if timestamp is not None:
            try:
                stamp = timestamp.to_datetime().replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            except (AttributeError, ValueError, OverflowError):
                stamp = None
        properties = {"@osmId": osm_identity, "@osmType": osm_type, "@version": int(version), "tags": tag_map}
        if stamp:
            properties["@lastEdit"] = stamp
        record = {"type": "Feature", "id": osm_identity, "geometry": json.loads(geometry_json), "properties": properties}
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        stream.write(("," if counters["node"] + counters["way"] + counters["relation_area"] else "") + encoded)
        counters[counter_key] += 1
        seen.add(osm_identity)
        if len(seen) > max_features:
            raise InfrastructureError("feature_limit_exceeded", "Selected PBF extraction exceeded max_features; narrow the AOI or reduce category scope.")
        if stream.tell() > max_bytes:
            raise InfrastructureError("geojson_size_limit_exceeded", "Filtered GeoJSON exceeded the 2 GiB safety limit; narrow the AOI or category scope.")

    class Extractor(osmium.SimpleHandler):
        def __init__(self, stream):
            super().__init__()
            self.stream = stream

        def node(self, node):
            if not node.location.valid():
                counters["invalid_geometry"] += 1
                return
            try:
                geometry_json = factory.create_point(node)
            except Exception:
                counters["invalid_geometry"] += 1
                return
            write_feature(self.stream, "node", node.id, node.tags, node.version, node.timestamp, geometry_json, "node")

        def way(self, way):
            tags = {str(tag.k): str(tag.v) for tag in way.tags}
            found = [item for item in categorize_tags(tags) if item in selected_categories]
            if not found or not ("roads" in found or "bridges" in found):
                return
            try:
                geometry_json = factory.create_linestring(way)
            except Exception:
                counters["invalid_geometry"] += 1
                return
            write_feature(self.stream, "way", way.id, way.tags, way.version, way.timestamp, geometry_json, "way")

        def area(self, area):
            tags = {str(tag.k): str(tag.v) for tag in area.tags}
            found = [item for item in categorize_tags(tags) if item in selected_categories]
            if not found or not (set(found) - {"roads", "bridges"}):
                return
            try:
                geometry_json = factory.create_multipolygon(area)
            except Exception:
                counters["invalid_geometry"] += 1
                return
            osm_type = "way" if area.from_way() else "relation"
            write_feature(self.stream, osm_type, area.orig_id(), area.tags, area.version, area.timestamp, geometry_json, "relation_area")

    try:
        with geojson_path.open("w", encoding="utf-8") as stream:
            stream.write('{"type":"FeatureCollection","features":[')
            extractor = Extractor(stream)
            with tempfile.TemporaryDirectory(prefix="jaldrishti-osm-index-", dir=root) as index_dir:
                index_spec = f"sparse_file_array,{Path(index_dir) / 'locations'}"
                try:
                    extractor.apply_file(str(source), locations=True, idx=index_spec)
                except InfrastructureError:
                    raise
                except Exception as exc:
                    raise InfrastructureError("pbf_parse_failed", "PyOsmium could not parse the selected PBF; confirm the transfer completed and the PBF is valid.") from exc
            stream.write("]}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not seen:
            raise InfrastructureError("no_osm_features_in_aoi", "The valid source PBF contains no selected tagged features intersecting this AOI.")
        if source.stat().st_size != source_size_bytes or _md5_file(source) != expected_md5:
            raise InfrastructureError("pbf_changed_during_import", "The source PBF changed while it was being parsed; no snapshot was accepted.")
        retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        pbf_relative = source.relative_to(root).as_posix()
        evidence = {
            "status": "provider_dated_historical",
            "provider_snapshot_date": snapshot_date.isoformat(),
            "date_precision": "calendar_day_from_Geofabrik_filename",
            "event_cutoff_date": cutoff.date().isoformat(),
            "pre_event": True,
            "source_page_url": source_page_url,
            "source_file_url": source_url,
            "provider_md5_verified": True,
            "per_feature_snapshot_timestamps_available": False,
            "limitations": "Geofabrik filename and checksum identify the provider's dated regional extract. This is date-level snapshot evidence; it does not prove an exact per-feature edit timestamp or map completeness.",
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "event_id": str(event_id),
            "event_date": str(event_date),
            "event_cutoff_timestamp": iso_utc(cutoff),
            "requested_snapshot_timestamp": None,
            "verified_underlying_snapshot_timestamp": None,
            "provider_snapshot_date": snapshot_date.isoformat(),
            "retrieval_timestamp": retrieved,
            "provider": "Geofabrik regional OpenStreetMap extract",
            "provider_metadata_url": source_page_url,
            "source_url": source_url,
            "source_checksum_url": checksum_url,
            "source_local_filename": source.name,
            "source_pbf_path": pbf_relative,
            "source_pbf_size_bytes": source_size_bytes,
            "source_pbf_md5": observed_md5,
            "source_pbf_sha256": observed_sha256,
            "source_artifact": "infrastructure.geojson",
            "source_format": "GeoJSON FeatureCollection derived from OSM PBF",
            "source_crs": "EPSG:4326",
            "aoi": {"bbox_wgs84": list(bounds), "crs": "EPSG:4326"},
            "license": "Open Database License (ODbL) 1.0",
            "license_url": ODBL_URL,
            "attribution": OSM_ATTRIBUTION,
            "attribution_url": OSM_COPYRIGHT_URL,
            "source_artifact_size_bytes": geojson_path.stat().st_size,
            "source_artifact_sha256": sha256_file(geojson_path),
            "acquisition_method": "local PBF streamed through PyOsmium; source download is not started by this import command",
            "temporal_verification": evidence,
            "temporal_verification_status": evidence["status"],
            "selection": {"categories": list(categories), "feature_count": len(seen), "feature_counts": counters, "bbox_filter_wgs84": list(bounds)},
            "geometry_validation_status": "pending_preparation",
            "software": {"python": __import__("platform").python_version(), "pyosmium": getattr(osmium, "__version__", "version unavailable"), "rasterio": rasterio.__version__, "shapely": __import__("shapely").__version__},
            "warnings": [evidence["limitations"], "PBF element timestamps are retained as source metadata only; they are not treated as an exact snapshot time.", "OSM completeness and physical existence at event time are not established."],
        }
        if final_dir.exists():
            raise InfrastructureError("output_exists", f"Snapshot directory already exists: {final_dir}")
        (temp_dir / "attribution.txt").write_text(f"{OSM_ATTRIBUTION}\n{OSM_COPYRIGHT_URL}\n", encoding="utf-8")
        _write_json_atomic(temp_dir / "snapshot_metadata.json", manifest)
        os.replace(temp_dir, final_dir)
        manifest["snapshot_directory"] = str(final_dir)
        manifest["source_artifact_path"] = str(final_dir / "infrastructure.geojson")
        return manifest
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def load_snapshot(snapshot_dir: str | Path, *, verify_checksum: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = Path(snapshot_dir).expanduser().resolve()
    manifest_path = directory / "snapshot_metadata.json"
    if not manifest_path.is_file():
        raise InfrastructureError("snapshot_manifest_missing", "Snapshot manifest is missing.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = directory / str(manifest.get("source_artifact", ""))
    if not source.is_file():
        raise InfrastructureError("snapshot_artifact_missing", "Snapshot source artifact is missing.")
    if verify_checksum:
        if source.stat().st_size != manifest.get("source_artifact_size_bytes") or sha256_file(source) != manifest.get("source_artifact_sha256"):
            raise InfrastructureError("snapshot_checksum_mismatch", "Snapshot source file size or SHA-256 does not match its manifest.")
    collection = json.loads(source.read_text(encoding="utf-8"))
    return manifest, collection


def audit_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    manifest, collection = load_snapshot(snapshot_dir)
    status = manifest.get("temporal_verification_status")
    if status == "verified_historical":
        evidence = verify_ohsome_temporal_evidence(
            collection,
            manifest.get("source_metadata") or {},
            manifest.get("requested_snapshot_timestamp"),
            manifest.get("event_date"),
            manifest.get("event_cutoff_timestamp"),
        )
        if evidence["status"] != status or evidence["verified_underlying_snapshot_timestamp"] != manifest.get("verified_underlying_snapshot_timestamp"):
            raise InfrastructureError("snapshot_provenance_mismatch", "Manifest historical timestamp does not match the verified source response.")
    elif status == "provider_dated_historical":
        source_rel = Path(str(manifest.get("source_pbf_path", "")))
        if source_rel.is_absolute() or ".." in source_rel.parts or not source_rel.parts:
            raise InfrastructureError("pbf_provenance_path_invalid", "Geofabrik PBF provenance path must be a safe relative path.")
        root = Path(snapshot_dir).expanduser().resolve().parent
        source_pbf = (root / source_rel).resolve()
        try:
            source_pbf.relative_to(root)
        except ValueError as exc:
            raise InfrastructureError("pbf_provenance_path_invalid", "Geofabrik PBF provenance path escapes the configured OSM data directory.") from exc
        if not source_pbf.is_file():
            raise InfrastructureError("pbf_provenance_missing", "The raw Geofabrik PBF referenced by this snapshot is missing.")
        _safe_geofabrik_urls(
            str(manifest.get("source_url", "")),
            str(manifest.get("source_checksum_url", "")),
            str(manifest.get("provider_metadata_url", "")),
            source_pbf.name,
        )
        snapshot_date = _geofabrik_snapshot_date(source_pbf.name)
        cutoff = event_cutoff(manifest.get("event_date", ""))
        temporal = manifest.get("temporal_verification") or {}
        if (
            temporal.get("status") != status
            or temporal.get("provider_md5_verified") is not True
            or temporal.get("provider_snapshot_date") != snapshot_date.isoformat()
            or temporal.get("date_precision") != "calendar_day_from_Geofabrik_filename"
            or manifest.get("provider_snapshot_date") != snapshot_date.isoformat()
            or temporal.get("event_cutoff_date") != cutoff.date().isoformat()
            or temporal.get("pre_event") is not True
            or temporal.get("source_page_url") != manifest.get("provider_metadata_url")
            or temporal.get("source_file_url") != manifest.get("source_url")
            or snapshot_date >= cutoff.date()
            or manifest.get("license") != "Open Database License (ODbL) 1.0"
            or manifest.get("attribution") != OSM_ATTRIBUTION
            or manifest.get("source_pbf_size_bytes") != source_pbf.stat().st_size
            or not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("source_pbf_md5", "")))
            or _md5_file(source_pbf) != manifest.get("source_pbf_md5")
            or sha256_file(source_pbf) != manifest.get("source_pbf_sha256")
        ):
            raise InfrastructureError("pbf_provenance_mismatch", "Geofabrik source date, checksum, size, or pre-event metadata does not match the manifest.")
    elif status not in {"current_snapshot", "historical_unverified", "invalid"}:
        raise InfrastructureError("invalid_temporal_status", "Manifest has an unknown temporal verification status.")
    return {"snapshot_id": manifest.get("snapshot_id"), "temporal_verification_status": status, "feature_count": len(collection.get("features", [])), "artifact_size_bytes": manifest.get("source_artifact_size_bytes"), "sha256_verified": True, "source_pbf_integrity_verified": status == "provider_dated_historical", "historical_use_allowed": status in {"verified_historical", "provider_dated_historical"}, "attribution": manifest.get("attribution"), "provider_snapshot_date": manifest.get("provider_snapshot_date"), "temporal_precision": (manifest.get("temporal_verification") or {}).get("date_precision")}


def _transform_geometry(geometry, source_crs: str | CRS, target_crs: str | CRS):
    source = CRS.from_user_input(source_crs)
    target = CRS.from_user_input(target_crs)
    if source == target:
        return geometry
    transformed = transform_geom(source, target, mapping(geometry), precision=-1)
    return shape(transformed)


def _unescape_ohsome_tag_key(key: str) -> str:
    return key[1:] if key.startswith("@@") else key


def _extract_tags(properties: dict[str, Any]) -> dict[str, Any]:
    nested = properties.get("tags")
    if isinstance(nested, dict):
        return dict(nested)
    return {_unescape_ohsome_tag_key(k): v for k, v in properties.items() if not str(k).startswith("@") or str(k).startswith("@@")}


def categorize_tags(tags: dict[str, Any]) -> list[str]:
    """Apply explicit tag rules; geometry alone never infers feature purpose."""
    categories: list[str] = []
    if tags.get("highway") not in (None, "", "no"):
        categories.append("roads")
    if tags.get("building") not in (None, "", "no"):
        categories.append("buildings")
    if tags.get("bridge") not in (None, "", "no", "false", "0") or tags.get("man_made") == "bridge":
        categories.append("bridges")
    if tags.get("amenity") in {"hospital", "clinic", "doctors"} or tags.get("healthcare") in {"hospital", "clinic", "doctor", "health_post"}:
        categories.append("healthcare")
    if tags.get("emergency") in {"fire_station", "ambulance_station", "police"}:
        categories.append("critical")
    if tags.get("railway") == "station":
        categories.append("railway")
    if tags.get("place") in {"city", "town", "village", "hamlet", "isolated_dwelling"}:
        categories.append("settlements")
    return categories


def _normalize_feature(feature: dict[str, Any], snapshot_id: str, temporal_status: str) -> dict[str, Any] | None:
    geometry_obj = feature.get("geometry")
    if not geometry_obj:
        return None
    try:
        geometry = shape(geometry_obj)
    except Exception as exc:
        raise InfrastructureError("invalid_geometry", "An OSM feature has invalid GeoJSON geometry.") from exc
    if geometry.is_empty:
        return None
    props = feature.get("properties") or {}
    tags = _extract_tags(props)
    categories = categorize_tags(tags)
    osm_id = props.get("@osmId") or props.get("osm_id")
    osm_type = props.get("@osmType") or props.get("osm_type")
    if not osm_id:
        raise InfrastructureError("osm_id_missing", "Every infrastructure feature must retain its OSM element ID.")
    match = re.fullmatch(r"(node|way|relation)/(\d+)", str(osm_id))
    if not match:
        raise InfrastructureError("osm_id_invalid", "OSM IDs must be namespaced as node/<id>, way/<id>, or relation/<id>.")
    osm_type = osm_type or match.group(1)
    return {
        "type": "Feature",
        "id": str(osm_id),
        "geometry": geometry,
        "properties": {
            "osm_id": str(osm_id),
            "osm_type": str(osm_type),
            "osm_version": props.get("@version", props.get("osm_version")),
            "osm_changeset_id": props.get("@changesetId", props.get("osm_changeset_id")),
            "osm_last_edit": props.get("@lastEdit", props.get("osm_last_edit")),
            "osm_snapshot_timestamp": props.get("@snapshotTimestamp", props.get("osm_snapshot_timestamp")),
            "tags": tags,
            "categories": categories,
            "snapshot_id": snapshot_id,
            "temporal_verification_status": temporal_status,
        },
    }


def _utm_crs_for_bbox(bounds: Iterable[float]) -> CRS:
    west, south, east, north = validate_bbox(bounds)
    longitude, latitude = (west + east) / 2, (south + north) / 2
    if not (-80 <= latitude <= 84):
        raise InfrastructureError("metric_crs_required", "Automatic UTM measurement CRS selection only supports latitudes between 80°S and 84°N; supply a suitable projected meter CRS.")
    zone = min(60, max(1, int((longitude + 180) // 6) + 1))
    return CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone)


def prepare_infrastructure(
    *, snapshot_dir: str | Path, output_dir: str | Path,
    aoi_bbox_wgs84: Iterable[float] | None = None,
    measurement_crs: str | None = None,
) -> dict[str, Any]:
    """Normalize tags/IDs, repair valid geometry, clip AOI, audit and write GeoJSON."""
    manifest, collection = load_snapshot(snapshot_dir)
    source_crs = manifest.get("source_crs")
    if not source_crs:
        raise InfrastructureError("source_crs_missing", "Snapshot manifest has no source CRS; refusing to guess or assign one.")
    try:
        CRS.from_user_input(source_crs)
    except Exception as exc:
        raise InfrastructureError("source_crs_invalid", "Snapshot manifest source CRS is invalid.") from exc
    snapshot_aoi = (manifest.get("aoi") or {}).get("bbox_wgs84")
    selected_bbox = validate_bbox(aoi_bbox_wgs84 if aoi_bbox_wgs84 is not None else snapshot_aoi) if (aoi_bbox_wgs84 is not None or snapshot_aoi is not None) else None
    if selected_bbox is None:
        raise InfrastructureError("aoi_missing", "An AOI bbox in WGS84 is required for infrastructure preparation.")
    if snapshot_aoi is not None and tuple(float(v) for v in snapshot_aoi) != selected_bbox:
        raise InfrastructureError("aoi_mismatch", "Preparation AOI must match the OSM snapshot AOI; acquire/import a matching extract.")
    aoi = bbox_geometry(selected_bbox)
    metric_crs = CRS.from_user_input(measurement_crs) if measurement_crs else _utm_crs_for_bbox(selected_bbox)
    if not metric_crs.is_projected or metric_crs.linear_units_factor[1] != 1.0:
        raise InfrastructureError("metric_crs_invalid", "measurement_crs must be projected with meter units.")
    snapshot_id = str(manifest.get("snapshot_id"))
    temporal_status = str(manifest.get("temporal_verification_status", "invalid"))
    if temporal_status == "provider_dated_historical":
        audit_snapshot(snapshot_dir)
    output_features: list[dict[str, Any]] = []
    seen: set[str] = set()
    repairs = 0
    invalid = 0
    outside = 0
    unclassified = 0
    for raw in collection["features"]:
        item = _normalize_feature(raw, snapshot_id, temporal_status)
        if item is None:
            invalid += 1
            continue
        osm_id = item["properties"]["osm_id"]
        if osm_id in seen:
            continue
        seen.add(osm_id)
        geom = item["geometry"]
        if not geom.is_valid:
            geom = make_valid(geom)
            repairs += 1
        if geom.is_empty or not geom.is_valid:
            invalid += 1
            continue
        geom_wgs84 = _transform_geometry(geom, source_crs, "EPSG:4326")
        if not geom_wgs84.intersects(aoi):
            outside += 1
            continue
        clipped = geom_wgs84.intersection(aoi)
        if clipped.is_empty:
            outside += 1
            continue
        if not item["properties"]["categories"]:
            unclassified += 1
        metric_geom = _transform_geometry(clipped, "EPSG:4326", metric_crs)
        props = item["properties"]
        if "roads" in props["categories"] and metric_geom.geom_type in {"LineString", "MultiLineString"}:
            props["length_m"] = float(metric_geom.length)
            props["road_class"] = props["tags"].get("highway")
        if "buildings" in props["categories"] and metric_geom.geom_type in {"Polygon", "MultiPolygon"}:
            props["footprint_area_m2"] = float(metric_geom.area)
        item["geometry"] = clipped
        output_features.append(item)
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=False)
    prepared_geojson_path = output_path / "infrastructure_prepared.geojson"
    features_json = [
        {"type": "Feature", "id": f["id"], "geometry": mapping(f["geometry"]), "properties": f["properties"]}
        for f in output_features
    ]
    prepared_collection = {"type": "FeatureCollection", "features": features_json}
    _write_json_atomic(prepared_geojson_path, prepared_collection)
    audit = {
        "schema_version": PREPARED_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "event_id": manifest.get("event_id"),
        "event_date": manifest.get("event_date"),
        "temporal_verification_status": temporal_status,
        "aoi_bbox_wgs84": list(selected_bbox),
        "source_crs": CRS.from_user_input(source_crs).to_string(),
        "output_geometry_crs": "EPSG:4326",
        "measurement_crs": metric_crs.to_string(),
        "input_feature_count": len(collection["features"]),
        "prepared_feature_count": len(output_features),
        "excluded_outside_aoi_count": outside,
        "excluded_invalid_or_empty_count": invalid,
        "geometry_repair_count": repairs,
        "unclassified_feature_count": unclassified,
        "feature_counts_by_category": {name: sum(name in f["properties"]["categories"] for f in output_features) for name in sorted({c for f in output_features for c in f["properties"]["categories"]})},
        "output_file": prepared_geojson_path.name,
        "output_size_bytes": prepared_geojson_path.stat().st_size,
        "output_sha256": sha256_file(prepared_geojson_path),
        "attribution": manifest.get("attribution", OSM_ATTRIBUTION),
        "attribution_url": manifest.get("attribution_url", OSM_COPYRIGHT_URL),
        "limitations": ["A timestamp does not prove OSM completeness or physical existence of infrastructure.", "GeoJSON output coordinates are EPSG:4326; metric measurements were computed in the recorded projected CRS."],
    }
    _write_json_atomic(output_path / "processing_metadata.json", audit)
    return audit


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfrastructureError("manifest_unreadable", f"Could not read JSON manifest: {path}") from exc
    if not isinstance(value, dict):
        raise InfrastructureError("manifest_invalid", "Manifest root must be a JSON object.")
    return value


def validate_flood_mask(mask_manifest_path: str | Path, *, event_id: str, event_date: str, aoi_bbox_wgs84: Iterable[float] | None = None) -> dict[str, Any]:
    """Fail-closed readiness contract for a real, approved inference mask."""
    manifest_path = Path(mask_manifest_path).expanduser().resolve()
    manifest = _read_json(manifest_path)
    def reject(message: str, code: str = "flood_mask_not_ready"):
        raise InfrastructureError(code, message)
    if manifest.get("synthetic") is not False or manifest.get("artifact_type") != "flood_mask" or manifest.get("source_type") != "model_inference":
        reject("Flood mask must be a real model-inference artifact; synthetic, placeholder, and derived test fixtures are rejected.")
    readiness = manifest.get("analysis_readiness") or {}
    if readiness.get("status") != "approved" or not readiness.get("acceptance_policy_id") or not readiness.get("independent_validation_id"):
        reject("Model evaluation has not approved this mask under a recorded acceptance policy and independent validation record.")
    report_relative = readiness.get("independent_validation_report_path")
    report_sha256 = readiness.get("independent_validation_report_sha256")
    if not isinstance(report_relative, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", str(report_sha256 or "")):
        reject("Approved masks must reference a checksummed independent validation report.", "independent_validation_missing")
    report_path = (manifest_path.parent / report_relative).resolve()
    if not report_path.is_relative_to(manifest_path.parent):
        reject("Independent validation report must be stored beside the mask manifest.", "independent_validation_path_invalid")
    if not report_path.is_file() or sha256_file(report_path) != report_sha256.lower():
        reject("Independent validation report is missing or its checksum does not match.", "independent_validation_checksum_mismatch")
    validation_report = _read_json(report_path)
    if (
        validation_report.get("schema_version") != "jaldrishti.independent-mask-validation.v1"
        or validation_report.get("validation_status") != "passed"
        or validation_report.get("validation_id") != readiness.get("independent_validation_id")
        or validation_report.get("event_id") != manifest.get("event_id")
        or validation_report.get("event_date") != manifest.get("event_date")
        or validation_report.get("prediction_mask_sha256") != manifest.get("artifact_sha256")
        or validation_report.get("aoi_bbox_wgs84") != manifest.get("aoi_bbox_wgs84")
    ):
        reject("Independent validation report does not approve this exact mask and event.", "independent_validation_mismatch")
    reference = validation_report.get("reference") or {}
    reference_url = str(reference.get("source_url", ""))
    parsed_reference_url = urlsplit(reference_url)
    if (
        reference.get("use") != "validation_only"
        or reference.get("used_as_prediction_input") is not False
        or reference.get("independent_of_training_data") is not True
        or not str(reference.get("name", "")).strip()
        or not str(reference.get("license", "")).strip()
        or parsed_reference_url.scheme != "https"
        or not parsed_reference_url.netloc
        or parsed_reference_url.username
        or parsed_reference_url.password
        or parsed_reference_url.query
        or not re.fullmatch(r"[0-9a-fA-F]{64}", str(reference.get("sha256", "")))
    ):
        reject("Independent reference provenance is incomplete or not marked validation-only.", "independent_reference_provenance_invalid")
    relative_reference_path = Path(str(reference.get("path", "")))
    reference_path = None
    project_root = None
    if not relative_reference_path.is_absolute() and ".." not in relative_reference_path.parts:
        for project_candidate in manifest_path.parents:
            validation_root = (project_candidate / "data/validation").resolve()
            candidate_path = (project_candidate / relative_reference_path).resolve()
            if candidate_path.is_relative_to(validation_root) and candidate_path.is_file():
                reference_path = candidate_path
                project_root = project_candidate.resolve()
                break
    if reference_path is None or sha256_file(reference_path) != reference["sha256"].lower():
        reject("Validation reference must be preserved under data/validation and match its recorded SHA-256.", "independent_reference_missing")
    alignment_relative = Path(str(reference.get("alignment_sidecar_path", "")))
    if project_root is None or alignment_relative.is_absolute() or ".." in alignment_relative.parts:
        reject("Independent validation reference must include a safe alignment-sidecar path.", "independent_reference_alignment_missing")
    alignment_path = (project_root / alignment_relative).resolve()
    validation_root = (project_root / "data/validation").resolve()
    if (
        not alignment_path.is_relative_to(validation_root)
        or not alignment_path.is_file()
        or sha256_file(alignment_path) != str(reference.get("alignment_sidecar_sha256", "")).lower()
    ):
        reject("Reference alignment sidecar is missing or its checksum does not match.", "independent_reference_alignment_invalid")
    alignment = _read_json(alignment_path)
    if (
        alignment.get("schema_version") != "jaldrishti.reference-alignment.v1"
        or alignment.get("aligned_reference_path") != relative_reference_path.as_posix()
        or alignment.get("aligned_reference_sha256") != reference.get("sha256")
        or alignment.get("target_prediction_mask_sha256") != manifest.get("artifact_sha256")
        or alignment.get("resampling") != "nearest"
        or alignment.get("use") != "validation_only"
        or alignment.get("source_reference_path") != reference.get("original_source_path")
        or alignment.get("source_reference_sha256") != reference.get("original_source_sha256")
    ):
        reject("Reference alignment sidecar does not bind the original, aligned reference, and exact prediction mask.", "independent_reference_alignment_mismatch")
    original_relative = Path(str(reference.get("original_source_path", "")))
    if original_relative.is_absolute() or ".." in original_relative.parts:
        reject("Original independent reference path is unsafe.", "independent_reference_alignment_invalid")
    original_path = (project_root / original_relative).resolve()
    if (
        not original_path.is_relative_to(validation_root)
        or not original_path.is_file()
        or sha256_file(original_path) != str(reference.get("original_source_sha256", "")).lower()
    ):
        reject("Original independent reference is missing or its checksum does not match.", "independent_reference_alignment_invalid")
    reviewer = validation_report.get("review") or {}
    if not str(reviewer.get("reviewed_by", "")).strip() or not str(reviewer.get("reviewed_at", "")).strip():
        reject("An approved independent validation report must identify its reviewer and review time.", "independent_validation_review_missing")
    metrics = validation_report.get("metrics") or {}
    for metric_name in ("flood_iou", "flood_f1", "flood_precision", "flood_recall", "valid_pixel_coverage"):
        value = metrics.get(metric_name)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            reject("Independent validation report has a missing or invalid metric: " + metric_name, "independent_validation_metrics_invalid")
    if str(manifest.get("event_id")) != str(event_id) or str(manifest.get("event_date")) != str(event_date):
        reject("Flood-mask event ID/date do not match the requested infrastructure event.", "event_mismatch")
    if not manifest.get("inference_run_id") or not re.fullmatch(r"[0-9a-fA-F]{64}", str(manifest.get("model_checkpoint_sha256", ""))):
        reject("Flood mask must identify an inference run and SHA-256 model checkpoint.")
    required = ("preprocessing_version", "mask_semantics", "width", "height", "transform", "crs", "nodata", "valid_data_fraction", "aoi_bbox_wgs84", "artifact_sha256", "artifact_path")
    missing = [name for name in required if manifest.get(name) is None]
    if missing:
        reject("Flood-mask manifest is missing required fields: " + ", ".join(missing))
    bounds = validate_bbox(manifest["aoi_bbox_wgs84"])
    if aoi_bbox_wgs84 is not None and bounds != validate_bbox(aoi_bbox_wgs84):
        reject("Flood-mask AOI does not match the requested AOI.", "aoi_mismatch")
    semantics = manifest["mask_semantics"]
    try:
        flood_value = int(semantics["flood_value"])
        non_flood_value = int(semantics["non_flood_value"])
        nodata_value = int(semantics["nodata_value"])
    except (KeyError, TypeError, ValueError) as exc:
        reject("Mask semantics must specify integer flood, non-flood, and NoData class values.")
    if len({flood_value, non_flood_value, nodata_value}) != 3 or nodata_value != int(manifest["nodata"]):
        reject("Flood, non-flood, and NoData values must be distinct and match raster metadata.")
    artifact = Path(str(manifest["artifact_path"]))
    if not artifact.is_absolute():
        artifact = (manifest_path.parent / artifact).resolve()
    if not artifact.is_file():
        reject("Flood-mask raster artifact does not exist.", "flood_mask_missing")
    if sha256_file(artifact) != manifest["artifact_sha256"]:
        reject("Flood-mask raster SHA-256 does not match its manifest.", "flood_mask_checksum_mismatch")
    try:
        with rasterio.open(artifact) as src:
            if src.count != 1 or src.crs is None:
                reject("Flood-mask raster must have one band and a defined CRS.", "flood_mask_georeferencing_invalid")
            if src.width != int(manifest["width"]) or src.height != int(manifest["height"]):
                reject("Flood-mask raster dimensions do not match its manifest.", "flood_mask_metadata_mismatch")
            if tuple(float(v) for v in src.transform)[:6] != tuple(float(v) for v in manifest["transform"][:6]):
                reject("Flood-mask raster affine transform does not match its manifest.", "flood_mask_metadata_mismatch")
            if src.crs != CRS.from_user_input(manifest["crs"]):
                reject("Flood-mask raster CRS does not match its manifest.", "flood_mask_metadata_mismatch")
            if src.nodata is None or not float(src.nodata).is_integer() or int(src.nodata) != nodata_value:
                reject("Flood-mask raster NoData value is missing or inconsistent.", "flood_mask_nodata_invalid")
            raster_bounds_wgs84 = shape(transform_geom(src.crs, "EPSG:4326", mapping(box(*src.bounds)), precision=-1))
            if not raster_bounds_wgs84.covers(bbox_geometry(bounds)):
                reject("Flood-mask raster does not cover the requested AOI.", "flood_mask_coverage_invalid")
            observed: set[int] = set()
            valid_pixels = 0
            total_pixels = src.width * src.height
            for _, window in src.block_windows(1):
                array = src.read(1, window=window)
                valid = src.read_masks(1, window=window) > 0
                valid_pixels += int(valid.sum())
                if valid.any():
                    observed.update(int(v) for v in set(array[valid].tolist()))
            allowed = {flood_value, non_flood_value}
            if not observed or not observed.issubset(allowed):
                reject("Flood-mask raster contains unknown classes or has no valid classified pixels.", "flood_mask_encoding_invalid")
            valid_fraction = valid_pixels / total_pixels
            if not (0 < valid_fraction <= 1) or not math.isclose(valid_fraction, float(manifest["valid_data_fraction"]), rel_tol=1e-4, abs_tol=1e-6):
                reject("Flood-mask valid-data fraction is absent or inconsistent with the raster.", "flood_mask_coverage_invalid")
            raster_info = {"crs": src.crs.to_string(), "width": src.width, "height": src.height, "transform": list(src.transform)[:6], "bounds": [src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top], "nodata": src.nodata, "observed_classes": sorted(observed), "valid_data_fraction": valid_fraction}
    except rasterio.errors.RasterioError as exc:
        reject("Flood-mask raster could not be opened or read.", "flood_mask_unreadable")
    return {"manifest": manifest, "manifest_path": str(manifest_path), "artifact_path": str(artifact), "raster": raster_info, "analysis_ready": True}


def _mask_to_geometry(mask_path: str | Path, flood_value: int, max_pixels: int = 50_000_000):
    with rasterio.open(mask_path) as src:
        if src.width * src.height > max_pixels:
            raise InfrastructureError("mask_too_large", f"Flood mask exceeds the current vectorization limit of {max_pixels:,} pixels.")
        data = src.read(1)
        valid = src.read_masks(1) > 0
        flood = (data == flood_value) & valid
        if not flood.any():
            return None, src.crs
        polygons = [shape(geom) for geom, value in shapes(flood.astype("uint8"), mask=flood, transform=src.transform) if int(value) == 1]
        return unary_union(polygons), src.crs



def measure_feature_exposure(features: Iterable[tuple[str, dict[str, Any], Any]], flood_geometry) -> dict[str, Any]:
    """Measure intersections on already projected geometries.

    This low-level function intentionally has no provenance authority; production
    results are written only by calculate_exposure after its real-data gates.
    """
    seen: dict[str, set[str]] = {k: set() for k in ("roads", "buildings", "healthcare", "critical", "bridges", "railway")}
    roads_total = roads_exposed = 0.0
    roads_by_class: dict[str, dict[str, float]] = {}
    building_exposed = facility_exposed = critical_exposed = bridge_exposed = railway_exposed = 0
    for osm_id, props, geom in features:
        categories = set(props.get("categories", []))
        tags = props.get("tags", {})
        exposed = bool(flood_geometry is not None and geom.intersects(flood_geometry))
        if "roads" in categories and geom.geom_type in {"LineString", "MultiLineString"} and osm_id not in seen["roads"]:
            seen["roads"].add(osm_id)
            length = float(geom.length)
            roads_total += length
            entry = roads_by_class.setdefault(str(tags.get("highway", "unclassified")), {"total_length_m": 0.0, "potentially_exposed_length_m": 0.0})
            entry["total_length_m"] += length
            if flood_geometry is not None:
                exposed_len = float(geom.intersection(flood_geometry).length)
                roads_exposed += exposed_len
                entry["potentially_exposed_length_m"] += exposed_len
        if "buildings" in categories and geom.geom_type in {"Polygon", "MultiPolygon"} and osm_id not in seen["buildings"]:
            seen["buildings"].add(osm_id)
            if flood_geometry is not None and geom.intersection(flood_geometry).area > 0:
                building_exposed += 1
        if "healthcare" in categories and osm_id not in seen["healthcare"]:
            seen["healthcare"].add(osm_id)
            if exposed:
                facility_exposed += 1
        if "critical" in categories and osm_id not in seen["critical"]:
            seen["critical"].add(osm_id)
            if exposed:
                critical_exposed += 1
        if "bridges" in categories and osm_id not in seen["bridges"]:
            seen["bridges"].add(osm_id)
            if exposed:
                bridge_exposed += 1
        if "railway" in categories and osm_id not in seen["railway"]:
            seen["railway"].add(osm_id)
            if exposed:
                railway_exposed += 1
    return {
        "road_length_m": {"total_mapped": roads_total, "potentially_exposed": roads_exposed, "percent_potentially_exposed": 100.0 * roads_exposed / roads_total if roads_total > 0 else None, "by_highway_tag": roads_by_class},
        "buildings": {"total_mapped": len(seen["buildings"]), "potentially_exposed": building_exposed},
        "healthcare_facilities": {"total_mapped": len(seen["healthcare"]), "potentially_exposed": facility_exposed},
        "critical_facilities": {"total_mapped": len(seen["critical"]), "potentially_exposed": critical_exposed},
        "bridges": {"total_mapped": len(seen["bridges"]), "potentially_exposed": bridge_exposed, "counting_rule": "unique OSM element ID; bridge-tagged highway may also contribute to road length, so these metrics are separate subsets and must not be summed"},
        "railway_features": {"total_mapped": len(seen["railway"]), "potentially_exposed": railway_exposed},
    }

def calculate_exposure(
    *, snapshot_dir: str | Path, prepared_dir: str | Path,
    flood_mask_manifest_path: str | Path, event_id: str, event_date: str,
    output_path: str | Path, aoi_bbox_wgs84: Iterable[float] | None = None,
) -> dict[str, Any]:
    """Compute predicted spatial exposure only after both real-data gates pass."""
    snapshot, _ = load_snapshot(snapshot_dir)
    snapshot_audit = audit_snapshot(snapshot_dir)
    if not snapshot_audit.get("historical_use_allowed"):
        raise InfrastructureError("historical_snapshot_required", "Production exposure requires an auditable pre-event OSM snapshot (ohsome verified history or a checksum-verified provider-dated extract).")
    if snapshot.get("event_id") != event_id or snapshot.get("event_date") != event_date:
        raise InfrastructureError("event_mismatch", "OSM snapshot event ID/date do not match the requested event.")
    prepared = _read_json(Path(prepared_dir) / "processing_metadata.json")
    if prepared.get("snapshot_id") != snapshot.get("snapshot_id") or prepared.get("temporal_verification_status") != snapshot.get("temporal_verification_status"):
        raise InfrastructureError("prepared_snapshot_mismatch", "Prepared infrastructure is not tied to the audited requested snapshot.")
    bbox = validate_bbox(aoi_bbox_wgs84 if aoi_bbox_wgs84 is not None else prepared.get("aoi_bbox_wgs84", []))
    if tuple(prepared.get("aoi_bbox_wgs84", [])) != bbox:
        raise InfrastructureError("aoi_mismatch", "Prepared infrastructure AOI does not match exposure AOI.")
    verified_mask = validate_flood_mask(flood_mask_manifest_path, event_id=event_id, event_date=event_date, aoi_bbox_wgs84=bbox)
    mask_manifest = verified_mask["manifest"]
    if tuple(snapshot.get("aoi", {}).get("bbox_wgs84", [])) != bbox:
        raise InfrastructureError("aoi_mismatch", "OSM snapshot AOI does not match exposure AOI.")
    mask_geometry, mask_crs = _mask_to_geometry(verified_mask["artifact_path"], int(mask_manifest["mask_semantics"]["flood_value"]))
    if mask_geometry is None:
        flood_wgs84 = None
        metric_crs = _utm_crs_for_bbox(bbox)
    else:
        if mask_crs is None:
            raise InfrastructureError("flood_mask_georeferencing_invalid", "Flood mask has no CRS.")
        try:
            flood_wgs84 = _transform_geometry(mask_geometry, mask_crs, "EPSG:4326")
        except Exception as exc:
            raise InfrastructureError("flood_mask_georeferencing_invalid", "Could not reproject flood mask geometry to WGS84.") from exc
        try:
            metric_crs = _utm_crs_for_bbox(bbox)
        except InfrastructureError:
            metric_crs = CRS.from_user_input(mask_crs)
            if not metric_crs.is_projected or metric_crs.linear_units_factor[1] != 1.0:
                raise
    prepared_geojson = Path(prepared_dir) / str(prepared.get("output_file", "infrastructure_prepared.geojson"))
    if not prepared_geojson.is_file() or sha256_file(prepared_geojson) != prepared.get("output_sha256"):
        raise InfrastructureError("prepared_data_checksum_mismatch", "Prepared infrastructure GeoJSON is missing or its checksum does not match.")
    collection = _read_json(prepared_geojson)
    features = []
    for raw in collection.get("features", []):
        try:
            geom = shape(raw["geometry"])
        except Exception as exc:
            raise InfrastructureError("prepared_geometry_invalid", "Prepared infrastructure contains invalid geometry.") from exc
        features.append((str(raw.get("id") or raw.get("properties", {}).get("osm_id")), raw.get("properties", {}), _transform_geometry(geom, "EPSG:4326", metric_crs)))
    flood_metric = _transform_geometry(flood_wgs84, "EPSG:4326", metric_crs) if flood_wgs84 is not None else None
    metrics = measure_feature_exposure(features, flood_metric)
    result = {
        "schema_version": "jaldrishti.infrastructure-exposure.v1",
        "status": "completed",
        "result_label": "predicted spatial exposure; not confirmed damage, closure, service disruption, or access loss",
        "event_id": event_id,
        "event_date": event_date,
        "aoi_bbox_wgs84": list(bbox),
        "osm_snapshot_id": snapshot["snapshot_id"],
        "osm_snapshot_timestamp": snapshot.get("verified_underlying_snapshot_timestamp"),
        "osm_temporal_verification_status": snapshot["temporal_verification_status"],
        "osm_provider_snapshot_date": snapshot.get("provider_snapshot_date"),
        "osm_temporal_precision": (snapshot.get("temporal_verification") or {}).get("date_precision", "exact_timestamp" if snapshot.get("verified_underlying_snapshot_timestamp") else None),
        "flood_mask_id": mask_manifest.get("inference_run_id"),
        "flood_mask_validation_status": (mask_manifest.get("analysis_readiness") or {}).get("status"),
        "flood_mask_sha256": mask_manifest.get("artifact_sha256"),
        "metric_crs": metric_crs.to_string(),
        "road_length_m": metrics["road_length_m"],
        "buildings": metrics["buildings"],
        "healthcare_facilities": metrics["healthcare_facilities"],
        "critical_facilities": metrics["critical_facilities"],
        "bridges": metrics["bridges"],
        "railway_features": metrics["railway_features"],
        "attribution": OSM_ATTRIBUTION,
        "attribution_url": OSM_COPYRIGHT_URL,
        "limitations": ["OSM is incomplete and may not represent all infrastructure or physical existence at event time.", "Spatial intersection is predicted exposure only; it does not establish damage, closure, or operational status.", "Nodata is excluded by the validated mask contract; no missing mask is treated as zero exposure."],
    }
    _write_json_atomic(Path(output_path).expanduser().resolve(), result)
    return result
