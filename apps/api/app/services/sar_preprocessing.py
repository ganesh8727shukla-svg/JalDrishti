from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable
import uuid
import zipfile
from xml.etree import ElementTree

import httpx
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom
from shapely.geometry import box, mapping, shape

from ..core.config import PROJECT_ROOT, Settings
from ..schemas.observations import ObservationMetadata
from ..schemas.preprocessing import SARPreprocessingRequest
from ..services.product_downloads import SAFE_ATTRIBUTION, STAC_ITEM_URL
from ..services.satellite_catalogue import COLLECTION_ID, CatalogueSearchError, _normalize_feature
from .scene_matching import rank_observation_pairs


SNAP_TIMEOUT_SECONDS = 6 * 60 * 60
STAC_TIMEOUT_SECONDS = 20.0
MODEL_INPUT_BLOCKED = "blocked_unknown_model_contract"
SNAP_OUTPUT_BAND_ORDER = ("Sigma0_VV", "Sigma0_VH")
SNAP_BAND_ORDER_PROVENANCE = (
    "Explicit SNAP BandSelect sourceBands order; missing named bands cause SNAP to fail"
)
WORLDDEM30_MODIFIED_ATTRIBUTION = (
    "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
    "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved"
)
WORLDDEM30_SOURCE_ATTRIBUTION = (
    "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved"
)


