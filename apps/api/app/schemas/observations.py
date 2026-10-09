from datetime import date, datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ObservationSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float]
    start_date: date
    end_date: date
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("bbox", mode="before")
    @classmethod
    def parse_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        if isinstance(value, str):
            parts = value.split(",")
        elif isinstance(value, (list, tuple)):
            parts = value
        else:
            raise ValueError("bbox must be west,south,east,north")

        if len(parts) != 4:
            raise ValueError("bbox must contain exactly four WGS84 values")

        try:
            parsed = tuple(float(part) for part in parts)
        except (TypeError, ValueError) as exc:
            raise ValueError("bbox values must be numeric") from exc
        if not all(isfinite(part) for part in parsed):
            raise ValueError("bbox values must be finite numbers")
        return parsed  # type: ignore[return-value]

    @staticmethod
    def validate_bbox_bounds(
        bbox: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        west, south, east, north = bbox
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("bbox longitude values must be between -180 and 180")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("bbox latitude values must be between -90 and 90")
        if west >= east:
            raise ValueError("bbox west must be less than east")
        if south >= north:
            raise ValueError("bbox south must be less than north")
        return bbox

    @model_validator(mode="after")
    def validate_bounds_and_dates(self) -> "ObservationSearchQuery":
        self.validate_bbox_bounds(self.bbox)
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self

    def to_stac_datetime(self) -> str:
        return (
            f"{self.start_date.isoformat()}T00:00:00Z/"
            f"{self.end_date.isoformat()}T23:59:59.999999Z"
        )


class ObservationMetadata(BaseModel):
    scene_id: str
    product_name: str | None = None
    title: str | None = None
    collection: str
    sensor: Literal["Sentinel-1"] = "Sentinel-1"
    product_type: str | None = None
    acquisition_start: datetime | None = None
    acquisition_end: datetime | None = None
    polarization: list[str] | None = None
    orbit_direction: str | None = None
    relative_orbit: int | None = None
    platform: str | None = None
    bbox: list[float] | None = None
    footprint: dict[str, Any] | None = None
    asset_keys: list[str] = Field(default_factory=list)
    catalogue_source: str
    metadata_retrieved_at: datetime


class ObservationSearchResponse(BaseModel):
    status: Literal["completed", "no_data"]
    query: ObservationSearchQuery
    catalogue_source: str
    returned_count: int
    number_matched: int | None = None
    has_more: bool
    metadata_retrieved_at: datetime
    observations: list[ObservationMetadata]
    warnings: list[str] = Field(default_factory=list)
