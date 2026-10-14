from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FloodAnalysisRequest(BaseModel):
    """Start inference for an existing, completed Sentinel preprocessing job."""

    model_config = ConfigDict(extra="forbid")

    preprocessing_job_id: UUID
    tile_size: int = Field(default=256, ge=16, le=1024)
    overlap: int = Field(default=32, ge=0, lt=1024)
    max_pixels: int = Field(default=10_000_000, ge=1, le=50_000_000)

    @model_validator(mode="after")
    def validate_overlap(self) -> "FloodAnalysisRequest":
        if self.overlap >= self.tile_size:
            raise ValueError("overlap must be smaller than tile_size")
        return self


FloodAnalysisStatus = Literal[
    "queued",
    "running",
    "blocked",
    "pending_independent_validation",
    "completed",
    "failed",
]


class FloodAnalysisResponse(BaseModel):
    analysis_id: UUID
    status: FloodAnalysisStatus
    stage: str
    created_at: str
    updated_at: str
    preprocessing_job_id: UUID
    event_id: str | None = None
    event_date: str | None = None
    model_version: str | None = None
    model_checkpoint_sha256: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    rasters: dict[str, Any] = Field(default_factory=dict)
    boundaries: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, str] | None = None