class SARPreprocessingError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class SARPreprocessingService:
    """Local asynchronous SNAP preprocessing for an explicitly selected pair."""

    _manifest_lock = threading.RLock()

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        snap_executable: str | None = None,
        metadata_fetcher: Callable[[str], tuple[dict[str, Any], ObservationMetadata]] | None = None,
        snap_runner: Callable[[list[str], Path, int], None] | None = None,
    ) -> None:
        self.settings = settings or Settings.from_environment()
        self.transport = transport
        self.metadata_fetcher = metadata_fetcher or self._fetch_item
        self.snap_runner = snap_runner or self._run_snap
        configured_processed = Path(os.getenv("JALDRISHTI_PROCESSED_DIR", "data/processed/sentinel1")).expanduser()
        self.processed_dir = configured_processed if configured_processed.is_absolute() else PROJECT_ROOT / configured_processed
        self.manifest_dir = PROJECT_ROOT / "data/manifests/sentinel1_preprocessing"
        self.snap_executable = self._resolve_snap(snap_executable)
        self.timeout_seconds = self._read_timeout()
        try:
            self._recover_interrupted_jobs()
        except SARPreprocessingError:
            pass

    @staticmethod
    def _resolve_snap(injected: str | None) -> str | None:
        if injected:
            candidate = Path(injected).expanduser()
            return str(candidate.resolve()) if candidate.exists() else injected
        configured = os.getenv("JALDRISHTI_SNAP_GPT", "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
            resolved_command = shutil.which(configured)
            if resolved_command and "snap" in Path(resolved_command).resolve().as_posix().lower():
                return str(Path(resolved_command).resolve())
            return None
        snap_home = os.getenv("SNAP_HOME")
        candidates = [
            Path(snap_home).expanduser() / "bin/gpt" if snap_home else None,
            Path("/Applications/esa-snap/bin/gpt"),
            Path("/Applications/snap/bin/gpt"),
            Path.home() / "Applications/esa-snap/bin/gpt",
            Path.home() / "Applications/snap/bin/gpt",
        ]
        for candidate in candidates:
            if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        path_candidate = shutil.which("gpt")
        if path_candidate and "snap" in Path(path_candidate).resolve().as_posix().lower():
            return str(Path(path_candidate).resolve())
        return None

    @staticmethod
    def _read_timeout() -> int:
        raw = os.getenv("JALDRISHTI_SNAP_TIMEOUT_SECONDS", str(SNAP_TIMEOUT_SECONDS))
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("JALDRISHTI_SNAP_TIMEOUT_SECONDS must be a positive integer") from exc
        if value < 1 or value > 24 * 60 * 60:
            raise ValueError("JALDRISHTI_SNAP_TIMEOUT_SECONDS must be between 1 and 86400")
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def jobs_manifest_path(self) -> Path:
        return self.manifest_dir / "manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        path = self.jobs_manifest_path
        if not path.exists():
            return {"schema_version": 1, "jobs": []}
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SARPreprocessingError("manifest_unreadable", "The SAR processing manifest is unreadable and was left unchanged.", 500) from exc
        if not isinstance(body, dict) or body.get("schema_version") != 1 or not isinstance(body.get("jobs"), list):
            raise SARPreprocessingError("manifest_invalid", "The SAR processing manifest has an unsupported structure and was left unchanged.", 500)
        return body

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".manifest-", suffix=".tmp", dir=self.manifest_dir, delete=False) as target:
                temporary_name = target.name
                json.dump(manifest, target, indent=2, ensure_ascii=False, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.jobs_manifest_path)
            os.chmod(self.jobs_manifest_path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise SARPreprocessingError("manifest_write_failed", "The SAR processing manifest could not be written safely.", 500) from exc

    def _recover_interrupted_jobs(self) -> None:
        if not self.jobs_manifest_path.exists():
            return
        with self._manifest_lock:
            manifest = self._load_manifest()
            changed = False
            for job in manifest["jobs"]:
                if isinstance(job, dict) and job.get("status") == "running":
                    job.update({
                        "status": "failed",
                        "stage": "interrupted",
                        "updated_at": self._now(),
                        "error": {"code": "worker_interrupted", "message": "The API process stopped during SNAP processing. Submit the same explicit pair again to retry."},
                    })
                    changed = True
            if changed:
                self._save_manifest(manifest)

    def _update(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._manifest_lock:
            manifest = self._load_manifest()
            job = next((item for item in manifest["jobs"] if item.get("job_id") == job_id), None)
            if job is None:
                raise SARPreprocessingError("job_not_found", "The SAR preprocessing job was not found.", 404)
            job.update(changes)
            job["updated_at"] = self._now()
            self._save_manifest(manifest)
            return dict(job)

    def get_status(self, job_id: str) -> dict[str, Any]:
        with self._manifest_lock:
            manifest = self._load_manifest()
            job = next((item for item in manifest["jobs"] if item.get("job_id") == job_id), None)
            if job is None:
                raise SARPreprocessingError("job_not_found", "The SAR preprocessing job was not found.", 404)
            return dict(job)

    def _fetch_item(self, scene_id: str) -> tuple[dict[str, Any], ObservationMetadata]:
        try:
            with httpx.Client(transport=self.transport, timeout=STAC_TIMEOUT_SECONDS) as client:
                response = client.get(
                    f"{STAC_ITEM_URL}/{scene_id}",
                    headers={"Accept": "application/geo+json, application/json"},
                )
        except httpx.TimeoutException as exc:
            raise SARPreprocessingError("catalogue_timeout", "The CDSE item metadata request timed out.", 503) from exc
        except httpx.RequestError as exc:
            raise SARPreprocessingError("catalogue_network_error", "Could not connect to CDSE to verify the selected pair.", 503) from exc
        if response.status_code == 404:
            raise SARPreprocessingError("product_not_found", "A selected Sentinel-1 product is no longer present in the CDSE catalogue.", 409)
        if response.status_code in (401, 403):
            raise SARPreprocessingError("catalogue_access_denied", "CDSE denied access to selected product metadata.", 503)
        if response.status_code == 429 or response.status_code >= 500:
            raise SARPreprocessingError("catalogue_unavailable", "CDSE could not verify the selected products; retry later.", 503)
        if not 200 <= response.status_code < 300:
            raise SARPreprocessingError("catalogue_request_rejected", "CDSE rejected the selected product metadata request.", 502)
        try:
            item = response.json()
        except ValueError as exc:
            raise SARPreprocessingError("invalid_catalogue_response", "CDSE returned non-JSON product metadata.", 502) from exc
        if not isinstance(item, dict) or item.get("id") != scene_id or item.get("collection") != COLLECTION_ID:
            raise SARPreprocessingError("invalid_catalogue_response", "CDSE returned a different item or collection than the selected product.", 502)
        try:
            metadata = _normalize_feature(item, datetime.now(timezone.utc))
        except CatalogueSearchError as exc:
            raise SARPreprocessingError(exc.code, exc.message, 502) from exc
        return item, metadata

    @staticmethod
    def _safe_product_path(data_root: Path, entry: dict[str, Any]) -> Path:
        if entry.get("status") != "geospatial_inspected":
            raise SARPreprocessingError("source_not_validated", "Both selected downloads must have status geospatial_inspected.", 409)
        validation = entry.get("validation")
        integrity = validation.get("integrity") if isinstance(validation, dict) else None
        if not isinstance(integrity, dict) or integrity.get("integrity") != "valid":
            raise SARPreprocessingError("source_integrity_not_validated", "Both selected downloads must pass archive integrity validation.", 409)
        relative = entry.get("file_path")
        file_name = entry.get("file_name")
        if (
            not isinstance(relative, str) or not isinstance(file_name, str)
            or relative != f"products/{file_name}" or Path(relative).is_absolute()
            or "\\" in relative or ":" in relative or ".." in Path(relative).parts
        ):
            raise SARPreprocessingError("unsafe_product_path", "A source archive path is invalid; no file was opened.", 409)
        try:
            root = data_root.resolve(strict=True)
            candidate = data_root / relative
            if candidate.is_symlink():
                raise SARPreprocessingError("unsafe_product_path", "A source archive is a symbolic link; no file was opened.", 409)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            actual_size = resolved.stat().st_size
        except SARPreprocessingError:
            raise
        except (OSError, ValueError) as exc:
            raise SARPreprocessingError("source_product_missing", "A previously downloaded source archive is unavailable.", 409) from exc
        expected_size = entry.get("expected_size_bytes")
        if not resolved.is_file() or actual_size <= 0 or actual_size != expected_size or actual_size != entry.get("file_size_bytes"):
            raise SARPreprocessingError("source_size_mismatch", "A source archive no longer matches its validated size record.", 409)
        try:
            is_zip = zipfile.is_zipfile(resolved)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise SARPreprocessingError("source_archive_invalid", "A selected SAFE archive cannot be reopened; repeat download validation.", 409) from exc
        if not is_zip:
            raise SARPreprocessingError("source_archive_invalid", "A selected source file is not a readable SAFE ZIP archive.", 409)
        return resolved

    @staticmethod
    def _inspect_source_processing(path: Path) -> dict[str, bool]:
        flags: dict[str, list[bool]] = {"VV": [], "VH": []}
        try:
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise SARPreprocessingError("source_archive_invalid", "A selected SAFE archive failed its ZIP CRC check.", 409)
                annotation_names = [
                    name for name in archive.namelist()
                    if name.lower().endswith(".xml")
                    and len(name.lower().split("/")) >= 3
                    and name.lower().split("/")[-2] == "annotation"
                ]
                for name in annotation_names:
                    normalized_name = name.lower()
                    polarization = "VV" if re.search(r"(?:^|[-_])vv(?:[-_.]|$)", normalized_name) else (
                        "VH" if re.search(r"(?:^|[-_])vh(?:[-_.]|$)", normalized_name) else None
                    )
                    if polarization is None:
                        continue
                    root = ElementTree.fromstring(archive.read(name))
                    value = next(
                        (element.text for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "thermalNoiseCorrectionPerformed"),
                        None,
                    )
                    if value is None or value.strip().lower() not in {"true", "false"}:
                        raise SARPreprocessingError("thermal_noise_state_unavailable", "SAFE annotation does not provide a valid thermal-noise correction state for VV/VH.", 409)
                    flags[polarization].append(value.strip().lower() == "true")
        except SARPreprocessingError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, ElementTree.ParseError) as exc:
            raise SARPreprocessingError("source_annotation_invalid", "A SAFE annotation required to determine processing history is unreadable.", 409) from exc
        if any(not values for values in flags.values()):
            raise SARPreprocessingError("thermal_noise_state_unavailable", "SAFE annotations do not identify thermal-noise correction state for both selected polarizations.", 409)
        if any(len(set(values)) != 1 for values in flags.values()):
            raise SARPreprocessingError("thermal_noise_state_inconsistent", "SAFE annotations disagree about prior thermal-noise correction within a polarization.", 409)
        return {polarization: values[0] for polarization, values in flags.items()}

    def _load_pair(self, request: SARPreprocessingRequest) -> tuple[dict[str, Any], dict[str, Any], Path, Path, dict[str, Any]]:
        data_manifest_path = self.settings.data_dir / "manifest.json"
        try:
            body = json.loads(data_manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SARPreprocessingError("download_manifest_missing", "No validated Sentinel-1 download manifest exists in the configured data directory.", 409) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SARPreprocessingError("download_manifest_invalid", "The Sentinel-1 download manifest cannot be read.", 500) from exc
        if not isinstance(body, dict) or body.get("schema_version") != 1 or not isinstance(body.get("downloads"), list):
            raise SARPreprocessingError("download_manifest_invalid", "The Sentinel-1 download manifest has an unsupported structure.", 500)
        by_id = {entry.get("download_id"): entry for entry in body["downloads"] if isinstance(entry, dict)}
        pre = by_id.get(str(request.pre_download_id))
        post = by_id.get(str(request.post_download_id))
        if pre is None or post is None:
            raise SARPreprocessingError("download_not_found", "Both download IDs must refer to entries in the local Sentinel-1 manifest.", 404)
        if pre.get("role") != "pre_event" or post.get("role") != "post_event":
            raise SARPreprocessingError("download_role_mismatch", "Select one pre_event download and one post_event download.", 422)
        for entry in (pre, post):
            if entry.get("asset_key") != "Product" or entry.get("collection") != COLLECTION_ID:
                raise SARPreprocessingError("unsupported_source_asset", "Only the validated native Sentinel-1 GRD Product archive is supported in this workflow.", 422)
        required_pair_fields = ("pre_scene_id", "post_scene_id", "event_date", "bbox")
        if any(pre.get(key) != post.get(key) for key in required_pair_fields):
            raise SARPreprocessingError("downloads_from_different_pairs", "The selected downloads do not belong to the same recorded observation pair and AOI.", 422)
        if pre.get("pre_scene_id") != pre.get("scene_id") or post.get("post_scene_id") != post.get("scene_id"):
            raise SARPreprocessingError("download_pair_identity_mismatch", "The local download records do not match their recorded pre/post scene identities.", 422)
        try:
            event_date = date.fromisoformat(pre["event_date"])
            selected_bbox = tuple(float(value) for value in pre["bbox"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SARPreprocessingError("download_pair_metadata_invalid", "The source download records lack valid event-date or AOI metadata.", 409) from exc
        requested = box(*request.bbox)
        if not box(*selected_bbox).covers(requested):
            raise SARPreprocessingError("requested_aoi_outside_selected_pair", "The requested AOI must be within the AOI used to select this compatible pair.", 422)
        root = self.settings.data_dir
        pre_path = self._safe_product_path(root, pre)
        post_path = self._safe_product_path(root, post)

        pre_item, pre_metadata = self.metadata_fetcher(str(pre["scene_id"]))
        post_item, post_metadata = self.metadata_fetcher(str(post["scene_id"]))
        pairs = rank_observation_pairs([pre_metadata], [post_metadata], request.bbox, event_date)
        if not pairs or pairs[0].verification_status != "compatible":
            raise SARPreprocessingError("pair_not_compatible", "The selected pair no longer passes strict date, orbit, polarization and AOI checks.", 422)
        asset_records: dict[str, Any] = {}
        for label, item, metadata, download_record in (
            ("pre-event", pre_item, pre_metadata, pre),
            ("post-event", post_item, post_metadata, post),
        ):
            assets = item.get("assets")
            product_asset = assets.get("Product") if isinstance(assets, dict) else None
            if not isinstance(product_asset, dict) or product_asset.get("type") != "application/zip" or "archive" not in product_asset.get("roles", []):
                raise SARPreprocessingError("source_asset_changed", f"CDSE no longer exposes the selected native Product archive for the {label} scene.", 409)
            if metadata.scene_id != (pre if label == "pre-event" else post).get("scene_id"):
                raise SARPreprocessingError("catalogue_identity_mismatch", f"CDSE metadata identity changed for the {label} scene.", 502)
            footprint = item.get("geometry")
            try:
                if not isinstance(footprint, dict) or not shape(footprint).covers(requested):
                    raise SARPreprocessingError("source_coverage_changed", f"The {label} scene catalogue footprint does not cover the requested AOI.", 422)
            except SARPreprocessingError:
                raise
            except Exception as exc:
                raise SARPreprocessingError("invalid_catalogue_footprint", f"The {label} scene has invalid footprint metadata.", 502) from exc
            key = "pre" if label == "pre-event" else "post"
            validation = download_record.get("validation")
            integrity = validation.get("integrity") if isinstance(validation, dict) else None
            asset_records[key] = {
                "asset_key": "Product",
                "media_type": product_asset.get("type"),
                "roles": product_asset.get("roles"),
                "catalogue_size_bytes": product_asset.get("file:size"),
                "product_name": metadata.product_name,
                "odata_product_id": download_record.get("odata_product_id"),
                "download_id": download_record.get("download_id"),
                "local_path_relative_to_raw_data_dir": download_record.get("file_path"),
                "file_size_bytes": download_record.get("file_size_bytes"),
                "computed_sha256": download_record.get("computed_sha256"),
                "provider_checksums": download_record.get("provider_checksums", []),
                "integrity_validation": integrity,
            }
        pair_provenance = {
            "event_date": event_date.isoformat(),
            "selection_bbox_wgs84": list(selected_bbox),
            "requested_bbox_wgs84": list(request.bbox),
            "catalogue_source": STAC_ITEM_URL,
            "pre": self._metadata_record(pre_metadata),
            "post": self._metadata_record(post_metadata),
            "source_assets": asset_records,
            "orbit_compatibility": pairs[0].orbit_compatibility,
            "polarization_compatibility": pairs[0].polarization_compatibility,
            "common_aoi_coverage": pairs[0].common_aoi_coverage,
        }
        return pre, post, pre_path, post_path, pair_provenance

    @staticmethod
    def _metadata_record(metadata: ObservationMetadata) -> dict[str, Any]:
        return {
            "scene_id": metadata.scene_id,
            "product_name": metadata.product_name,
            "product_type": metadata.product_type,
            "acquisition_start": metadata.acquisition_start.isoformat().replace("+00:00", "Z") if metadata.acquisition_start else None,
            "acquisition_end": metadata.acquisition_end.isoformat().replace("+00:00", "Z") if metadata.acquisition_end else None,
            "orbit_direction": metadata.orbit_direction,
            "relative_orbit": metadata.relative_orbit,
            "polarization": metadata.polarization,
            "platform": metadata.platform,
            "catalogue_source": metadata.catalogue_source,
            "metadata_retrieved_at": metadata.metadata_retrieved_at.isoformat().replace("+00:00", "Z"),
        }

    def create_job(self, request: SARPreprocessingRequest) -> dict[str, Any]:
        if not self.snap_executable:
            raise SARPreprocessingError("snap_not_installed", "ESA SNAP Graph Processing Tool (gpt) was not found. Install SNAP and set JALDRISHTI_SNAP_GPT before starting preprocessing.", 503)
        if not request.allow_snap_auxiliary_downloads:
            raise SARPreprocessingError("auxiliary_downloads_not_approved", "SNAP may need orbit files and the selected DEM. Set allow_snap_auxiliary_downloads=true only when you approve those auxiliary downloads.", 409)
        pre, post, pre_path, post_path, provenance = self._load_pair(request)
        job_id = str(uuid.uuid4())
        now = self._now()
        job = {
            "job_id": job_id,
            "status": "selected",
            "stage": "validated_inputs",
            "created_at": now,
            "updated_at": now,
            "source_download_ids": [pre["download_id"], post["download_id"]],
            "source_scene_ids": [pre["scene_id"], post["scene_id"]],
            "source_paths": [pre["file_path"], post["file_path"]],
            "storage": {
                "processed_root_configured_as": os.getenv("JALDRISHTI_PROCESSED_DIR", "data/processed/sentinel1"),
                "job_output_directory": job_id,
                "manifest_relative_path": "data/manifests/sentinel1_preprocessing/manifest.json",
            },
            "pair": provenance,
            "parameters": {
                "bbox_wgs84": list(request.bbox),
                "target_crs": CRS.from_user_input(request.target_crs).to_string(),
                "pixel_spacing_m": request.pixel_spacing_m,
                "dem_name": request.dem_name,
                "dem_resampling_method": "BILINEAR_INTERPOLATION",
                "terrain_correction_image_resampling_method": "BILINEAR_INTERPOLATION",
                "common_grid_resampling_method": "bilinear",
                "orbit_type": "Sentinel Precise (Auto Download)",
                "snap_auxiliary_downloads_approved": request.allow_snap_auxiliary_downloads,
                "speckle_filter": "not_applied",
                "output_backscatter": "linear Sigma0 power, float32",
                "snap_output_band_order": list(SNAP_OUTPUT_BAND_ORDER),
                "snap_band_order_provenance": SNAP_BAND_ORDER_PROVENANCE,
                "thermal_noise_removal": "determined from SAFE annotations before SNAP processing",
                "source_attribution": SAFE_ATTRIBUTION,
                "dem_source_attribution": WORLDDEM30_SOURCE_ATTRIBUTION,
                "dem_attribution": WORLDDEM30_MODIFIED_ATTRIBUTION,
            },
            "outputs": None,
            "model_input_status": MODEL_INPUT_BLOCKED,
            "software": {
                "python": sys.version.split()[0],
                "rasterio": rasterio.__version__,
                "numpy": np.__version__,
                "snap": None,
            },
            "warnings": [
                "These downloaded COG_SAFE products contain detected measurements; SAFE annotations determine whether SNAP thermal-noise removal is required, and calibration/terrain correction are applied once.",
                "Model tensor bands, normalization, units and dimensions remain unverified because the repository contains no model contract or trained checkpoint.",
                "Copernicus DEM 30 m access may require the CDSE contributing-missions profile setting; no DEM replacement is attempted.",
            ],
            "error": None,
        }
        with self._manifest_lock:
            manifest = self._load_manifest()
            manifest["jobs"].append(job)
            self._save_manifest(manifest)
        return dict(job)

    def run_job(self, job_id: str) -> None:
        try:
            self._update(job_id, {"status": "running", "stage": "snap_processing", "error": None})
            job = self.get_status(job_id)
            parameters = job["parameters"]
            snap_version = self._get_snap_version()
            software = dict(job.get("software") or {})
            software["snap"] = snap_version
            self._update(job_id, {"software": software})
            work_dir = self.processed_dir / job_id
            work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            intermediate_pre = work_dir / "pre_event_snap.tif"
            intermediate_post = work_dir / "post_event_snap.tif"
            graph_dir = work_dir / "graphs"
            graph_dir.mkdir(mode=0o700)
            paths = [self.settings.data_dir / rel for rel in job["source_paths"]]
            self._update(job_id, {"stage": "source_annotation_and_crc_validation"})
            source_processing = [self._inspect_source_processing(path) for path in paths]
            pre_flags, post_flags = source_processing
            if len(set(pre_flags.values())) != 1 or len(set(post_flags.values())) != 1:
                raise SARPreprocessingError("thermal_noise_state_inconsistent", "VV and VH annotations disagree about prior thermal-noise correction; the product cannot be processed consistently.", 422)
            if pre_flags != post_flags:
                raise SARPreprocessingError("thermal_noise_processing_mismatch", "Pre/post SAFE products disagree on prior thermal-noise correction; this pair cannot be processed consistently.", 422)
            apply_thermal_noise = not all(pre_flags.values())
            parameters["thermal_noise_removal"] = apply_thermal_noise
            self._update(job_id, {
                "parameters": parameters,
                "source_processing": {
                    "pre_event": {"thermal_noise_correction_performed": pre_flags},
                    "post_event": {"thermal_noise_correction_performed": post_flags},
                    "radiometric_calibration": "not recorded as pre-applied for Sentinel-1 Level-1 GRD; SNAP Sigma0 calibration is applied once",
                },
            })
            # SNAP writes full-scene intermediate rasters before the AOI crop.
            # Reserve an intentionally conservative multiple of the validated
            # input sizes and final AOI arrays before invoking the processor.
            expected_source_bytes = 0
            download_manifest = json.loads((self.settings.data_dir / "manifest.json").read_text(encoding="utf-8"))
            source_records = {item.get("download_id"): item for item in download_manifest.get("downloads", []) if isinstance(item, dict)}
            for source_id in job["source_download_ids"]:
                expected_source_bytes += int(source_records[source_id]["expected_size_bytes"])
            required_free_bytes = max(4 * 1024**3, expected_source_bytes * 3)
            available_path = self.processed_dir if self.processed_dir.exists() else PROJECT_ROOT
            if shutil.disk_usage(available_path).free < required_free_bytes:
                raise SARPreprocessingError("insufficient_disk_space", "Free disk space is below the conservative SNAP intermediate-space estimate; no processing was started.", 507)
            raw_outputs = [intermediate_pre, intermediate_post]
            for source, target, role in zip(paths, raw_outputs, ("pre_event", "post_event")):
                graph_path = graph_dir / f"{role}.xml"
                graph_path.write_text(self._graph_xml(parameters), encoding="utf-8")
                command = [
                    str(self.snap_executable), str(graph_path),
                    f"-Pinput={source}", f"-Poutput={target}",
                    f"-Ptarget_crs={parameters['target_crs']}",
                    f"-Ppixel_spacing={parameters['pixel_spacing_m']}",
                ]
                self.snap_runner(command, work_dir / f"{role}_snap.log", self.timeout_seconds)
                if not target.is_file() or target.stat().st_size <= 0:
                    raise SARPreprocessingError("snap_output_missing", f"SNAP did not produce a valid {role} intermediate raster.", 500)

            self._update(job_id, {"stage": "aligning_common_grid"})
            final = self._align_pair(raw_outputs, work_dir, parameters)
            self._update(job_id, {"status": "completed", "stage": "geospatially_validated", "outputs": final, "error": None})
        except SARPreprocessingError as exc:
            self._update(job_id, {"status": "failed", "stage": "failed", "error": {"code": exc.code, "message": exc.message}})
        except subprocess.TimeoutExpired:
            self._update(job_id, {"status": "failed", "stage": "failed", "error": {"code": "snap_timeout", "message": "SNAP exceeded JALDRISHTI_SNAP_TIMEOUT_SECONDS; partial outputs were not accepted."}})
        except Exception as exc:
            # Do not persist exception text: child tools may echo local details or URLs.
            self._update(job_id, {"status": "failed", "stage": "failed", "error": {"code": "preprocessing_failed", "message": f"SAR preprocessing failed ({type(exc).__name__}); inspect the local job log and input validation."}})

    @staticmethod
    def _graph_xml(parameters: dict[str, Any]) -> str:
        # SNAP's automatic auxiliary retrieval is enabled only after the caller
        # explicitly approves it in the request.
        return """<?xml version="1.0" encoding="UTF-8"?>
<graph id="JalDrishti-S1-GRD-Linear-Sigma0-Terrain-Corrected">
  <version>1.0</version>
  <node id="Read"><operator>Read</operator><parameters><file>${input}</file></parameters></node>
  <node id="Apply-Orbit-File"><operator>Apply-Orbit-File</operator><sources><sourceProduct refid="Read"/></sources><parameters>
    <orbitType>Sentinel Precise (Auto Download)</orbitType>
    <continueOnFail>false</continueOnFail>
  </parameters></node>
  {thermal_node}
  <node id="GRD-Border-Noise-Removal"><operator>Remove-GRD-Border-Noise</operator><sources><sourceProduct refid="{thermal_source}"/></sources></node>
  <node id="Calibration"><operator>Calibration</operator><sources><sourceProduct refid="GRD-Border-Noise-Removal"/></sources><parameters>
    <outputSigmaBand>true</outputSigmaBand><outputGammaBand>false</outputGammaBand><outputBetaBand>false</outputBetaBand>
    <outputImageScaleInDb>false</outputImageScaleInDb><selectedPolarisations>VV,VH</selectedPolarisations>
  </parameters></node>
  <node id="Terrain-Correction"><operator>Terrain-Correction</operator><sources><sourceProduct refid="Calibration"/></sources><parameters>
    <demName>Copernicus 30m Global DEM</demName><mapProjection>${target_crs}</mapProjection>
    <pixelSpacingInMeter>${pixel_spacing}</pixelSpacingInMeter><demResamplingMethod>BILINEAR_INTERPOLATION</demResamplingMethod>
    <imgResamplingMethod>BILINEAR_INTERPOLATION</imgResamplingMethod>
  </parameters></node>
  <node id="Select-Sigma0-Bands"><operator>BandSelect</operator><sources><sourceProduct refid="Terrain-Correction"/></sources><parameters>
    <sourceBands>{band_order}</sourceBands>
  </parameters></node>
  <node id="Write"><operator>Write</operator><sources><sourceProduct refid="Select-Sigma0-Bands"/></sources><parameters>
    <file>${output}</file><formatName>GeoTIFF-BigTIFF</formatName><deleteOutputOnFailure>true</deleteOutputOnFailure>
  </parameters></node>
</graph>
""".replace(
            "{band_order}", ",".join(SNAP_OUTPUT_BAND_ORDER)
        ).replace(
            "{thermal_node}",
            (
                '<node id="Thermal-Noise-Removal"><operator>ThermalNoiseRemoval</operator>'
                '<sources><sourceProduct refid="Apply-Orbit-File"/></sources></node>'
                if parameters.get("thermal_noise_removal")
                else ""
            ),
        ).replace("{thermal_source}", "Thermal-Noise-Removal" if parameters.get("thermal_noise_removal") else "Apply-Orbit-File")

    @staticmethod
    def _run_snap(command: list[str], log_path: Path, timeout_seconds: int) -> None:
        environment = os.environ.copy()
        for secret_name in ("CDSE_USERNAME", "CDSE_PASSWORD", "CDSE_ACCESS_TOKEN", "CDSE_CLIENT_SECRET"):
            environment.pop(secret_name, None)
        with log_path.open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_seconds, check=False, env=environment)
        if result.returncode != 0:
            raise SARPreprocessingError("snap_processing_failed", "SNAP returned a non-zero exit status. Review the local per-scene SNAP log.", 500)

    def _get_snap_version(self) -> str | None:
        environment = os.environ.copy()
        for secret_name in ("CDSE_USERNAME", "CDSE_PASSWORD", "CDSE_ACCESS_TOKEN", "CDSE_CLIENT_SECRET"):
            environment.pop(secret_name, None)
        try:
            result = subprocess.run(
                [str(self.snap_executable), "--diag"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=min(30, self.timeout_seconds),
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"SNAP Release version\s+([A-Za-z0-9._-]+)", result.stdout or "")
        return match.group(1) if result.returncode == 0 and match else None

    @staticmethod
    def _align_pair(raw_paths: list[Path], work_dir: Path, parameters: dict[str, Any]) -> dict[str, Any]:
        work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_crs = CRS.from_user_input(parameters["target_crs"])
        bbox = tuple(float(value) for value in parameters["bbox_wgs84"])
        left, bottom, right, top = transform_bounds("EPSG:4326", target_crs, *bbox, densify_pts=21)
        spacing = float(parameters["pixel_spacing_m"])
        grid_left = math.floor(left / spacing) * spacing
        grid_bottom = math.floor(bottom / spacing) * spacing
        grid_right = math.ceil(right / spacing) * spacing
        grid_top = math.ceil(top / spacing) * spacing
        width = int(round((grid_right - grid_left) / spacing))
        height = int(round((grid_top - grid_bottom) / spacing))
        if width <= 0 or height <= 0 or width * height > 100_000_000:
            raise SARPreprocessingError("invalid_output_grid", "Requested AOI and pixel spacing produce an invalid or excessively large raster grid.", 422)
        transform = from_origin(grid_left, grid_top, spacing, spacing)
        projected_aoi = transform_geom("EPSG:4326", target_crs, mapping(box(*bbox)), precision=8)
        aoi_in_grid = geometry_mask(
            [projected_aoi],
            out_shape=(height, width),
            transform=transform,
            invert=True,
            all_touched=False,
        )
        role_arrays: list[np.ndarray] = []
        role_masks: list[np.ndarray] = []
        role_stats: list[dict[str, Any]] = []
        if parameters.get("snap_output_band_order") != list(SNAP_OUTPUT_BAND_ORDER):
            raise SARPreprocessingError(
                "snap_band_order_unverified",
                "SNAP output band order is not backed by the explicit BandSelect graph configuration.",
                500,
            )
        for role, source_path in zip(("pre_event", "post_event"), raw_paths):
            with rasterio.open(source_path) as source:
                if source.crs is None:
                    raise SARPreprocessingError("snap_output_missing_crs", f"SNAP {role} output has no CRS.", 500)
                if CRS.from_user_input(source.crs) != target_crs:
                    raise SARPreprocessingError("snap_output_crs_mismatch", f"SNAP {role} output CRS differs from the requested projected CRS.", 500)
                if source.count != len(SNAP_OUTPUT_BAND_ORDER) or source.dtypes != ("float32", "float32"):
                    raise SARPreprocessingError(
                        "invalid_snap_band_layout",
                        f"SNAP {role} output must contain exactly two float32 bands selected in the recorded Sigma0 order.",
                        500,
                    )
                # SNAP's GeoTIFF writer may omit band descriptions. The graph's
                # BandSelect node validates the names and the selected order;
                # accept absent labels only with that explicit graph provenance.
                for index, (description, expected_name) in enumerate(zip(source.descriptions, SNAP_OUTPUT_BAND_ORDER), start=1):
                    if description and description != expected_name:
                        raise SARPreprocessingError(
                            "snap_band_order_mismatch",
                            f"SNAP {role} output band {index} is labelled {description!r}, expected {expected_name!r}.",
                            500,
                        )
                indices = [1, 2]
                if source.width <= 0 or source.height <= 0:
                    raise SARPreprocessingError("invalid_snap_raster", f"SNAP {role} output has invalid raster dimensions.", 500)
                bands = np.full((2, height, width), np.nan, dtype=np.float32)
                for out_index, source_index in enumerate(indices):
                    reproject(
                        source=rasterio.band(source, source_index),
                        destination=bands[out_index],
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=source.nodatavals[source_index - 1],
                        dst_transform=transform,
                        dst_crs=target_crs,
                        dst_nodata=np.nan,
                        resampling=Resampling.bilinear,
                        num_threads=2,
                    )
                mask = np.isfinite(bands).all(axis=0) & aoi_in_grid
                bands[:, ~mask] = np.nan
                if not mask.any():
                    raise SARPreprocessingError("aoi_has_no_valid_pixels", f"SNAP {role} output contains no valid Sigma0 pixels inside the requested AOI.", 422)
                role_arrays.append(bands)
                role_masks.append(mask)
        common_mask = role_masks[0] & role_masks[1] & aoi_in_grid
        if not common_mask.any():
            raise SARPreprocessingError("pair_has_no_common_valid_pixels", "The pre/post rasters have no common valid pixels inside the requested AOI.", 422)
        for bands in role_arrays:
            bands[:, ~common_mask] = np.nan
        total_aoi_pixels = int(aoi_in_grid.sum())
        individual_valid_counts = [int(mask.sum()) for mask in role_masks]
        thermal_label = (
            "thermal noise removal applied"
            if parameters.get("thermal_noise_removal")
            else "existing thermal-noise correction retained"
        )

        output_records: dict[str, Any] = {}
        profile = {
            "driver": "GTiff", "width": width, "height": height, "count": 2,
            "dtype": "float32", "crs": target_crs, "transform": transform,
            "nodata": float("nan"), "compress": "deflate", "predictor": 3,
            "tiled": True, "blockxsize": 256, "blockysize": 256,
        }
        grid_signature = {
            "width": width,
            "height": height,
            "transform": [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f],
            "crs": target_crs.to_string(),
        }
        for role_index, (role, bands) in enumerate(zip(("pre_event", "post_event"), role_arrays)):
            output_path = work_dir / f"{role}.tif"
            temporary_path = work_dir / f".{role}.tif.tmp"
            with rasterio.open(temporary_path, "w", **profile) as target:
                target.write(bands)
                target.set_band_description(1, "Sigma0_VV")
                target.set_band_description(2, "Sigma0_VH")
                target.update_tags(
                    AREA_OR_POINT="Area",
                    BACKSCATTER_UNITS="linear_sigma0_power",
                    PROCESSING=f"SNAP orbit, {thermal_label}, GRD border noise, Sigma0 calibration, terrain correction; common-grid warp",
                    SNAP_BAND_SELECTION=",".join(SNAP_OUTPUT_BAND_ORDER),
                    BAND_ORDER_PROVENANCE=SNAP_BAND_ORDER_PROVENANCE,
                    SOURCE_ATTRIBUTION=SAFE_ATTRIBUTION,
                    DEM_SOURCE_ATTRIBUTION=WORLDDEM30_SOURCE_ATTRIBUTION,
                    DEM_ATTRIBUTION=WORLDDEM30_MODIFIED_ATTRIBUTION,
                    ANALYSIS_READY="false",
                )
            os.replace(temporary_path, output_path)
            values = bands[:, common_mask]
            role_stats.append({
                "valid_pixels": int(common_mask.sum()),
                "valid_aoi_coverage_fraction": float(common_mask.sum() / total_aoi_pixels) if total_aoi_pixels else 0.0,
                "bands": {
                    name: {
                        "minimum": float(np.min(values[index])),
                        "maximum": float(np.max(values[index])),
                        "mean": float(np.mean(values[index], dtype=np.float64)),
                        "standard_deviation": float(np.std(values[index], dtype=np.float64)),
                    }
                    for index, name in enumerate(("Sigma0_VV", "Sigma0_VH"))
                },
            })
            output_records[role] = {
                "path": str(output_path.relative_to(work_dir.parent)),
                "sha256": SARPreprocessingService._sha256_file(output_path),
                "crs": target_crs.to_string(),
                "transform": grid_signature["transform"],
                "width": width,
                "height": height,
                "pixel_spacing_m": spacing,
                "bands": ["Sigma0_VV", "Sigma0_VH"],
                "band_order_provenance": SNAP_BAND_ORDER_PROVENANCE,
                "dtype": "float32",
                "units": "linear Sigma0 power",
                "nodata": "NaN",
                "valid_data": role_stats[-1],
                "valid_aoi_pixel_count": individual_valid_counts[role_index],
                "invalid_aoi_pixel_count": total_aoi_pixels - individual_valid_counts[role_index],
                "analysis_ready": False,
            }
        with rasterio.open(work_dir / "pre_event.tif") as pre, rasterio.open(work_dir / "post_event.tif") as post:
            if (
                (pre.width, pre.height, pre.transform, pre.crs, pre.descriptions)
                != (post.width, post.height, post.transform, post.crs, post.descriptions)
                or pre.count != 2
                or post.count != 2
                or pre.dtypes != ("float32", "float32")
                or post.dtypes != ("float32", "float32")
            ):
                raise SARPreprocessingError("pair_grid_mismatch", "Written pre/post outputs failed CRS, grid, band, or data-type alignment checks.", 500)
        return {
            "pre_event": output_records["pre_event"],
            "post_event": output_records["post_event"],
            "common_grid": {
                "crs": target_crs.to_string(),
                "transform": grid_signature["transform"],
                "width": width,
                "height": height,
                "pixel_spacing_m": spacing,
                "aoi_pixel_count": total_aoi_pixels,
                "common_valid_pixel_count": int(common_mask.sum()),
                "common_valid_coverage_fraction": float(common_mask.sum() / total_aoi_pixels) if total_aoi_pixels else 0.0,
                "pre_post_grid_aligned": True,
                "alignment_check": {
                    "pre_event": grid_signature,
                    "post_event": grid_signature,
                },
            },
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
