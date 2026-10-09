from datetime import datetime, timezone
from typing import Any

import httpx

from ..schemas.observations import ObservationMetadata, ObservationSearchQuery, ObservationSearchResponse

STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
COLLECTION_ID = "sentinel-1-grd"
REQUEST_TIMEOUT_SECONDS = 20.0


class CatalogueSearchError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.upstream_status = upstream_status


def _parse_provider_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            f"CDSE returned a non-string {field_name} timestamp.",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            f"CDSE returned an invalid {field_name} timestamp.",
        ) from exc
    if parsed.tzinfo is None:
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            f"CDSE returned a timezone-free {field_name} timestamp.",
        )
    return parsed.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _normalize_feature(
    feature: Any,
    retrieved_at: datetime,
) -> ObservationMetadata:
    if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "CDSE returned a STAC item without a string identifier.",
        )

    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "CDSE returned a STAC item without a properties object.",
        )

    collection = feature.get("collection")
    if not isinstance(collection, str):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "CDSE returned a STAC item without a collection identifier.",
        )

    private_properties = properties.get("_private")
    if not isinstance(private_properties, dict):
        private_properties = {}

    raw_relative_orbit = properties.get("sat:relative_orbit")
    relative_orbit: int | None = None
    if raw_relative_orbit is not None:
        if isinstance(raw_relative_orbit, bool) or not isinstance(raw_relative_orbit, int):
            raise CatalogueSearchError(
                "invalid_catalogue_response",
                "CDSE returned an invalid relative-orbit value.",
            )
        relative_orbit = raw_relative_orbit

    raw_polarizations = properties.get("sar:polarizations")
    polarizations: list[str] | None = None
    if raw_polarizations is not None:
        if not isinstance(raw_polarizations, list) or not all(
            isinstance(value, str) for value in raw_polarizations
        ):
            raise CatalogueSearchError(
                "invalid_catalogue_response",
                "CDSE returned an invalid polarization value.",
            )
        polarizations = raw_polarizations

    raw_bbox = feature.get("bbox")
    bbox: list[float] | None = None
    if raw_bbox is not None:
        if not isinstance(raw_bbox, list) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in raw_bbox
        ):
            raise CatalogueSearchError(
                "invalid_catalogue_response",
                "CDSE returned an invalid item bounding box.",
            )
        bbox = [float(value) for value in raw_bbox]

    geometry = feature.get("geometry")
    if geometry is not None and not isinstance(geometry, dict):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "CDSE returned an invalid item footprint.",
        )

    assets = feature.get("assets") or {}
    if not isinstance(assets, dict):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "CDSE returned an invalid assets object.",
        )

    title = _optional_string(properties.get("title")) or _optional_string(feature.get("title"))
    product_name = _optional_string(private_properties.get("product_name"))
    if product_name is None:
        product_name = _optional_string(properties.get("product:name"))

    return ObservationMetadata(
        scene_id=feature["id"],
        product_name=product_name,
        title=title,
        collection=collection,
        product_type=_optional_string(properties.get("product:type")),
        acquisition_start=_parse_provider_datetime(
            properties.get("start_datetime") or properties.get("datetime"),
            "acquisition start",
        ),
        acquisition_end=_parse_provider_datetime(
            properties.get("end_datetime"),
            "acquisition end",
        ),
        polarization=polarizations,
        orbit_direction=_optional_string(properties.get("sat:orbit_state")),
        relative_orbit=relative_orbit,
        platform=_optional_string(properties.get("platform")),
        bbox=bbox,
        footprint=geometry,
        asset_keys=sorted(key for key in assets if isinstance(key, str)),
        catalogue_source=STAC_SEARCH_URL,
        metadata_retrieved_at=retrieved_at,
    )


def search_sentinel1(query: ObservationSearchQuery) -> ObservationSearchResponse:
    payload = {
        "collections": [COLLECTION_ID],
        "bbox": list(query.bbox),
        "datetime": query.to_stac_datetime(),
        "limit": query.limit,
    }

    try:
        response = httpx.post(
            STAC_SEARCH_URL,
            json=payload,
            headers={"Accept": "application/geo+json, application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise CatalogueSearchError(
            "catalogue_timeout",
            "The CDSE catalogue request timed out. Retry the search shortly.",
        ) from exc
    except httpx.RequestError as exc:
        raise CatalogueSearchError(
            "catalogue_network_error",
            "Could not connect to the CDSE catalogue. Check network connectivity and retry.",
        ) from exc

    if not 200 <= response.status_code < 300:
        status = response.status_code
        if status in (401, 403):
            code = "catalogue_authentication_error"
            message = "CDSE rejected anonymous metadata search; check the official service status or authentication policy."
        elif status == 429:
            code = "catalogue_rate_limited"
            message = "CDSE rate-limited the search. Retry after a short wait."
        elif status >= 500:
            code = "catalogue_unavailable"
            message = "The CDSE catalogue returned a server error. Retry later."
        else:
            code = "catalogue_request_rejected"
            message = "The CDSE catalogue rejected the search request. Check the bbox, dates, and collection."
        raise CatalogueSearchError(code, message, upstream_status=status)

    try:
        body = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown")
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            f"The CDSE catalogue returned a non-JSON response (content type: {content_type}).",
            upstream_status=response.status_code,
        ) from exc

    if not isinstance(body, dict) or not isinstance(body.get("features"), list):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "The CDSE response did not contain a STAC features array.",
        )

    retrieved_at = datetime.now(timezone.utc)
    observations = [
        _normalize_feature(feature, retrieved_at)
        for feature in body["features"]
    ]
    links = body.get("links", [])
    if not isinstance(links, list):
        raise CatalogueSearchError(
            "invalid_catalogue_response",
            "The CDSE response contained an invalid links value.",
        )

    raw_matched = body.get("numberMatched")
    number_matched = raw_matched if isinstance(raw_matched, int) and not isinstance(raw_matched, bool) else None
    has_more = any(isinstance(link, dict) and link.get("rel") == "next" for link in links)
    warnings = []
    if not observations:
        warnings.append("CDSE returned no Sentinel-1 GRD items for this bbox and date range.")

    return ObservationSearchResponse(
        status="completed" if observations else "no_data",
        query=query,
        catalogue_source=STAC_SEARCH_URL,
        returned_count=len(observations),
        number_matched=number_matched,
        has_more=has_more,
        metadata_retrieved_at=retrieved_at,
        observations=observations,
        warnings=warnings,
    )
