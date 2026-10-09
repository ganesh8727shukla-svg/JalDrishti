from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .observations import ObservationMetadata, ObservationSearchQuery


class ObservationPairSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float]
    event_date: date
    pre_event_start: date
    pre_event_end: date
    post_event_start: date
    post_event_end: date
    limit_per_period: int = Field(default=10, ge=1, le=10)

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        parsed = ObservationSearchQuery.parse_bbox(value)
        return ObservationSearchQuery.validate_bbox_bounds(parsed)

    @model_validator(mode="after")
    def validate_event_windows(self) -> "ObservationPairSearchQuery":
        if self.pre_event_start > self.pre_event_end:
            raise ValueError("pre_event_end must be on or after pre_event_start")
        if self.pre_event_end >= self.event_date:
            raise ValueError("pre-event window must end before event_date")
        if self.post_event_start <= self.event_date:
            raise ValueError("post-event window must start after event_date")
        if self.post_event_start > self.post_event_end:
            raise ValueError("post_event_end must be on or after post_event_start")
        return self

    def pre_query(self) -> ObservationSearchQuery:
        return ObservationSearchQuery(
            bbox=self.bbox,
            start_date=self.pre_event_start,
            end_date=self.pre_event_end,
            limit=self.limit_per_period,
        )

    def post_query(self) -> ObservationSearchQuery:
        return ObservationSearchQuery(
            bbox=self.bbox,
            start_date=self.post_event_start,
            end_date=self.post_event_end,
            limit=self.limit_per_period,
        )


Compatibility = Literal["match", "mismatch", "unknown"]
Coverage = Literal["full", "partial", "none", "unknown"]
PairVerificationStatus = Literal["compatible", "questionable", "not_verified", "unsuitable"]


class ObservationPairCandidate(BaseModel):
    rank: int
    pre_scene_id: str
    post_scene_id: str
    elapsed_seconds: float | None = None
    elapsed_days: float | None = None
    product_compatibility: Compatibility
    temporal_compatibility: Compatibility
    orbit_compatibility: Compatibility
    polarization_compatibility: Compatibility
    pre_aoi_coverage: Coverage
    post_aoi_coverage: Coverage
    common_aoi_coverage: Coverage
    common_footprint_intersection: dict[str, Any] | None = None
    verification_status: PairVerificationStatus
    ranking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ObservationPairSearchResponse(BaseModel):
    status: Literal["completed", "no_data", "partial"]
    query: ObservationPairSearchQuery
    catalogue_source: str
    metadata_retrieved_at: datetime
    pre_observation_count: int
    post_observation_count: int
    pre_has_more: bool
    post_has_more: bool
    observations: list[ObservationMetadata] = Field(default_factory=list)
    pairs: list[ObservationPairCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
