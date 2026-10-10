from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rasterio.crs import CRS

from .observations import ObservationSearchQuery


class SARPreprocessingRequest(BaseModel):
    """Explicit request to preprocess two already downloaded Sentinel-1 products."""

    model_config = ConfigDict(extra="forbid")

    pre_download_id: UUID
    post_download_id: UUID
    bbox: tuple[float, float, float, float]
    target_crs: str = Field(min_length=4, max_length=80)
    pixel_spacing_m: float = Field(gt=0, le=1000)
    dem_name: Literal["Copernicus 30m Global DEM"] = "Copernicus 30m Global DEM"
    allow_snap_auxiliary_downloads: bool = False

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        parsed = ObservationSearchQuery.parse_bbox(value)
        return ObservationSearchQuery.validate_bbox_bounds(parsed)

    @field_validator("target_crs")
    @classmethod
    def validate_target_crs(cls, value: str) -> str:
        try:
            crs = CRS.from_user_input(value.strip())
        except Exception as exc:
            raise ValueError("target_crs must identify a valid projected coordinate reference system") from exc
        if not crs.is_projected:
            raise ValueError("target_crs must be projected so pixel_spacing_m has meter units")
        if crs.linear_units_factor[1] != 1.0:
            raise ValueError("target_crs must use meters because pixel_spacing_m is specified in meters")
        return crs.to_string()

    @model_validator(mode="after")
    def validate_download_pair(self) -> "SARPreprocessingRequest":
        if self.pre_download_id == self.post_download_id:
            raise ValueError("pre_download_id and post_download_id must refer to different downloads")
        return self


PreprocessingStatus = Literal["selected", "running", "completed", "failed"]


class SARPreprocessingResponse(BaseModel):
    job_id: UUID
    status: PreprocessingStatus
    stage: str
    created_at: str
    updated_at: str
    source_download_ids: list[UUID]
    source_scene_ids: list[str]
    storage: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None
    pair: dict[str, Any] | None = None
    software: dict[str, Any] | None = None
    source_processing: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    model_input_status: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, str] | None = None
