from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit
import uuid

import httpx

from ..core.config import Settings
from ..schemas.downloads import ProductDownloadSelection
from ..schemas.observations import ObservationMetadata
from ..services.product_validation import (
    ProductValidationError,
    compute_checksums,
    inspect_geospatial_metadata,
    validate_native_archive,
)
from ..services.satellite_catalogue import (
    COLLECTION_ID,
    STAC_SEARCH_URL,
    CatalogueSearchError,
    _normalize_feature,
)
from .scene_matching import rank_observation_pairs


STAC_ITEM_URL = "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-1-grd/items"
ODATA_PRODUCTS_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
ODATA_DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SAFE_ATTRIBUTION = "Contains modified Copernicus Sentinel data 2026."
PRODUCT_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}\Z")
CHECKSUM_ALGORITHMS = {"MD5", "SHA1", "SHA256", "BLAKE3"}
PROGRESS_WRITE_INTERVAL = 32 * 1024 * 1024


class ProductDownloadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 503,
        upstream_status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.upstream_status = upstream_status
        self.retry_after = retry_after


class ProductDownloadService:
    """One-product-at-a-time CDSE OData downloads with a local JSON manifest."""

    _manifest_lock = threading.RLock()

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or Settings.from_environment()
        self.transport = transport
        self.sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        # This local prototype stores jobs in a manifest but runs work inside
        # the API process. Anything still selected/downloading at construction
        # time belongs to a previous process and cannot still be progressing.
        # Recover lazily when the service is first created after an API restart.
        try:
            self._recover_interrupted_jobs()
        except ProductDownloadError:
            # Preserve the normal request path's explicit manifest error; do
            # not hide or overwrite a malformed manifest during construction.
            pass

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.connect_timeout_seconds,
            read=self.settings.read_timeout_seconds,
            write=self.settings.read_timeout_seconds,
            pool=self.settings.connect_timeout_seconds,
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout(),
            follow_redirects=True,
            transport=self.transport,
            headers={"User-Agent": "JalDrishti/0.1 (controlled Sentinel-1 download)"},
        )

    @property
    def manifest_path(self) -> Path:
        return self.settings.data_dir / "manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"schema_version": 1, "downloads": []}
        try:
            with self.manifest_path.open("r", encoding="utf-8") as source:
                body = json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductDownloadError(
                "manifest_unreadable",
                "The local download manifest is unreadable; it was left unchanged.",
                500,
            ) from exc
        if not isinstance(body, dict) or body.get("schema_version") != 1 or not isinstance(body.get("downloads"), list):
            raise ProductDownloadError(
                "manifest_invalid",
                "The local download manifest has an unsupported structure; it was left unchanged.",
                500,
            )
        return body

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manifest_path.parent.chmod(0o700)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".manifest-",
                suffix=".tmp",
                dir=self.settings.data_dir,
                delete=False,
            ) as target:
                temporary_name = target.name
                json.dump(manifest, target, ensure_ascii=False, indent=2, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.manifest_path)
            os.chmod(self.manifest_path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise ProductDownloadError(
                "manifest_write_failed",
                "The download manifest could not be written safely.",
                500,
            ) from exc

    def _recover_interrupted_jobs(self) -> None:
        if not self.manifest_path.exists():
            return

        with self._manifest_lock:
            manifest = self._load_manifest()
            changed = False
            now = self._now()
            temporary_dir = self.settings.data_dir / "temporary"
            for entry in manifest["downloads"]:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if not isinstance(status, str) or status not in {"selected", "downloading"}:
                    continue

                download_id = entry.get("download_id")
                if isinstance(download_id, str) and PRODUCT_ID_PATTERN.fullmatch(download_id):
                    for partial in temporary_dir.glob(f"{download_id}-*.part"):
                        try:
                            partial.unlink(missing_ok=True)
                        except OSError:
                            entry.setdefault("warnings", []).append(
                                "An interrupted temporary file could not be removed automatically."
                            )

                error = {
                    "code": "worker_interrupted",
                    "message": "The API process stopped during this download. The incomplete transfer was discarded; submit the same explicit selection again to retry.",
                }
                attempts = entry.get("attempts", [])
                if isinstance(attempts, list):
                    for attempt in attempts:
                        if isinstance(attempt, dict) and attempt.get("outcome") == "downloading" and not attempt.get("ended_at"):
                            attempt.update({"ended_at": now, "outcome": "interrupted", "error": error})
                entry.update({"status": "failed", "error": error, "updated_at": now, "progress_bytes": 0})
                changed = True

            if changed:
                self._save_manifest(manifest)

    def _update_job(self, download_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next((item for item in manifest["downloads"] if item.get("download_id") == download_id), None)
            if entry is None:
                raise ProductDownloadError("download_not_found", "The download job was not found.", 404)
            entry.update(changes)
            entry["updated_at"] = self._now()
            self._save_manifest(manifest)
            return dict(entry)

    def _get_job(self, download_id: str) -> dict[str, Any]:
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next((item for item in manifest["downloads"] if item.get("download_id") == download_id), None)
            if entry is None:
                raise ProductDownloadError("download_not_found", "The download job was not found.", 404)
            return dict(entry)

    def _public_job(self, entry: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        allowed = {
            "download_id",
            "scene_id",
            "role",
            "asset_key",
            "product_name",
            "status",
            "created_at",
            "updated_at",
            "progress_bytes",
            "expected_size_bytes",
            "file_size_bytes",
            "file_path",
            "computed_sha256",
            "provider_checksums",
            "checksum_status",
            "validation",
            "warnings",
            "error",
        }
        result = {key: entry[key] for key in allowed if key in entry}
        result["duplicate"] = duplicate
        return result

    def get_status(self, download_id: str) -> dict[str, Any]:
        return self._public_job(self._get_job(download_id))

    def reinspect_geospatial_metadata(self, download_id: str) -> dict[str, Any]:
        """Re-run metadata inspection on an already downloaded, integrity-validated archive."""
        entry = self._get_job(download_id)
        if entry.get("status") != "validation_failed":
            raise ProductDownloadError(
                "inspection_not_available",
                "Only a downloaded product that failed geospatial inspection can be re-inspected.",
                409,
            )

        validation = entry.get("validation")
        integrity = validation.get("integrity") if isinstance(validation, dict) else None
        if not isinstance(integrity, dict) or integrity.get("integrity") != "valid":
            raise ProductDownloadError(
                "integrity_not_validated",
                "The stored product has no successful integrity validation to reuse.",
                409,
            )

        relative_path = entry.get("file_path")
        file_name = entry.get("file_name")
        if (
            not isinstance(relative_path, str)
            or not isinstance(file_name, str)
            or relative_path != f"products/{file_name}"
            or Path(relative_path).is_absolute()
            or "\\" in relative_path
            or ":" in relative_path
            or ".." in Path(relative_path).parts
        ):
            raise ProductDownloadError("unsafe_product_path", "The stored product path is invalid; no file was opened.", 409)

        try:
            data_root = self.settings.data_dir.resolve(strict=True)
            candidate = self.settings.data_dir / relative_path
            if candidate.is_symlink():
                raise ProductDownloadError("unsafe_product_path", "The stored product path is a symbolic link; no file was opened.", 409)
            product_path = candidate.resolve(strict=True)
            product_path.relative_to(data_root)
            actual_size = product_path.stat().st_size
        except ProductDownloadError:
            raise
        except (OSError, ValueError) as exc:
            raise ProductDownloadError("stored_product_missing", "The previously downloaded product file is unavailable for inspection.", 409) from exc

        expected_size = entry.get("expected_size_bytes")
        recorded_size = entry.get("file_size_bytes")
        if (
            not product_path.is_file()
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or actual_size <= 0
            or actual_size != expected_size
            or recorded_size != actual_size
        ):
            raise ProductDownloadError("stored_product_size_mismatch", "The stored product size no longer matches its validated download record.", 409)

        started_at = self._now()
        inspection_attempt = {"started_at": started_at, "ended_at": None, "outcome": "inspecting"}
        prior_attempts = entry.get("inspection_attempts", [])
        inspection_attempts = list(prior_attempts) if isinstance(prior_attempts, list) else []
        inspection_attempts.append(inspection_attempt)
        self._update_job(download_id, {"inspection_attempts": inspection_attempts})

        try:
            geospatial = inspect_geospatial_metadata(product_path, integrity)
        except ProductValidationError as exc:
            inspection_attempt.update(
                {"ended_at": self._now(), "outcome": "validation_failed", "error": {"code": exc.code, "message": exc.message}}
            )
            self._update_job(
                download_id,
                {
                    "status": "validation_failed",
                    "validation": {"integrity": integrity, "geospatial": {"status": "failed", "code": exc.code}},
                    "inspection_attempts": inspection_attempts,
                    "error": {"code": exc.code, "message": exc.message},
                    "warnings": [*integrity.get("warnings", [])],
                },
            )
            return self.get_status(download_id)

        inspection_attempt.update({"ended_at": self._now(), "outcome": "geospatial_inspected"})
        self._update_job(
            download_id,
            {
                "status": "geospatial_inspected",
                "validation": {"integrity": integrity, "geospatial": geospatial},
                "inspection_attempts": inspection_attempts,
                "warnings": [*integrity.get("warnings", []), *geospatial["warnings"]],
                "error": None,
            },
        )
        return self.get_status(download_id)

    def _deduplicate(self, selection: ProductDownloadSelection) -> dict[str, Any] | None:
        with self._manifest_lock:
            manifest = self._load_manifest()
            for entry in reversed(manifest["downloads"]):
                if (
                    entry.get("scene_id") == selection.selected_scene_id
                    and entry.get("role") == selection.role
                    and entry.get("asset_key") == selection.asset_key
                ):
                    status = entry.get("status")
                    relative_path = entry.get("file_path")
                    file_exists = bool(relative_path and (self.settings.data_dir / relative_path).is_file())
                    if status in {"selected", "downloading"} or (
                        status in {"downloaded", "integrity_validated", "geospatial_inspected", "validation_failed"}
                        and file_exists
                    ):
                        return self._public_job(entry, duplicate=True)
            return None

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        stage: str,
        not_found_code: str | None = None,
    ) -> dict[str, Any]:
        last_error: ProductDownloadError | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                with self._client() as client:
                    response = client.post(url, data=data) if data is not None else client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = ProductDownloadError(
                    f"{stage}_timeout",
                    f"The CDSE {stage.replace('_', ' ')} request timed out.",
                    503,
                )
                if attempt < self.settings.max_attempts:
                    self.sleep(min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds))
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                last_error = ProductDownloadError(
                    f"{stage}_network_error",
                    f"Could not connect to CDSE for the {stage.replace('_', ' ')} request.",
                    503,
                )
                if attempt < self.settings.max_attempts:
                    self.sleep(min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds))
                    continue
                raise last_error from exc

            if response.status_code in (429, 500, 502, 503, 504):
                code = "catalogue_rate_limited" if response.status_code == 429 else f"{stage}_unavailable"
                last_error = ProductDownloadError(
                    code,
                    "CDSE requested that the operation be retried later." if response.status_code == 429 else f"CDSE returned a temporary error during {stage.replace('_', ' ')}.",
                    503,
                    response.status_code,
                    response.headers.get("Retry-After"),
                )
                if attempt < self.settings.max_attempts:
                    delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                    if delay is None:
                        raise last_error
                    self.sleep(delay)
                    continue
                raise last_error
            if response.status_code in (401, 403):
                raise ProductDownloadError(
                    "catalogue_access_denied",
                    "CDSE denied access to product metadata.",
                    502,
                    response.status_code,
                )
            if response.status_code == 404 and not_found_code:
                raise ProductDownloadError(not_found_code, "The selected Sentinel-1 product is no longer available in CDSE.", 404, 404)
            if not 200 <= response.status_code < 300:
                raise ProductDownloadError(
                    f"{stage}_rejected",
                    f"CDSE rejected the {stage.replace('_', ' ')} request.",
                    502,
                    response.status_code,
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProductDownloadError(
                    "invalid_catalogue_response",
                    f"CDSE returned a non-JSON response during {stage.replace('_', ' ')}.",
                    502,
                    response.status_code,
                ) from exc
            if not isinstance(body, dict):
                raise ProductDownloadError(
                    "invalid_catalogue_response",
                    f"CDSE returned an invalid JSON response during {stage.replace('_', ' ')}.",
                    502,
                    response.status_code,
                )
            return body
        if last_error:
            raise last_error
        raise ProductDownloadError(f"{stage}_failed", "The CDSE request did not complete.", 503)

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float | None:
        if retry_after:
            try:
                seconds = max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_time = parsedate_to_datetime(retry_after)
                    seconds = max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    seconds = 0.0
            if seconds > self.settings.max_backoff_seconds:
                return None
            return seconds
        return min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds)

    def _fetch_item(self, scene_id: str) -> tuple[dict[str, Any], ObservationMetadata]:
        escaped_id = quote(scene_id, safe="")
        item = self._request_json(
            f"{STAC_ITEM_URL}/{escaped_id}",
            stage="stac_item_lookup",
            not_found_code="product_not_found",
        )
        if item.get("id") != scene_id or item.get("collection") != COLLECTION_ID:
            raise ProductDownloadError("invalid_catalogue_response", "CDSE did not return the requested Sentinel-1 GRD item.", 502)
        try:
            observation = _normalize_feature(item, datetime.now(timezone.utc))
        except CatalogueSearchError as exc:
            raise ProductDownloadError(exc.code, exc.message, 502, exc.upstream_status) from exc
        if observation.product_type is None or "GRDH" not in observation.product_type.upper():
            raise ProductDownloadError("unsupported_product", "Only Sentinel-1 GRD high-resolution products are supported.", 422)
        return item, observation

    def _provider_checksums(self, record: dict[str, Any]) -> list[dict[str, str]]:
        raw = record.get("Checksum")
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise ProductDownloadError("invalid_catalogue_response", "CDSE returned invalid checksum metadata.", 502)
        checksums: list[dict[str, str]] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            algorithm = value.get("Algorithm")
            checksum_value = value.get("Value")
            if not isinstance(algorithm, str) or not isinstance(checksum_value, str):
                continue
            if algorithm.upper() not in CHECKSUM_ALGORITHMS:
                continue
            if not re.fullmatch(r"[A-Fa-f0-9]{16,128}", checksum_value):
                raise ProductDownloadError("invalid_catalogue_response", "CDSE returned malformed checksum metadata.", 502)
            checksums.append({"algorithm": algorithm.upper(), "value": checksum_value.lower()})
        return checksums

    def _lookup_odata_product(self, product_name: str, asset: dict[str, Any]) -> dict[str, Any]:
        escaped_name = product_name.replace("'", "''")
        params = {
            "$filter": f"Collection/Name eq 'SENTINEL-1' and Name eq '{escaped_name}'",
            "$select": "Id,Name,ContentType,ContentLength,Checksum,Online,EvictionDate,ContentDate",
            "$top": "2",
        }
        body = self._request_json(ODATA_PRODUCTS_URL, params=params, stage="odata_product_lookup")
        records = body.get("value")
        if not isinstance(records, list):
            raise ProductDownloadError("invalid_catalogue_response", "CDSE OData response did not include a product list.", 502)
        matches = [record for record in records if isinstance(record, dict) and record.get("Name") == product_name]
        if len(matches) != 1:
            raise ProductDownloadError("product_not_available", "CDSE did not return exactly one OData product matching the selected STAC item.", 409)
        record = matches[0]
        product_uuid = record.get("Id")
        if not isinstance(product_uuid, str) or not PRODUCT_ID_PATTERN.fullmatch(product_uuid):
            raise ProductDownloadError("invalid_catalogue_response", "CDSE returned an invalid OData product identifier.", 502)
        if record.get("Online") is not True:
            raise ProductDownloadError("product_offline", "The selected Sentinel-1 product is not currently online for download.", 409)
        expected_size = record.get("ContentLength")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
            raise ProductDownloadError("invalid_catalogue_response", "CDSE did not provide a valid product size.", 502)
        asset_size = asset.get("file:size")
        if asset_size is not None and asset_size != expected_size:
            raise ProductDownloadError("asset_size_mismatch", "The selected STAC Product asset size does not match OData product metadata.", 409)
        if expected_size > self.settings.max_download_bytes:
            raise ProductDownloadError(
                "download_limit_exceeded",
                "The selected product exceeds JALDRISHTI_MAX_DOWNLOAD_BYTES; raise the configured limit explicitly to proceed.",
                413,
            )
        return {
            "odata_id": product_uuid,
            "name": product_name,
            "content_type": record.get("ContentType"),
            "content_length": expected_size,
            "online": True,
            "eviction_date": record.get("EvictionDate"),
            "content_date": record.get("ContentDate"),
            "provider_checksums": self._provider_checksums(record),
        }

    def _find_duplicate(self, selection: ProductDownloadSelection) -> dict[str, Any] | None:
        return self._deduplicate(selection)

    def request_download(self, selection: ProductDownloadSelection) -> tuple[dict[str, Any], bool]:
        duplicate = self._find_duplicate(selection)
        if duplicate is not None:
            return duplicate, False

        if not self.settings.has_cdse_credentials:
            raise ProductDownloadError(
                "credentials_required",
                "Set CDSE_USERNAME and CDSE_PASSWORD in the API environment before requesting a product download.",
                503,
            )

        pre_item, pre = self._fetch_item(selection.pre_scene_id)
        post_item, post = self._fetch_item(selection.post_scene_id)
        pairs = rank_observation_pairs([pre], [post], selection.bbox, selection.event_date)
        if not pairs or pairs[0].verification_status != "compatible":
            raise ProductDownloadError(
                "pair_not_compatible",
                "The selected products no longer pass the compatible pre/post pair checks for this event date and AOI.",
                422,
            )

        selected_item = pre_item if selection.role == "pre_event" else post_item
        selected_observation = pre if selection.role == "pre_event" else post
        assets = selected_item.get("assets")
        if not isinstance(assets, dict):
            raise ProductDownloadError("asset_unavailable", "The selected STAC item has no asset list.", 409)
        asset = assets.get(selection.asset_key)
        if not isinstance(asset, dict):
            raise ProductDownloadError("asset_unavailable", "The selected Product asset is not present on this STAC item.", 409)
        if asset.get("type") != "application/zip" or "archive" not in asset.get("roles", []):
            raise ProductDownloadError("unsupported_asset", "Only the selected item's native ZIP Product archive is supported; no asset substitution was made.", 422)
        product_name = selected_observation.product_name
        if not product_name or not product_name.endswith(".SAFE"):
            raise ProductDownloadError("asset_unavailable", "The selected STAC item does not expose a SAFE product name for OData lookup.", 409)

        odata = self._lookup_odata_product(product_name, asset)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(self.settings.data_dir).free < odata["content_length"]:
            raise ProductDownloadError("insufficient_disk_space", "Available disk space is less than the selected product size.", 507)

        download_id = str(uuid.uuid4())
        now = self._now()
        filename = f"{selection.selected_scene_id}__{selection.role}__{download_id}.zip"
        entry = {
            "download_id": download_id,
            "scene_id": selection.selected_scene_id,
            "partner_scene_id": selection.counterpart_scene_id,
            "pre_scene_id": selection.pre_scene_id,
            "post_scene_id": selection.post_scene_id,
            "event_date": selection.event_date.isoformat(),
            "bbox": list(selection.bbox),
            "role": selection.role,
            "asset_key": selection.asset_key,
            "product_name": product_name,
            "collection": COLLECTION_ID,
            "catalogue_source": STAC_ITEM_URL,
            "source_endpoint": f"{ODATA_DOWNLOAD_URL}({odata['odata_id']})/$value",
            "odata_product_id": odata["odata_id"],
            "content_type": odata["content_type"],
            "expected_size_bytes": odata["content_length"],
            "provider_checksums": odata["provider_checksums"],
            "online": odata["online"],
            "eviction_date": odata["eviction_date"],
            "content_date": odata["content_date"],
            "status": "selected",
            "created_at": now,
            "updated_at": now,
            "progress_bytes": 0,
            "file_path": None,
            "file_name": filename,
            "file_size_bytes": None,
            "computed_sha256": None,
            "checksum_status": None,
            "validation": None,
            "warnings": [],
            "error": None,
            "attempts": [],
            "attribution": SAFE_ATTRIBUTION,
        }
        with self._manifest_lock:
            manifest = self._load_manifest()
            manifest["downloads"].append(entry)
            self._save_manifest(manifest)
        return self._public_job(entry), True

    def _access_token(self) -> str:
        if not self.settings.has_cdse_credentials:
            raise ProductDownloadError("credentials_required", "CDSE download credentials are not configured.", 503)
        now = time.monotonic()
        with self._token_lock:
            if self._token and now < self._token_expires_at:
                return self._token
            response: httpx.Response | None = None
            for attempt in range(1, self.settings.max_attempts + 1):
                try:
                    with self._client() as client:
                        response = client.post(
                            TOKEN_URL,
                            data={
                                "client_id": "cdse-public",
                                "grant_type": "password",
                                "username": self.settings.cdse_username,
                                "password": self.settings.cdse_password,
                            },
                        )
                except httpx.TimeoutException as exc:
                    if attempt < self.settings.max_attempts:
                        self.sleep(min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds))
                        continue
                    raise ProductDownloadError("authentication_timeout", "The CDSE token request timed out.", 503) from exc
                except httpx.RequestError as exc:
                    if attempt < self.settings.max_attempts:
                        self.sleep(min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds))
                        continue
                    raise ProductDownloadError("authentication_network_error", "Could not connect to the CDSE token service.", 503) from exc
                if response.status_code in (401, 403):
                    raise ProductDownloadError("authentication_failed", "CDSE rejected the configured account credentials.", 401, response.status_code)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.settings.max_attempts:
                        delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                        if delay is not None:
                            self.sleep(delay)
                            continue
                    raise ProductDownloadError(
                        "authentication_service_unavailable",
                        "The CDSE token service is temporarily unavailable.",
                        503,
                        response.status_code,
                        response.headers.get("Retry-After"),
                    )
                if not 200 <= response.status_code < 300:
                    raise ProductDownloadError("authentication_failed", "CDSE could not issue a download token.", 502, response.status_code)
                break
            if response is None:
                raise ProductDownloadError("authentication_failed", "CDSE did not return an authentication response.", 502)
            try:
                body = response.json()
            except ValueError as exc:
                raise ProductDownloadError("invalid_authentication_response", "CDSE returned an invalid token response.", 502) from exc
            token = body.get("access_token") if isinstance(body, dict) else None
            expires_in = body.get("expires_in", 300) if isinstance(body, dict) else 300
            if not isinstance(token, str) or not token or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
                raise ProductDownloadError("invalid_authentication_response", "CDSE token response did not include a usable access token.", 502)
            self._token = token
            self._token_expires_at = time.monotonic() + max(1.0, float(expires_in) - 60.0)
            return token

    def _invalidate_access_token(self, rejected_token: str) -> None:
        """Clear only the cached token that the download endpoint rejected."""
        with self._token_lock:
            if self._token == rejected_token:
                self._token = None
                self._token_expires_at = 0.0

    def _begin_attempt(self, download_id: str, number: int) -> None:
        now = self._now()
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next(item for item in manifest["downloads"] if item.get("download_id") == download_id)
            entry["status"] = "downloading"
            entry["progress_bytes"] = 0
            entry["attempts"].append({"number": number, "started_at": now, "ended_at": None, "outcome": "downloading"})
            entry["updated_at"] = now
            self._save_manifest(manifest)

    def _finish_attempt(self, download_id: str, outcome: str, error: dict[str, str] | None = None) -> None:
        now = self._now()
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next(item for item in manifest["downloads"] if item.get("download_id") == download_id)
            if entry.get("attempts"):
                entry["attempts"][-1].update({"ended_at": now, "outcome": outcome})
                if error:
                    entry["attempts"][-1]["error"] = error
            entry["updated_at"] = now
            self._save_manifest(manifest)

    def _append_auth_attempt(self, download_id: str, outcome: str, error: dict[str, str] | None = None) -> None:
        now = self._now()
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next(item for item in manifest["downloads"] if item.get("download_id") == download_id)
            entry["attempts"].append({"number": len(entry["attempts"]) + 1, "stage": "authentication", "started_at": now, "ended_at": now, "outcome": outcome, "error": error})
            entry["updated_at"] = now
            self._save_manifest(manifest)

    def _write_progress(self, download_id: str, count: int) -> None:
        with self._manifest_lock:
            manifest = self._load_manifest()
            entry = next(item for item in manifest["downloads"] if item.get("download_id") == download_id)
            entry["progress_bytes"] = count
            entry["updated_at"] = self._now()
            self._save_manifest(manifest)

    def _download_once(
        self,
        entry: dict[str, Any],
        token: str,
        attempt_number: int,
    ) -> tuple[Path, dict[str, str], int, int | None]:
        temp_dir = self.settings.data_dir / "temporary"
        temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        final_path = self.settings.data_dir / "products" / entry["file_name"]
        final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f"{entry['download_id']}-{attempt_number}-",
            suffix=".part",
            dir=temp_dir,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        digests = {"sha256": hashlib.sha256(), "md5": hashlib.md5(usedforsecurity=False)}
        downloaded = 0
        response_length: int | None = None
        try:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream, application/zip, */*"}
            with self._client() as client:
                with self._safe_download_stream(client, entry["source_endpoint"], headers) as response:
                    if response.status_code in (401, 403):
                        if response.status_code == 401:
                            raise ProductDownloadError("download_authentication_failed", "CDSE rejected the download token.", 401, response.status_code)
                        raise ProductDownloadError("download_access_denied", "CDSE denied access to the selected product.", 403, response.status_code)
                    if response.status_code in (429, 500, 502, 503, 504):
                        raise ProductDownloadError(
                            "download_rate_limited" if response.status_code == 429 else "download_upstream_unavailable",
                            "CDSE requested that the product download be retried later." if response.status_code == 429 else "CDSE returned a temporary product download error.",
                            503,
                            response.status_code,
                            response.headers.get("Retry-After"),
                        )
                    if not 200 <= response.status_code < 300:
                        raise ProductDownloadError("download_rejected", "CDSE rejected the product download request.", 502, response.status_code)
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith("text/") or "json" in content_type or "html" in content_type:
                        raise ProductDownloadError("unexpected_download_format", "CDSE returned a text or JSON response instead of the selected product archive.", 502, response.status_code)
                    raw_response_length = response.headers.get("content-length")
                    if raw_response_length is not None:
                        try:
                            response_length = int(raw_response_length)
                        except ValueError as exc:
                            raise ProductDownloadError("invalid_download_response", "CDSE returned an invalid Content-Length header.", 502) from exc
                    with temporary_path.open("wb") as target:
                        last_saved_progress = 0
                        # Mock transports often return an already-buffered response;
                        # live network responses remain streamed and are read raw so
                        # Content-Length is compared against the transferred bytes.
                        chunks = response.iter_bytes(chunk_size=1024 * 1024) if response.is_stream_consumed else response.iter_raw(chunk_size=1024 * 1024)
                        for chunk in chunks:
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > self.settings.max_download_bytes:
                                raise ProductDownloadError("download_limit_exceeded", "The download exceeded JALDRISHTI_MAX_DOWNLOAD_BYTES.", 413)
                            target.write(chunk)
                            for digest in digests.values():
                                digest.update(chunk)
                            if downloaded - last_saved_progress >= PROGRESS_WRITE_INTERVAL:
                                self._write_progress(entry["download_id"], downloaded)
                                last_saved_progress = downloaded
                        target.flush()
                        os.fsync(target.fileno())
            expected = entry["expected_size_bytes"]
            if downloaded != expected or (response_length is not None and downloaded != response_length):
                raise ProductDownloadError("size_mismatch", "The completed transfer size does not match CDSE product metadata.", 502)
            if downloaded <= 0:
                raise ProductDownloadError("empty_file", "CDSE returned an empty product file.", 502)
            self._write_progress(entry["download_id"], downloaded)
            return temporary_path, {key: digest.hexdigest() for key, digest in digests.items()}, downloaded, response_length
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @contextmanager
    def _safe_download_stream(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
    ) -> Iterator[httpx.Response]:
        current_url = url
        for _redirect_number in range(6):
            with client.stream("GET", current_url, headers=headers, follow_redirects=False) as response:
                if response.status_code not in (301, 302, 303, 307, 308):
                    yield response
                    return

                location = response.headers.get("Location")
                if not location:
                    raise ProductDownloadError("invalid_download_redirect", "CDSE returned a redirect without a destination.", 502)
                next_url = urljoin(current_url, location)
                parsed = urlsplit(next_url)
                host = (parsed.hostname or "").lower()
                trusted_host = (
                    host == "dataspace.copernicus.eu"
                    or host.endswith(".dataspace.copernicus.eu")
                    or host == "cloudferro.com"
                    or host.endswith(".cloudferro.com")
                )
                if parsed.scheme != "https" or not trusted_host:
                    raise ProductDownloadError("unsafe_download_redirect", "CDSE redirected the product download outside its trusted HTTPS domains.", 502)
                current_url = next_url
        raise ProductDownloadError("too_many_download_redirects", "CDSE exceeded the permitted product download redirect count.", 502)

    def run_download_job(self, download_id: str) -> None:
        try:
            entry = self._get_job(download_id)
            try:
                token = self._access_token()
            except ProductDownloadError as exc:
                self._append_auth_attempt(download_id, "failed", {"code": exc.code, "message": exc.message})
                raise

            last_error: ProductDownloadError | None = None
            token_refreshes = 0
            for attempt in range(1, self.settings.max_attempts + 1):
                self._begin_attempt(download_id, attempt)
                temporary_path: Path | None = None
                try:
                    temporary_path, computed, file_size, _response_length = self._download_once(entry, token, attempt)
                    self._finish_attempt(download_id, "downloaded")
                    self._update_job(download_id, {"status": "downloaded", "file_size_bytes": file_size})
                    integrity = validate_native_archive(
                        temporary_path,
                        entry["expected_size_bytes"],
                        entry.get("provider_checksums", []),
                        computed_checksums=computed,
                    )
                    self._update_job(
                        download_id,
                        {
                            "status": "integrity_validated",
                            "computed_sha256": computed.get("sha256"),
                            "checksum_status": integrity["provider_checksum_status"],
                            "validation": {"integrity": integrity},
                            "warnings": integrity["warnings"],
                        },
                    )
                    final_path = self.settings.data_dir / "products" / entry["file_name"]
                    os.replace(temporary_path, final_path)
                    os.chmod(final_path, 0o600)
                    relative_path = final_path.relative_to(self.settings.data_dir).as_posix()
                    self._update_job(download_id, {"file_path": relative_path})
                    try:
                        geospatial = inspect_geospatial_metadata(final_path, integrity)
                    except ProductValidationError as exc:
                        self._finish_attempt(download_id, "validation_failed", {"code": exc.code, "message": exc.message})
                        self._update_job(
                            download_id,
                            {
                                "status": "validation_failed",
                                "file_path": relative_path,
                                "error": {"code": exc.code, "message": exc.message},
                                "validation": {"integrity": integrity, "geospatial": {"status": "failed", "code": exc.code}},
                            },
                        )
                        return
                    self._finish_attempt(download_id, "geospatial_inspected")
                    self._update_job(
                        download_id,
                        {
                            "status": "geospatial_inspected",
                            "file_path": relative_path,
                            "validation": {"integrity": integrity, "geospatial": geospatial},
                            "warnings": [*integrity["warnings"], *geospatial["warnings"]],
                            "error": None,
                        },
                    )
                    return
                except ProductValidationError as exc:
                    if temporary_path:
                        temporary_path.unlink(missing_ok=True)
                    self._finish_attempt(download_id, "validation_failed", {"code": exc.code, "message": exc.message})
                    self._update_job(download_id, {"status": "validation_failed", "error": {"code": exc.code, "message": exc.message}})
                    return
                except ProductDownloadError as exc:
                    if temporary_path:
                        temporary_path.unlink(missing_ok=True)
                    last_error = exc
                    self._finish_attempt(download_id, "failed", {"code": exc.code, "message": exc.message})
                    if exc.code == "download_authentication_failed":
                        self._invalidate_access_token(token)
                        if exc.upstream_status == 401 and token_refreshes == 0 and attempt < self.settings.max_attempts:
                            token_refreshes += 1
                            try:
                                token = self._access_token()
                            except ProductDownloadError as refresh_error:
                                self._append_auth_attempt(
                                    download_id,
                                    "failed",
                                    {"code": refresh_error.code, "message": refresh_error.message},
                                )
                                last_error = refresh_error
                                break
                            continue
                    retryable = exc.code in {
                        "download_rate_limited",
                        "download_upstream_unavailable",
                        "download_timeout",
                        "download_network_error",
                        "size_mismatch",
                    }
                    if retryable and attempt < self.settings.max_attempts:
                        delay = self._retry_delay(exc.retry_after, attempt)
                        if delay is not None:
                            self.sleep(delay)
                            continue
                    break
                except (httpx.TimeoutException, httpx.RequestError) as exc:
                    if temporary_path:
                        temporary_path.unlink(missing_ok=True)
                    last_error = ProductDownloadError(
                        "download_timeout" if isinstance(exc, httpx.TimeoutException) else "download_network_error",
                        "The CDSE product transfer was interrupted; no partial file was accepted.",
                        503,
                    )
                    self._finish_attempt(download_id, "failed", {"code": last_error.code, "message": last_error.message})
                    if attempt < self.settings.max_attempts:
                        self.sleep(min(self.settings.backoff_seconds * (2 ** (attempt - 1)), self.settings.max_backoff_seconds))
                        continue
                    break
                except OSError as exc:
                    if temporary_path:
                        temporary_path.unlink(missing_ok=True)
                    last_error = ProductDownloadError("storage_error", "The product could not be safely written to the configured data directory.", 507)
                    self._finish_attempt(download_id, "failed", {"code": last_error.code, "message": last_error.message})
                    break

            if last_error is None:
                last_error = ProductDownloadError("download_failed", "The product download did not complete.", 503)
            self._update_job(download_id, {"status": "failed", "error": {"code": last_error.code, "message": last_error.message}})
        except ProductDownloadError as exc:
            self._update_job(download_id, {"status": "failed", "error": {"code": exc.code, "message": exc.message}})
        except Exception:
            self._update_job(
                download_id,
                {
                    "status": "failed",
                    "error": {"code": "internal_download_error", "message": "The download failed unexpectedly; sensitive details were not recorded."},
                },
            )
