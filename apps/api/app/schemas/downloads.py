from datetime import date, datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .observations import ObservationSearchQuery


SCENE_ID_PATTERN = re.compile(r"S1[A-Z]_[A-Za-z0-9_-]{8,170}\Z")


class ProductDownloadSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pre_scene_id: str = Field(min_length=12, max_length=180)
    post_scene_id: str = Field(min_length=12, max_length=180)
    event_date: date
    bbox: tuple[float, float, float, float]
    role: Literal["pre_event", "post_event"]
    asset_key: Literal["Product"]

    @field_validator("pre_scene_id", "post_scene_id")
    @classmethod
    def validate_scene_id(cls, value: str) -> str:
        if not SCENE_ID_PATTERN.fullmatch(value):
            raise ValueError("scene IDs must be Sentinel-1 catalogue identifiers")
        return value

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        parsed = ObservationSearchQuery.parse_bbox(value)
        return ObservationSearchQuery.validate_bbox_bounds(parsed)

    @model_validator(mode="after")
    def validate_pair(self) -> "ProductDownloadSelection":
        if self.pre_scene_id == self.post_scene_id:
            raise ValueError("pre_scene_id and post_scene_id must identify different observations")
        return self

    @property
    def selected_scene_id(self) -> str:
        return self.pre_scene_id if self.role == "pre_event" else self.post_scene_id

    @property
    def counterpart_scene_id(self) -> str:
        return self.post_scene_id if self.role == "pre_event" else self.pre_scene_id


class ProductPairDownloadPreviewRequest(BaseModel):
    """Preflight one explicitly selected pair without creating download jobs."""

    model_config = ConfigDict(extra="forbid")

    pre_scene_id: str = Field(min_length=12, max_length=180)
    post_scene_id: str = Field(min_length=12, max_length=180)
    event_date: date
    bbox: tuple[float, float, float, float]

    @field_validator("pre_scene_id", "post_scene_id")
    @classmethod
    def validate_scene_id(cls, value: str) -> str:
        if not SCENE_ID_PATTERN.fullmatch(value):
            raise ValueError("scene IDs must be Sentinel-1 catalogue identifiers")
        return value

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: Any) -> tuple[float, float, float, float]:
        parsed = ObservationSearchQuery.parse_bbox(value)
        return ObservationSearchQuery.validate_bbox_bounds(parsed)

    @model_validator(mode="after")
    def validate_pair(self) -> "ProductPairDownloadPreviewRequest":
        if self.pre_scene_id == self.post_scene_id:
            raise ValueError("pre_scene_id and post_scene_id must identify different observations")
        return self


class ProductAssetPreview(BaseModel):
    scene_id: str
    role: Literal["pre_event", "post_event"]
    product_name: str
    asset_key: Literal["Product"] = "Product"
    acquisition_start: datetime | None = None
    expected_size_bytes: int
    provider_checksums: list[dict[str, str]] = Field(default_factory=list)


class ProductPairDownloadPreviewResponse(BaseModel):
    pre_event: ProductAssetPreview
    post_event: ProductAssetPreview
    total_size_bytes: int
    available_disk_bytes: int
    warnings: list[str] = Field(default_factory=list)


DownloadStatus = Literal[
    "selected",
    "downloading",
    "downloaded",
    "integrity_validated",
    "geospatial_inspected",
    "failed",
    "validation_failed",
]


class ProductDownloadResponse(BaseModel):
    download_id: str
    scene_id: str
    role: Literal["pre_event", "post_event"]
    asset_key: Literal["Product"]
    duplicate: bool = False
    product_name: str | None = None
    status: DownloadStatus
    created_at: datetime
    updated_at: datetime
    progress_bytes: int = 0
    expected_size_bytes: int | None = None
    file_size_bytes: int | None = None
    file_path: str | None = None
    computed_sha256: str | None = None
    provider_checksums: list[dict[str, str]] = Field(default_factory=list)
    checksum_status: str | None = None
    validation: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, str] | None = None


class DownloadManifestResponse(BaseModel):
    downloads: list[ProductDownloadResponse]
