from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from ...schemas.observation_pairs import (
    ObservationPairSearchQuery,
    ObservationPairSearchResponse,
)
from ...schemas.observations import ObservationSearchQuery, ObservationSearchResponse
from ...services.satellite_catalogue import (
    STAC_SEARCH_URL,
    CatalogueSearchError,
    search_sentinel1,
)
from ...services.scene_matching import rank_observation_pairs

router = APIRouter(prefix="/api/v1/observations", tags=["observations"])


def _catalogue_http_error(exc: CatalogueSearchError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": exc.code,
            "message": exc.message,
            "upstream_status": exc.upstream_status,
        },
    )


@router.get("/search", response_model=ObservationSearchResponse)
def search_observations(
    bbox: str = Query(
        ...,
        description="WGS84 bbox as west,south,east,north; for example 84.04,28.20,84.15,28.31",
    ),
    start_date: date = Query(..., description="First acquisition date to include (UTC date)"),
    end_date: date = Query(..., description="Last acquisition date to include (UTC date)"),
    limit: int = Query(default=5, ge=1, le=10),
) -> ObservationSearchResponse:
    try:
        search_query = ObservationSearchQuery(
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except ValidationError as exc:
        safe_errors = [
            {
                "type": str(error.get("type", "value_error")),
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "Invalid request.")),
            }
            for error in exc.errors()
        ]
        raise HTTPException(status_code=422, detail=safe_errors) from exc

    try:
        return search_sentinel1(search_query)
    except CatalogueSearchError as exc:
        raise _catalogue_http_error(exc) from exc


@router.get("/pairs", response_model=ObservationPairSearchResponse)
def search_observation_pairs(
    bbox: str = Query(..., description="WGS84 bbox as west,south,east,north"),
    event_date: date = Query(...),
    pre_event_start: date = Query(...),
    pre_event_end: date = Query(...),
    post_event_start: date = Query(...),
    post_event_end: date = Query(...),
    limit_per_period: int = Query(default=10, ge=1, le=10),
) -> ObservationPairSearchResponse:
    try:
        query = ObservationPairSearchQuery(
            bbox=bbox,
            event_date=event_date,
            pre_event_start=pre_event_start,
            pre_event_end=pre_event_end,
            post_event_start=post_event_start,
            post_event_end=post_event_end,
            limit_per_period=limit_per_period,
        )
    except ValidationError as exc:
        safe_errors = [
            {
                "type": str(error.get("type", "value_error")),
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "Invalid request.")),
            }
            for error in exc.errors()
        ]
        raise HTTPException(status_code=422, detail=safe_errors) from exc

    try:
        pre_result = search_sentinel1(query.pre_query())
        post_result = search_sentinel1(query.post_query())
    except CatalogueSearchError as exc:
        raise _catalogue_http_error(exc) from exc

    pairs = rank_observation_pairs(
        pre_result.observations,
        post_result.observations,
        query.bbox,
        query.event_date,
    )
    unique_observations = {
        item.scene_id: item
        for item in [*pre_result.observations, *post_result.observations]
    }
    observations = sorted(
        unique_observations.values(),
        key=lambda item: (item.acquisition_start is None, item.acquisition_start or datetime.max.replace(tzinfo=timezone.utc)),
    )

    warnings = [*pre_result.warnings, *post_result.warnings]
    if not pre_result.observations:
        warnings.append("CDSE returned no observations in the configured pre-event window.")
    if not post_result.observations:
        warnings.append("CDSE returned no observations in the configured post-event window.")
    if pre_result.has_more or post_result.has_more:
        warnings.append(
            "At least one period has additional catalogue results; ranking is limited to the requested per-period result limit."
        )
    if not pairs and pre_result.observations and post_result.observations:
        warnings.append("No pre/post pair could be evaluated from the returned catalogue records.")

    status = (
        "partial"
        if pre_result.has_more or post_result.has_more
        else "completed"
        if pairs
        else "no_data"
    )
    if not pre_result.observations or not post_result.observations:
        status = "partial" if pre_result.has_more or post_result.has_more else "no_data"

    return ObservationPairSearchResponse(
        status=status,
        query=query,
        catalogue_source=STAC_SEARCH_URL,
        metadata_retrieved_at=datetime.now(timezone.utc),
        pre_observation_count=len(pre_result.observations),
        post_observation_count=len(post_result.observations),
        pre_has_more=pre_result.has_more,
        post_has_more=post_result.has_more,
        observations=observations,
        pairs=pairs,
        warnings=warnings,
    )
